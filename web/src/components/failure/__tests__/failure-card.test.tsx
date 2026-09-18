/**
 * FailureCard tests — the app-level error card in the conversation.
 *
 * All six live callers (project-create, version-restore, version-pin,
 * no-project, export-mark, photo-upload) pass a plain card — stream
 * failures render as a conversation turn (FailureTurn, issue #124), so
 * the tests for the dead stream-only Retry branch were removed (issue
 * #175).
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { FailureCard } from "../FailureCard";
import type { DisplayError } from "../../../lib/errorMapping";

const baseError: DisplayError = {
  message: "Something went wrong",
  detail: "raw code: error_class_not_ok",
  retryable: true,
};

describe("FailureCard", () => {
  it("renders the error message", () => {
    render(<FailureCard error={baseError} />);
    expect(screen.getByTestId("app-error").textContent).toContain("Something went wrong");
  });

  it("shows the detail block when detail is present", () => {
    render(<FailureCard error={baseError} />);
    expect(screen.getByTestId("app-error-detail").textContent).toContain("error_class_not_ok");
  });

  it("hides the detail block when detail is absent", () => {
    render(
      <FailureCard error={{ message: "No detail here", detail: undefined, retryable: false }} />
    );
    expect(screen.queryByTestId("app-error-detail")).toBeNull();
  });
});
