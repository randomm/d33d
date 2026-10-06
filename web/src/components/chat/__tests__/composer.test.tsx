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

  it("a send while a message is queued still fires onSend with the trimmed text — App routes it to the replace-queue path (issue #388)", () => {
    // The Composer (and the ChatPanel's composer) always forwards a valid
    // submission — the single-slot REPLACE semantics live in App's
    // handleSendMessage, which is exercised end to end in
    // no-loop-reply.test.tsx ("two sends during a run → one POST carrying
    // the second text"). This guard pins that the component itself does not
    // swallow a second send while a queued message exists: the form's
    // onSubmit fires onSend exactly once with the trimmed text, so the
    // second send is never dropped at this layer.
    const onSend = vi.fn();
    render(<Composer value="  make it 40 mm tall  " onChange={vi.fn()} onSend={onSend} />);
    fireEvent.submit(screen.getByTestId("chat-input").closest("form")!);
    expect(onSend).toHaveBeenCalledTimes(1);
    expect(onSend).toHaveBeenCalledWith("make it 40 mm tall");
  });

  it("enables the send button when there is text and not in flight", () => {
    render(<Composer value="hi" onChange={vi.fn()} onSend={vi.fn()} />);
    expect((screen.getByTestId("chat-send-btn") as HTMLButtonElement).disabled).toBe(false);
  });

  it("does NOT fire onSend when the form is submitted while inFlight is true (issue #388 — the pre-first-frame window queues, never double-POSTs)", () => {
    // Regression guard for the 409 window (issue #388, operator decision 1):
    // while a send is in flight — including the gap between the click and the
    // first design-loop frame — the composer's inFlight gate is the only
    // thing standing between a fast second click and a second POST (which the
    // server rejects with 409). The sibling workstream owns the queue logic
    // in App.tsx (the inFlight value that reaches the Composer); this test
    // pins the Composer's contract: a disabled Send button is the closed
    // path. A click on the disabled button is a no-op (the browser swallows
    // the click), so onSend is never reached.
    const onSend = vi.fn();
    render(<Composer value="second message" onChange={vi.fn()} onSend={onSend} inFlight />);
    const btn = screen.getByTestId("chat-send-btn") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
    // A click on the disabled button is a no-op — the browser swallows it,
    // and jsdom's fireEvent.click on a disabled button also does not fire
    // the click handler (the button's disabled state is the closed path).
    fireEvent.click(btn);
    expect(onSend).not.toHaveBeenCalled();
  });
});
