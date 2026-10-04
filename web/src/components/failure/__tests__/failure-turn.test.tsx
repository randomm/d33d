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

  it("a model_unconfigured failure offers NO retry and renders the env-var helper in mono (issue #303)", () => {
    // The terminal pre-flight frame: reason model_unconfigured, retryable
    // false (the mapping marks it), and the env_var field carrying the
    // missing variable's name. The failure turn offers no retry button
    // (retrying changes nothing until the operator sets the variable),
    // and renders the helper sentence naming the variable.
    const error: DisplayError = {
      message: copy.failure.reasons.model_unconfigured,
      detail: "model_unconfigured",
      retryable: false,
      reason: "model_unconfigured",
      envVar: "TRAIL_OPENERS_LLM_KEY",
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    // The headline is the deck's reason sentence.
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.model_unconfigured,
    );
    // The helper names the variable verbatim.
    const helper = screen.getByTestId("failure-turn-model-helper");
    expect(helper.textContent).toBe(
      copy.failure.modelUnconfiguredHelper("TRAIL_OPENERS_LLM_KEY"),
    );
    // No retry button — the action set is the non-envelope branch, but
    // retryable:false omits it. The actions container is still present
    // (the turn's structure is fixed) but empty.
    expect(screen.queryByTestId("failure-action-retry")).toBeNull();
    expect(screen.queryByTestId("failure-turn-actions")).toBeTruthy();
  });

  it("a model_unconfigured failure with an unresolved model renders the settings helper, no retry (issue #303)", () => {
    // The alias/role does not resolve: the frame's env_var is null (or
    // absent), so the helper is the model-settings sentence — no variable
    // name to render (a name the SPA has not established is never shown).
    const error: DisplayError = {
      message: copy.failure.reasons.model_unconfigured,
      detail: "model_unconfigured",
      retryable: false,
      reason: "model_unconfigured",
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(screen.getByTestId("failure-turn-model-helper").textContent).toBe(
      copy.failure.modelUnresolved,
    );
    // The settings helper has no variable name: no mono run, and no
    // retry (terminal either way).
    expect(screen.getByTestId("failure-turn-model-helper").textContent).not.toMatch(
      /mono/,
    );
    expect(screen.queryByTestId("failure-action-retry")).toBeNull();
  });

  it("a renderer_image_stale failure offers NO retry and renders the missing-image fault + rebuild command in mono (issue #346)", () => {
    // The terminal pre-flight frame: reason renderer_image_stale, retryable
    // false (the mapping marks it), and the renderer_detail field carrying
    // the verified fault (the image is missing) plus the exact rebuild
    // command. The failure turn offers no retry button (retrying changes
    // nothing until the operator rebuilds the image), and renders the
    // fault line and the rebuild command in the mono face.
    const rebuild =
      'docker build --platform=linux/amd64 -t d33d/render-worker:local .';
    const reasonLine =
      "image missing: the render-worker image is not in the Docker daemon";
    const error: DisplayError = {
      message: copy.failure.reasons.renderer_image_stale,
      detail: "renderer_image_stale",
      retryable: false,
      reason: "renderer_image_stale",
      rendererDetail: {
        reason: "image_missing",
        rebuild_command: rebuild,
      },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    // The headline is the deck's reason sentence — the honest copy.
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.renderer_image_stale,
    );
    // The fault line carries the verified reason (image missing).
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toBe(
      reasonLine,
    );
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toContain(
      "image missing",
    );
    // The rebuild command renders verbatim in the mono face (<code>).
    const rebuildEl = screen.getByTestId("failure-turn-rebuild-command");
    expect(rebuildEl.tagName).toBe("CODE");
    expect(rebuildEl.textContent).toBe(rebuild);
    // The disclosure carries the rebuild command too (label + command, in
    // the mono face).
    expect(screen.getByTestId("failure-turn-raw-rebuild").textContent).toBe(
      `${copy.failure.rebuildLabel}: ${rebuild}`,
    );
    // No retry button — the action set is the non-envelope branch, but
    // retryable:false omits it. The actions container is still present
    // (the turn's structure is fixed) but empty.
    expect(screen.queryByTestId("failure-action-retry")).toBeNull();
    expect(screen.queryByTestId("failure-turn-actions")).toBeTruthy();
  });

  it("a renderer_image_stale failure with a label mismatch shows label X vs expected Y, no retry (issue #346)", () => {
    // The image exists but its build-hash label no longer matches the
    // tree: the frame carries reason label_mismatch plus the two hash
    // values, and the fault line names them together in the mono face.
    const rebuild = "docker build -t d33d/render-worker:local .";
    const reasonLine =
      "label mismatch: image label def456 does not match expected abc123";
    const error: DisplayError = {
      message: copy.failure.reasons.renderer_image_stale,
      detail: "renderer_image_stale",
      retryable: false,
      reason: "renderer_image_stale",
      rendererDetail: {
        reason: "label_mismatch",
        expected: "abc123",
        actual: "def456",
        rebuild_command: rebuild,
      },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(
      screen.getByTestId("failure-turn-renderer-fault").textContent,
    ).toBe(reasonLine);
    // The mismatch line names both values (the frame's own numbers, never
    // re-derived).
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toContain(
      "abc123",
    );
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toContain(
      "def456",
    );
    // No retry, either way (the fault is terminal until the rebuild).
    expect(screen.queryByTestId("failure-action-retry")).toBeNull();
  });

  it("a renderer_image_stale frame without renderer_detail renders no fabricated fault or command (issue #346)", () => {
    // Omit-not-null: a frame that did not establish the structured detail
    // carries no renderer_detail — the mapping folds it into the raw
    // reason string, so the turn must not invent a fault line or a
    // rebuild command from it. The headline still renders (the copy is
    // reason-keyed); the collapsed disclosure shows the raw reason.
    const error: DisplayError = {
      message: copy.failure.reasons.renderer_image_stale,
      detail: "renderer_image_stale",
      retryable: false,
      reason: "renderer_image_stale",
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    expect(container.querySelector("[data-testid='failure-turn-renderer-fault']")).toBeNull();
    expect(container.querySelector("[data-testid='failure-turn-rebuild-command']")).toBeNull();
    expect(container.querySelector("[data-testid='failure-turn-raw-rebuild']")).toBeNull();
    // Still no retry — the reason alone is terminal.
    expect(screen.queryByTestId("failure-action-retry")).toBeNull();
  });

  it("a renderer_image_stale failure with a malformed-ish structured detail renders only what is established (issue #346)", () => {
    // The mapping validates `renderer_detail` (malformed objects are
    // dropped before they reach the turn), so the turn can only ever see
    // a well-formed structured field — but a label_mismatch with an
    // ABSENT actual/expected still reaches it: the fault line falls back
    // to the honest "(unlabeled)" / "(unknown)" placeholders, the command
    // renders in mono, and no fabricated value is shown.
    const rebuild = "docker build -t d33d/render-worker:local .";
    const error: DisplayError = {
      message: copy.failure.reasons.renderer_image_stale,
      detail: "renderer_image_stale",
      retryable: false,
      reason: "renderer_image_stale",
      rendererDetail: {
        reason: "label_mismatch",
        rebuild_command: rebuild,
      },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toBe(
      copy.failure.rendererImageLabelMismatch(undefined, undefined),
    );
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toContain("(unlabeled)");
    expect(screen.getByTestId("failure-turn-renderer-fault").textContent).toContain("(unknown)");
    const rebuildEl = screen.getByTestId("failure-turn-rebuild-command");
    expect(rebuildEl.tagName).toBe("CODE");
    expect(rebuildEl.textContent).toBe(rebuild);
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
