/**
 * Composer tests — the chat input form.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Composer } from "../Composer";

describe("Composer", () => {
  it("renders the input and send button", () => {
    render(<Composer value="" onChange={vi.fn()} onSend={vi.fn()} />);
    expect(screen.getByTestId("chat-input")).toBeTruthy();
    expect(screen.getByTestId("chat-send-btn")).toBeTruthy();
  });

  it("reflects the current value in the input", () => {
    render(<Composer value="hello" onChange={vi.fn()} onSend={vi.fn()} />);
    expect((screen.getByTestId("chat-input") as HTMLInputElement).value).toBe("hello");
  });

  it("calls onChange as the user types", () => {
    const onChange = vi.fn();
    render(<Composer value="" onChange={onChange} onSend={vi.fn()} />);
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: "typed" } });
    expect(onChange).toHaveBeenCalledWith("typed");
  });

  it("calls onSend with trimmed text on submit", () => {
    const onSend = vi.fn();
    render(<Composer value="  hello  " onChange={vi.fn()} onSend={onSend} />);
    fireEvent.submit(screen.getByTestId("chat-input").closest("form")!);
    expect(onSend).toHaveBeenCalledWith("hello");
  });

  it("does NOT call onSend for whitespace-only input", () => {
    const onSend = vi.fn();
    render(<Composer value="   " onChange={vi.fn()} onSend={onSend} />);
    fireEvent.submit(screen.getByTestId("chat-input").closest("form")!);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("regression guard: the form the input submits to triggers onSend via native submission", () => {
    // jsdom does not bridge a keyDown(Enter) to native form submission, so this
    // guard submits the form element directly — proving the input sits inside
    // a real <form onSubmit> (the structure a browser uses for Enter-to-submit)
    // with a type=submit button, rather than relying on any keydown handler.
    const onSend = vi.fn();
    render(<Composer value="regression check" onChange={vi.fn()} onSend={onSend} />);
    const input = screen.getByTestId("chat-input");
    const form = input.closest("form");
    expect(form).toBeTruthy();
    const submitButton = form!.querySelector("button[type=submit]");
    expect(submitButton).toBeTruthy();
    fireEvent.submit(form!);
    expect(onSend).toHaveBeenCalledWith("regression check");
  });

  it("disables the send button when the input is empty", () => {
    render(<Composer value="" onChange={vi.fn()} onSend={vi.fn()} />);
    expect((screen.getByTestId("chat-send-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("disables the send button when inFlight is true", () => {
    render(<Composer value="hi" onChange={vi.fn()} onSend={vi.fn()} inFlight />);
    expect((screen.getByTestId("chat-send-btn") as HTMLButtonElement).disabled).toBe(true);
  });

  it("enables the send button when there is text and not in flight", () => {
    render(<Composer value="hi" onChange={vi.fn()} onSend={vi.fn()} />);
    expect((screen.getByTestId("chat-send-btn") as HTMLButtonElement).disabled).toBe(false);
  });
});
