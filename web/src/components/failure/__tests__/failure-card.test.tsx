/**
 * FailureCard tests — the app-level error card in the conversation.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { FailureCard } from "../FailureCard";
import type { DisplayError } from "../../../lib/errorMapping";

const baseError: DisplayError = {
  message: "Something went wrong",
  detail: "raw code: error_class_not_ok",
  retryable: true,
};

describe("FailureCard", () => {
  it("renders the error message", () => {
    render(<FailureCard error={baseError} kind="stream" inFlight={false} onRetry={vi.fn()} />);
    expect(screen.getByTestId("app-error").textContent).toContain("Something went wrong");
  });

  it("shows the detail block when detail is present", () => {
    render(<FailureCard error={baseError} kind="stream" inFlight={false} onRetry={vi.fn()} />);
    expect(screen.getByTestId("app-error-detail").textContent).toContain("error_class_not_ok");
  });

  it("hides the detail block when detail is absent", () => {
    render(
      <FailureCard
        error={{ message: "No detail here", detail: undefined, retryable: false }}
        kind="stream"
        inFlight={false}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("app-error-detail")).toBeNull();
  });

  it("shows the Retry button for a retryable stream error", () => {
    const onRetry = vi.fn();
    render(<FailureCard error={baseError} kind="stream" inFlight={false} onRetry={onRetry} />);
    const btn = screen.getByTestId("app-error-retry");
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(onRetry).toHaveBeenCalledTimes(1);
  });

  it("does NOT show the Retry button for a non-retryable error", () => {
    render(
      <FailureCard
        error={{ ...baseError, retryable: false }}
        kind="stream"
        inFlight={false}
        onRetry={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("app-error-retry")).toBeNull();
  });

  it("does NOT show the Retry button for a non-stream error kind", () => {
    render(<FailureCard error={baseError} kind="other" inFlight={false} onRetry={vi.fn()} />);
    expect(screen.queryByTestId("app-error-retry")).toBeNull();
  });

  it("disables the Retry button while in flight", () => {
    render(<FailureCard error={baseError} kind="stream" inFlight onRetry={vi.fn()} />);
    expect((screen.getByTestId("app-error-retry") as HTMLButtonElement).disabled).toBe(true);
  });
});
