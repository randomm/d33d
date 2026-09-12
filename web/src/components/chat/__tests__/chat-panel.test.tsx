/**
 * ChatPanel tests — message display, input, send, streaming indicator,
 * and inline render images.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatPanel, type ChatMessage, MARKER_COLOR } from "../ChatPanel";

describe("ChatPanel", () => {
  it("renders empty state with no messages", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} renders={[]} />);
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    expect(screen.getByTestId("chat-input")).toBeTruthy();
  });

  it("displays user and assistant messages in order", () => {
    const messages: ChatMessage[] = [
      { id: "m1", role: "user", content: "Make me a box" },
      { id: "m2", role: "assistant", content: "I'll design a box for you." },
    ];
    render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
    expect(screen.getByTestId("chat-msg-user")).toHaveTextContent("Make me a box");
    expect(
      screen.getByTestId("chat-msg-assistant"),
    ).toHaveTextContent("I'll design a box for you.");
  });

  it("calls onSend with trimmed text when user submits", () => {
    const onSend = vi.fn();
    render(<ChatPanel messages={[]} onSend={onSend} renders={[]} />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "  hello  " } });
    fireEvent.submit(input.closest("form")!);
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does not send empty or whitespace-only messages", () => {
    const onSend = vi.fn();
    render(<ChatPanel messages={[]} onSend={onSend} renders={[]} />);
    const input = screen.getByTestId("chat-input");
    fireEvent.change(input, { target: { value: "   " } });
    fireEvent.submit(input.closest("form")!);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("shows streaming cursor on streaming messages", () => {
    const messages: ChatMessage[] = [
      { id: "m1", role: "assistant", content: "Thinking…", streaming: true },
    ];
    render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
    expect(screen.getByTestId("streaming-cursor")).toBeTruthy();
  });

  it("renders inline render images in order", () => {
    const renders = [
      { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
      { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
    ];
    render(<ChatPanel messages={[]} onSend={vi.fn()} renders={renders} />);
    expect(screen.getByTestId("render-img-view_00_front.png")).toBeTruthy();
    expect(screen.getByTestId("render-img-view_01_back.png")).toBeTruthy();
  });

  it("disables send button when input is empty", () => {
    render(<ChatPanel messages={[]} onSend={vi.fn()} renders={[]} />);
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
      render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
      const thumb = screen.getByTestId("selection-thumbnail-m1");
      expect(thumb).toBeTruthy();
      expect(thumb.getAttribute("src")).toBe("data:image/png;base64,AAA");
    });

    it("does not render a selection thumbnail for messages without a selection", () => {
      const messages: ChatMessage[] = [
        { id: "m1", role: "user", content: "make a box" },
      ];
      render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
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
      render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
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
      render(<ChatPanel messages={messages} onSend={vi.fn()} renders={[]} />);
      const thumb = screen.getByTestId("selection-thumbnail-m1");
      expect(thumb.style.borderColor.length).toBeGreaterThan(0);
    });
  });
});
