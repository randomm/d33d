/**
 * ChatPanel tests — message display, input, send, streaming indicator,
 * and inline render images.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ChatPanel, type ChatMessage } from "../ChatPanel";

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
});
