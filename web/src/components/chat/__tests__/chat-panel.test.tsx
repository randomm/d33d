/**
 * ChatPanel tests — message display, input, send, streaming indicator,
 * and pass-bearing turns (issue #125, W10).
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatPanel, type ChatMessage, MARKER_COLOR } from "../ChatPanel";
import type { RenderImage } from "../../../lib/renderImage";

describe("ChatPanel", () => {
  it("hides the composer when hideComposer is true (issue #193 — the first-run screen carries it)", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} hideComposer />);
    expect(screen.queryByTestId("chat-input")).toBeNull();
    expect(screen.queryByTestId("chat-send-btn")).toBeNull();
  });

  it("renders empty state with no messages (panel present, composer hidden — issue #193)", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} hideComposer />);
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
  });

  it("shows the composer when hideComposer is false (default)", () => {
    render(<ChatPanel messages={[{ id: "seed", role: "user", content: "seed" }]} onSend={vi.fn()} />);
    expect(screen.getByTestId("chat-input")).toBeTruthy();
  });

  it("displays user and assistant messages in order", () => {
    const messages: ChatMessage[] = [
      { id: "m1", role: "user", content: "Make me a box" },
      { id: "m2", role: "assistant", content: "I'll design a box for you." },
    ];
    render(<ChatPanel messages={messages} onSend={vi.fn()} />);
    expect(screen.getByTestId("chat-msg-user")).toHaveTextContent("Make me a box");
    expect(
      screen.getByTestId("chat-msg-assistant"),
    ).toHaveTextContent("I'll design a box for you.");
  });

  it("calls onSend with trimmed text when user submits", () => {
    const onSend = vi.fn();
    render(<ChatPanel messages={[{ id: "seed", role: "user", content: "seed" }]} onSend={onSend} />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "  hello  " } });
    fireEvent.submit(input.closest("form")!);
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does not send empty or whitespace-only messages", () => {
    const onSend = vi.fn();
    render(<ChatPanel messages={[{ id: "seed", role: "user", content: "seed" }]} onSend={onSend} />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.submit(input.closest("form")!);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("shows streaming cursor on streaming messages", () => {
    const messages: ChatMessage[] = [
      { id: "m1", role: "assistant", content: "Thinking…", streaming: true },
    ];
    render(<ChatPanel messages={messages} onSend={vi.fn()} />);
    expect(screen.getByTestId("streaming-cursor")).toBeTruthy();
  });

  describe("pass-bearing turns (issue #125)", () => {
    const views: RenderImage[] = [
      { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
      { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
    ];

    it("renders a PassCard for an assistant message that carries a versionId", () => {
      const messages: ChatMessage[] = [
        { id: "m1", role: "assistant", content: "A box, per your ask.", versionId: 4, views },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      expect(screen.getByTestId("pass-card")).toBeTruthy();
      // The views live on the pass card, not as the old chat-renders block.
      expect(screen.getAllByTestId(/^pass-card-view-/)).toHaveLength(2);
      expect(screen.queryByTestId("chat-renders")).toBeNull();
    });

    it("keeps the source in the disclosure, never as chat message text", () => {
      const source = "cube([20, 20, 20]);\n// two lines";
      const messages: ChatMessage[] = [
        {
          id: "m1",
          role: "assistant",
          content: "A box.",
          versionId: 4,
          views,
          source,
        },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      // Collapsed by default: the source is not visible in the DOM.
      expect(screen.queryByTestId("pass-card-source")).toBeNull();
      // ...and the source string does not appear anywhere in the message.
      expect(screen.getByTestId("chat-msg-assistant").textContent).not.toContain(
        "cube([20, 20, 20])",
      );
      // Expanding the disclosure surfaces it as disclosure content.
      fireEvent.click(screen.getByTestId("pass-card-source-toggle"));
      expect(screen.getByTestId("pass-card-source").textContent).toBe(source);
    });

    it("renders a plain message for an assistant turn that produced no version", () => {
      const messages: ChatMessage[] = [
        { id: "m1", role: "assistant", content: "What size do you need?" },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      expect(screen.queryByTestId("pass-card")).toBeNull();
      expect(screen.getByTestId("chat-msg-assistant")).toHaveTextContent(
        "What size do you need?",
      );
    });

    it("renders an answered-question turn as plain text with no PassCard, no filmstrip entry, no version badge (issue #249)", () => {
      // The answer path: an assistant message with the answer text as
      // content, no versionId, no views, no source. This is what the
      // App's onDone handler produces when data.kind === "answer" — it
      // sets content to the answer text verbatim (not the passCard
      // summary) and leaves versionId undefined, so ChatPanel renders
      // a plain message.
      const answerText = "It is 12 mm tall — you said that. The footprint is 20 × 20 mm, which I assumed.";
      const messages: ChatMessage[] = [
        { id: "m1", role: "user", content: "How tall is it now?" },
        { id: "m2", role: "assistant", content: answerText },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);

      // The answer renders verbatim as plain text in the assistant message.
      expect(screen.getByTestId("chat-msg-assistant")).toHaveTextContent(answerText);
      // No PassCard — the answer path produces no version.
      expect(screen.queryByTestId("pass-card")).toBeNull();
      // No view thumbnails (no version-created frame → no views).
      expect(screen.queryAllByTestId(/^pass-card-view-/)).toHaveLength(0);
      // The user's question is also in the transcript.
      expect(screen.getByTestId("chat-msg-user")).toHaveTextContent("How tall is it now?");
    });

    it("an answered turn with streaming:false renders no streaming cursor (issue #249)", () => {
      // The answer path sets streaming: false in onDone. The message
      // should not show the streaming cursor.
      const answerText = "It is 12 mm tall — you said that.";
      const messages: ChatMessage[] = [
        { id: "m1", role: "assistant", content: answerText, streaming: false },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      expect(screen.queryByTestId("streaming-cursor")).toBeNull();
      expect(screen.getByTestId("chat-msg-assistant")).toHaveTextContent(answerText);
    });

    it("the confirmation offer renders as a plain assistant message after the pass card (issue #250)", () => {
      // The done frame's additive `confirm_offer` field is routed by App as
      // its OWN assistant message AFTER the pass card — a plain sentence in
      // the flow, not inside the PassCard, not a form. The offer message
      // carries no versionId (so no PassCard of its own), no views, no
      // failure.
      const offerText =
        "I assumed 3.0\u202Fmm for Wall thickness. Want it different?";
      const messages: ChatMessage[] = [
        { id: "m1", role: "user", content: "make a shelf spacer 12 mm tall" },
        { id: "m2", role: "assistant", content: "pass summary", versionId: 4, views },
        { id: "m3", role: "assistant", content: offerText },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);

      // The offer text renders verbatim as a plain assistant message
      // (the NNBSP in "3.0 mm" is the deck's `mm` formatter, so the
      // substring assertion uses the spaced form of the value).
      const offerTurn = screen.getAllByTestId("chat-msg-assistant")[1];
      expect(offerTurn.textContent).toContain("I assumed 3.0\u202fmm for Wall thickness.");
      expect(offerTurn.textContent).toContain("Want it different?");
      // It is a separate turn, not part of the pass card's summary.
      const passCard = screen.getByTestId("pass-card");
      expect(passCard.textContent).not.toContain(offerText);
      expect(screen.getByTestId("pass-card-summary").textContent).toBe("pass summary");
      // Exactly one pass card — the offer message is not a card.
      expect(screen.getAllByTestId("pass-card")).toHaveLength(1);
      // No form, no buttons: an offer is a sentence, not a control. The
      // composer (the chat input form) is the ONLY form in the panel, and
      // the offer turn itself carries no button/input of its own — it is a
      // plain message, not a form control.
      expect(offerTurn.querySelectorAll("button")).toHaveLength(0);
      expect(offerTurn.querySelectorAll("input")).toHaveLength(0);
      expect(screen.getByTestId("chat-input")).toBeTruthy();
    });

    it("the confirmation acknowledgement renders as a plain message with the value in the mono face (issue #250)", () => {
      // The accepted-offer acknowledgement ("Got it — {label} stays
      // {value}.") is its own plain assistant message (no design run, no
      // new version). The measured value renders in the mono face — the
      // project rule that a number can never hide inside a sentence —
      // while the prose stays in the UI face.
      const ackText = "Got it — Wall thickness stays 3.0\u202Fmm.";
      const messages: ChatMessage[] = [
        { id: "m1", role: "assistant", content: "I assumed 3.0\u202Fmm for Wall thickness. Want it different?" },
        { id: "m2", role: "user", content: "yes" },
        {
          id: "m3",
          role: "assistant",
          content: ackText,
          confirmAck: { label: "Wall thickness", value: "3.0\u202Fmm" },
        },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);

      // The full acknowledgement sentence renders in the transcript
      // (the NBSP in "3.0 mm" is the deck's `mm` formatter, so the
      // substring assertion uses the spaced form of the value).
      const ackTurn = screen.getAllByTestId("chat-msg-assistant")[1];
      expect(ackTurn.textContent).toContain("Got it — Wall thickness stays 3.0\u202fmm.");
      // The value span renders in the mono face (the identifier fallback
      // would render there too — one rule, not two).
      const valueEl = screen.getAllByTestId("chat-msg-assistant")[1].querySelector(
        "[class*='mono'], [style*='mono']",
      );
      expect(valueEl, "the value must render in a mono-face span").not.toBeNull();
      // The value span carries the sentence's terminal period (the ack
      // sentence is "…stays {value}." — the period belongs to the value's
      // mono-face span, not to the prose label).
      expect(valueEl?.textContent).toBe("3.0\u202Fmm.");
      expect(valueEl?.getAttribute("style")).toContain("var(--font-mono)");
      // No PassCard — confirming a value never creates a version.
      expect(screen.queryByTestId("pass-card")).toBeNull();
    });
  });

  it("disables send button when input is empty", () => {
    render(<ChatPanel messages={[{ id: "seed", role: "user", content: "seed" }]} onSend={vi.fn()} />);
    expect(screen.getByTestId("chat-send-btn")).toBeDisabled();
  });

  describe("selection thumbnail persistence", () => {
    it("renders a selection thumbnail attached to the message that carries it", () => {
      const messages: ChatMessage[] = [
        {
          id: "m1",
          role: "user",
          content: "open up this spiral",
          selection: {
            thumbnail: "data:image/png;base64,AAA",
            viewId: "iso",
            moduleIds: ["curl_3", "curl_4"],
          },
        },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      const thumb = screen.getByTestId("selection-thumbnail-m1");
      expect(thumb).toBeTruthy();
      expect(thumb.getAttribute("src")).toBe("data:image/png;base64,AAA");
    });

    it("does not render a selection thumbnail for messages without a selection", () => {
      const messages: ChatMessage[] = [
        { id: "m1", role: "user", content: "make a box" },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      expect(screen.queryByTestId("selection-thumbnail-m1")).toBeNull();
    });

    it("keeps each message's own selection thumbnail attached when scrolling back through history", () => {
      const messages: ChatMessage[] = [
        {
          id: "m1",
          role: "user",
          content: "open up this spiral",
          selection: {
            thumbnail: "data:image/png;base64,AAA",
            viewId: "iso",
            moduleIds: ["curl_3"],
          },
        },
        { id: "m2", role: "assistant", content: "Sure, opening it up." },
        { id: "m3", role: "user", content: "now thicken the ear wire" },
        {
          id: "m4",
          role: "user",
          content: "just this part",
          selection: {
            thumbnail: "data:image/png;base64,BBB",
            viewId: "front",
            moduleIds: ["ear_wire"],
          },
        },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      // The earlier message's thumbnail is still attached to its own turn,
      // not overwritten or lost as later turns without a selection arrive.
      expect(screen.getByTestId("selection-thumbnail-m1").getAttribute("src")).toBe(
        "data:image/png;base64,AAA",
      );
      expect(screen.queryByTestId("selection-thumbnail-m2")).toBeNull();
      expect(screen.queryByTestId("selection-thumbnail-m3")).toBeNull();
      expect(screen.getByTestId("selection-thumbnail-m4").getAttribute("src")).toBe(
        "data:image/png;base64,BBB",
      );
    });
  });

  describe("marker colour constant", () => {
    it("is red or high-contrast warm (VLM marker-fragility: red-to-blue flips correctness)", () => {
      // arXiv 2512.17875: markers must be red/warm, not a styling choice.
      // Assert on parsed RGB channels so a hex-string restyle to a cool
      // colour (e.g. blue) cannot slip through unnoticed.
      const hex = MARKER_COLOR.replace("#", "");
      const r = Number.parseInt(hex.slice(0, 2), 16);
      const g = Number.parseInt(hex.slice(2, 4), 16);
      const b = Number.parseInt(hex.slice(4, 6), 16);
      expect(r).toBeGreaterThanOrEqual(200);
      expect(r).toBeGreaterThan(g);
      expect(r).toBeGreaterThan(b);
      expect(b).toBeLessThan(100);
    });

    it("is applied as the selection thumbnail's border colour", () => {
      const messages: ChatMessage[] = [
        {
          id: "m1",
          role: "user",
          content: "open up this spiral",
          selection: {
            thumbnail: "data:image/png;base64,AAA",
            viewId: "iso",
            moduleIds: ["curl_3"],
          },
        },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} />);
      const thumb = screen.getByTestId("selection-thumbnail-m1");
      expect(thumb.style.borderColor.length).toBeGreaterThan(0);
    });
  });
});
