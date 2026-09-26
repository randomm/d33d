/**
 * FailureTurn tests — the failure as a four-part turn in the conversation
 * (issue #124, W12).
 *
 * Parts, always in this order: (1) the sentence, (2) the number beside
 * the limit (envelope gate only, from the parsed gate string + the API
 * envelope), (3) the concrete actions, (4) the raw reason, collapsed.
 * Every failure renders identically, however many times it repeats — no
 * attempt counting, no goal state.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { FailureTurn } from "../FailureTurn";
import type { DisplayError } from "../../../lib/errorMapping";
import { copy } from "../../../copy";

const ENVELOPE = { x: 320, y: 320, z: 300 };

describe("FailureTurn", () => {
  it("renders part 1 — the plain-language sentence mapped from the closed enum", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.timeout,
      detail: "timeout",
      retryable: true,
      reason: "timeout",
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.timeout,
    );
  });

  it("renders all four parts in order: sentence, bars, survived, actions, raw", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 380, limit: 320, axis: 0 },
    };
    const { container } = render(
      <FailureTurn
        error={error}
        envelope={ENVELOPE}
        keptVersion="v4"
        inFlight={false}
        onAction={vi.fn()}
      />,
    );
    const parts = [
      container.querySelector("[data-testid='failure-turn-sentence']"),
      container.querySelector("[data-testid='failure-turn-bars']"),
      container.querySelector("[data-testid='failure-turn-survived']"),
      container.querySelector("[data-testid='failure-turn-actions']"),
      container.querySelector("[data-testid='failure-turn-raw']"),
    ];
    for (const p of parts) {
      expect(p, "a failure-turn part is missing").not.toBeNull();
    }
    // The DOM order is the contract (fixed by the W12 spec).
    const all = Array.from(
      container.querySelectorAll("[data-testid^='failure-turn-']"),
    ).map((el) => (el as HTMLElement).getAttribute("data-testid"));
    const firstIdx = (t: string) => all.indexOf(t);
    expect(firstIdx("failure-turn-sentence")).toBeLessThan(firstIdx("failure-turn-bars"));
    expect(firstIdx("failure-turn-bars")).toBeLessThan(firstIdx("failure-turn-survived"));
    expect(firstIdx("failure-turn-survived")).toBeLessThan(firstIdx("failure-turn-actions"));
    expect(firstIdx("failure-turn-actions")).toBeLessThan(firstIdx("failure-turn-raw"));
  });

  it("renders the measured value beside the limit for the envelope gate (part 2)", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 380, limit: 320, axis: 0 },
    };
    render(<FailureTurn error={error} envelope={ENVELOPE} inFlight={false} onAction={vi.fn()} />);
    // The failing axis's row: measured / limit, in the blocked colour.
    const xAxis = screen.getByTestId("failure-turn-bar-x");
    expect(xAxis.textContent).toContain("380.0");
    expect(xAxis.textContent).toContain("320.0");
    expect(xAxis.className).toContain("failure-turn-bar--failing");
    // The bar fill overruns the track (380/320 > 100%).
    const fill = xAxis.querySelector(".failure-turn-bar-fill") as HTMLElement;
    expect(fill.style.width).toBe("100%");
  });

  it("renders a 2mm overshoot as an overshoot, and a fitting axis as fits (one component at every severity)", () => {
    const over: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 2 (302.0mm) exceeds envelope 300.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 302, limit: 300, axis: 2 },
    };
    const { container } = render(
      <FailureTurn error={over} envelope={ENVELOPE} inFlight={false} onAction={vi.fn()} />,
    );
    const zAxis = container.querySelector("[data-testid='failure-turn-bar-z']") as HTMLElement;    expect(zAxis.className).toContain("failure-turn-bar--failing");
    expect(zAxis.textContent).toContain("302.0");
    // And an axis with no measurement never renders a number — it is the
    // not-established phrase, not an invented value.
    const xRow = container.querySelector("[data-testid='failure-turn-bar-x']") as HTMLElement;
    expect(xRow.textContent).toContain(copy.failure.envelope.axisNotMeasured("x"));
    expect(xRow.textContent).not.toMatch(/\d/);
  });

  it("renders one line per mismatch in the mono face under the headline (issue #276)", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.axis_params_mismatch,
      detail: "axis_params_mismatch",
      retryable: true,
      reason: "axis_params_mismatch",
      mismatches: [
        { label: "Tray height", model: 20, measured: 102, axis: "H" },
        { label: "Width", model: 60, measured: 64, axis: "W" },
      ],
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    // The structured mismatches render as one line per entry, formatted by
    // the SPA's own copy helper (the server never pre-formats them).
    const list = screen.getByTestId("failure-turn-mismatches");
    expect(list.textContent).toBe(
      [
        copy.failure.axisMismatchLine("Tray height", 20, 102),
        copy.failure.axisMismatchLine("Width", 60, 64),
      ].join(""),
    );
    // Each line renders the copy helper's output (the numbers are
    // mm-formatted by the SPA, never the server's raw string — the
    // "102" and "20" appear mm-formatted beside the label).
    const lines = container.querySelectorAll(".failure-turn-mismatch-line");
    expect(lines.length).toBe(2);
    expect((lines[0] as HTMLElement).textContent).toBe(
      copy.failure.axisMismatchLine("Tray height", 20, 102),
    );
    expect((lines[1] as HTMLElement).textContent).toBe(
      copy.failure.axisMismatchLine("Width", 60, 64),
    );
    // The headline stays number-free — the numbers live only in the
    // mismatch detail.
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.axis_params_mismatch,
    );
    expect(screen.getByTestId("failure-turn-sentence").textContent).not.toMatch(
      /\d/,
    );
  });

  it("no mismatch detail renders when the frame carries no mismatches", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.axis_params_mismatch,
      detail: "axis_params_mismatch",
      retryable: true,
      reason: "axis_params_mismatch",
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    expect(container.querySelector("[data-testid='failure-turn-mismatches']")).toBeNull();
  });

  it("the surviving version is always named", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.empty_model,
      detail: "empty_model",
      retryable: true,
      reason: "empty_model",
    };
    render(
      <FailureTurn error={error} keptVersion="v4" inFlight={false} onAction={vi.fn()} />,
    );
    expect(screen.getByTestId("failure-turn-survived").textContent).toBe(
      copy.failure.survived("v4"),
    );
  });

  it("renders the raw reason collapsed (part 4)", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.artifact_error,
      detail: "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "artifact_error",
    };
    render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    const details = screen.getByTestId("failure-turn-raw");
    expect(details.tagName).toBe("DETAILS");
    // Collapsed by default: the <details> is not open.
    expect((details as HTMLDetailsElement).open).toBe(false);
    // The summary names the disclosure; the raw string is inside.
    expect(details.querySelector("summary")?.textContent).toBe(
      copy.failure.rawDisclosure,
    );
    expect(screen.getByTestId("failure-turn-raw-code").textContent).toBe(
      "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
    );
    // Expanding shows it.
    fireEvent.click(details.querySelector("summary") as HTMLElement);
    expect((details as HTMLDetailsElement).open).toBe(true);
  });

  it("the envelope actions prefill the composer (part 3)", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 380, limit: 320, axis: 0 },
    };
    const onAction = vi.fn();
    render(
      <FailureTurn error={error} envelope={ENVELOPE} inFlight={false} onAction={onAction} />,
    );
    fireEvent.click(screen.getByTestId("failure-action-split"));
    expect(onAction).toHaveBeenCalledWith(copy.failure.envelope.actions.split);
    fireEvent.click(screen.getByTestId("failure-action-scale"));
    expect(onAction).toHaveBeenCalledWith(copy.failure.envelope.actions.scale);
    fireEvent.click(screen.getByTestId("failure-action-bigger"));
    expect(onAction).toHaveBeenCalledWith(copy.failure.envelope.actions.biggerPrinter);
  });

  it("a non-envelope failure offers the generic retry action", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.timeout,
      detail: "timeout",
      retryable: true,
      reason: "timeout",
    };
    const onAction = vi.fn();
    render(<FailureTurn error={error} inFlight={false} onAction={onAction} />);
    expect(screen.queryByTestId("failure-action-split")).toBeNull();
    fireEvent.click(screen.getByTestId("failure-action-retry"));
    expect(onAction).toHaveBeenCalledWith(copy.failure.retryAction);
  });

  it("actions are disabled while a loop is in flight", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.timeout,
      detail: "timeout",
      retryable: true,
      reason: "timeout",
    };
    render(<FailureTurn error={error} inFlight onAction={vi.fn()} />);
    expect((screen.getByTestId("failure-action-retry") as HTMLButtonElement).disabled).toBe(true);
  });

  it("no bars render when the API envelope is not available (no number invented)", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 380, limit: 320, axis: 0 },
    };
    const { container } = render(
      <FailureTurn error={error} envelope={null} inFlight={false} onAction={vi.fn()} />,
    );
    expect(container.querySelector("[data-testid='failure-turn-bars']")).toBeNull();
  });

  it("repeats render identically — a second failure of the same shape renders the same turn", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 380, limit: 320, axis: 0 },
    };
    const first = render(<FailureTurn error={error} envelope={ENVELOPE} inFlight={false} onAction={vi.fn()} />);
    const second = render(<FailureTurn error={error} envelope={ENVELOPE} inFlight={false} onAction={vi.fn()} />);
    expect(first.container.innerHTML).toBe(second.container.innerHTML);
    // And there is no attempt counting anywhere in the rendered turn.
    expect(first.container.textContent).not.toMatch(/2nd|second|again.*twice|twice/i);
  });
});
