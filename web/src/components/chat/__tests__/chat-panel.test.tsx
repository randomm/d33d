/**
 * ChatPanel tests — message display, input, send, streaming indicator,
 * and pass-bearing turns (issue #125, W10).
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatPanel, type ChatMessage, MARKER_COLOR } from "../ChatPanel";
import type { RenderImage } from "../../../lib/renderImage";

describe("ChatPanel", () => {
  it("renders empty state with no messages", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} />);
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
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
    render(<ChatPanel messages={[]} onSend={onSend} />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "  hello  " } });
    fireEvent.submit(input.closest("form")!);
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does not send empty or whitespace-only messages", () => {
    const onSend = vi.fn();
    render(<ChatPanel messages={[]} onSend={onSend} />);
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
  });

  it("disables send button when input is empty", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} />);
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
