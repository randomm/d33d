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

  it("when the export is disabled, the survived line omits the 'still exportable' claim (issue #352, operator decision 4)", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.empty_model,
      detail: "empty_model",
      retryable: true,
      reason: "empty_model",
    };
    render(
      <FailureTurn
        error={error}
        keptVersion="v4"
        exportable={false}
        inFlight={false}
        onAction={vi.fn()}
      />,
    );
    // The survivor is named, but no exportability claim — the export
    // button is disabled (unsettled units / pass in flight), so the claim
    // would contradict it.
    expect(screen.getByTestId("failure-turn-survived").textContent).toBe(
      copy.failure.survivedNoExport("v4"),
    );
    expect(screen.getByTestId("failure-turn-survived").textContent).not.toContain(
      "exportable",
    );
  });

  it("the survived line keeps the export claim by default (exportable absent) and when exportable is true", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.empty_model,
      detail: "empty_model",
      retryable: true,
      reason: "empty_model",
    };
    render(
      <FailureTurn error={error} keptVersion="v4" exportable inFlight={false} onAction={vi.fn()} />,
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

  it("a bbox_out_of_tolerance frame with no gate7 string and no envelope renders the generic card (issue #367, operator decision 2b)", () => {
    // The reason-only shape: detail is the plain reason code (no gate7/
    // envelope string), and the envelope field is absent. Under the new
    // card-selection rule (isEnvelope = error.envelope !== undefined),
    // this is NOT the bed card — it's the generic failure card with the
    // existing reason sentence and no rows (rule 2b).
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    // The headline is the generic reason sentence, not the bed headline.
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.bbox_out_of_tolerance,
    );
    // No bed card artifacts: no bars, no size rows, no bed actions.
    expect(container.querySelector("[data-testid='failure-turn-bars']")).toBeNull();
    expect(container.querySelector("[data-testid='failure-turn-size-rows']")).toBeNull();
    expect(screen.queryByTestId("failure-action-split")).toBeNull();
    expect(screen.queryByTestId("failure-action-scale")).toBeNull();
    expect(screen.queryByTestId("failure-action-bigger")).toBeNull();
    // The generic retry action is offered (retryable).
    expect(screen.getByTestId("failure-action-retry")).toBeTruthy();
  });

  it("the envelope actions prefill the composer (part 3)", () => {
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
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

  // ── Issue #367: the size-mismatch card ──────────────────────────────

  it("a stated-size frame (no gate7 string, carried+measured axes) renders the size card with asked → made rows and NO bed artifacts (issue #367)", () => {
    // The QA 60mm-tray/66mm-made shape: carried_axes {W: 60},
    // measured_axes {W: 66, D: 42, H: 12}. No gate7/envelope string
    // in the message — this is the stated-size gate, not the bed gate.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
      measuredAxes: { W: 66, D: 42, H: 12 },
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    // The headline is the reason sentence, not the bed headline.
    expect(screen.getByTestId("failure-turn-sentence").textContent).toBe(
      copy.failure.reasons.bbox_out_of_tolerance,
    );
    // The size card rows render: W (asked→made), D (made only), H (made only).
    const rows = container.querySelectorAll("[data-testid^='failure-turn-size-row-']");
    expect(rows.length).toBe(3);
    // W: asked 60 → made 66
    expect((container.querySelector("[data-testid='failure-turn-size-row-W']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.row("width", 60, 66),
    );
    // D: made only (no asked value for D)
    expect((container.querySelector("[data-testid='failure-turn-size-row-D']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.madeOnly("depth", 42),
    );
    // H: made only (no asked value for H)
    expect((container.querySelector("[data-testid='failure-turn-size-row-H']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.madeOnly("height", 12),
    );
    // NO bed card artifacts: no bars, no bed actions.
    expect(container.querySelector("[data-testid='failure-turn-bars']")).toBeNull();
    expect(screen.queryByTestId("failure-action-split")).toBeNull();
    expect(screen.queryByTestId("failure-action-scale")).toBeNull();
    expect(screen.queryByTestId("failure-action-bigger")).toBeNull();
  });

  it("a stated-size frame with carried_axes but no measured_axes shows the not-established phrase rows (issue #367)", () => {
    // The user asked for W=60 but no measurement was made (bbox absent
    // or pre-flight). The row shows the not-established phrase, never a
    // number.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    const wRow = container.querySelector("[data-testid='failure-turn-size-row-W']");
    expect(wRow).not.toBeNull();
    expect((wRow as HTMLElement).textContent).toBe(
      copy.failure.envelope.axisNotMeasured("width"),
    );
    // No made-only rows for D/H (no data at all).
    expect(container.querySelector("[data-testid='failure-turn-size-row-D']")).toBeNull();
    expect(container.querySelector("[data-testid='failure-turn-size-row-H']")).toBeNull();
  });

  it("a measured_axes-only frame (no carried_axes) renders made-only rows with no asked label (issue #367)", () => {
    // Defensive coverage for a frame carrying measured_axes WITHOUT
    // carried_axes. Note: the real backend does NOT emit this exact
    // shape for import projects — `_measured_axes` omits the field
    // whenever the confirmed set is empty (issue #332), so import
    // projects render the generic card. This pins the made-only row
    // shape any future frame shape could reach.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      measuredAxes: { W: 45, D: 30, H: 15 },
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    expect((container.querySelector("[data-testid='failure-turn-size-row-W']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.madeOnly("width", 45),
    );
    expect((container.querySelector("[data-testid='failure-turn-size-row-D']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.madeOnly("depth", 30),
    );
    expect((container.querySelector("[data-testid='failure-turn-size-row-H']") as HTMLElement).textContent).toBe(
      copy.failure.sizeMismatch.madeOnly("height", 15),
    );
  });

  it("the lip question renders as a heading plus TWO buttons for the first axis with both asked and made (issue #367, #398 operator decisions 3+4)", () => {
    // W: asked 60, made 66 — the first axis with both values, beyond the
    // gate tolerance. The question text is the HEADING (not a button);
    // the two buttons prefill the composer with the answer each
    // represents (the part's own width, or the overall width including
    // the lip). The old single-button which-measurement control is gone.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
      measuredAxes: { W: 66 },
    };
    const onAction = vi.fn();
    render(<FailureTurn error={error} inFlight={false} onAction={onAction} />);
    // The heading carries the question, with the per-axis adjective.
    const heading = screen.getByTestId("failure-question-which-measurement");
    expect(heading.textContent).toBe(
      copy.failure.sizeMismatch.whichMeasurement(60, "wide"),
    );
    expect(heading.textContent).toContain("how wide the part itself is");
    expect(heading.textContent).not.toContain("width");
    // The old single button is gone.
    expect(screen.queryByTestId("failure-action-which-measurement")).toBeNull();
    // Two buttons, each prefilling its own answer.
    const partBtn = screen.getByTestId("failure-action-lip-part-itself");
    const overallBtn = screen.getByTestId("failure-action-lip-overall");
    const partText = copy.failure.sizeMismatch.lipPartItself(60, "width");
    const overallText = copy.failure.sizeMismatch.lipOverallIncludingLip(60, "width");
    expect(partBtn.textContent).toBe(partText);
    expect(overallBtn.textContent).toBe(overallText);
    fireEvent.click(partBtn);
    expect(onAction).toHaveBeenCalledWith(partText);
    fireEvent.click(overallBtn);
    expect(onAction).toHaveBeenCalledWith(overallText);
    // No bed actions.
    expect(screen.queryByTestId("failure-action-split")).toBeNull();
  });

  it("no follow-up when only made is present (no asked value to compare) (issue #367)", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      measuredAxes: { W: 66 },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(screen.queryByTestId("failure-question-which-measurement")).toBeNull();
    expect(screen.queryByTestId("failure-action-lip-part-itself")).toBeNull();
    expect(screen.queryByTestId("failure-action-lip-overall")).toBeNull();
  });

  it("no follow-up when the first with-both axis is within gate tolerance (issue #367, operator decision 3)", () => {
    // OD3: the follow-up fires only for the first W/D/H axis that has BOTH
    // an asked and a made value AND differs beyond the gate tolerance
    // max(1%, 0.5 mm). Here W is within tolerance (60 asked / 60.5 made,
    // diff 0.5 <= max(0.6, 0.5)=0.6) while D diverges (40 asked / 44 made,
    // diff 4 > max(0.4, 0.5)=0.5). The follow-up must name D, NOT W.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60, D: 40 },
      measuredAxes: { W: 60.5, D: 44 },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    // W (diff 0.5) is within tolerance -> skipped; D (diff 4) is beyond.
    // The heading names the DEEP axis with its adjective ("deep", not the
    // noun "depth").
    const heading = screen.getByTestId("failure-question-which-measurement");
    expect(heading.textContent).toBe(
      copy.failure.sizeMismatch.whichMeasurement(40, "deep"),
    );
    expect(heading.textContent).toContain("how deep the part itself is");
    expect(heading.textContent).not.toContain("depth");
  });
  it("no follow-up when every with-both axis is within gate tolerance (issue #367, operator decision 3)", () => {
    // OD3: "if no axis qualifies, no follow-up." W is within tolerance
    // (diff 0.5 <= max(0.6,0.5)); D has a made value but no asked value, so
    // it can't qualify. No axis has both values beyond tolerance -> no
    // follow-up is offered.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
      measuredAxes: { W: 60.5, D: 44 },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    expect(screen.queryByTestId("failure-question-which-measurement")).toBeNull();
    expect(screen.queryByTestId("failure-action-lip-part-itself")).toBeNull();
    expect(screen.queryByTestId("failure-action-lip-overall")).toBeNull();
  });

  it("the size card uses --color-blocked, not #FF3300 (issue #367)", () => {
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
      measuredAxes: { W: 66 },
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    const row = container.querySelector("[data-testid='failure-turn-size-row-W']");
    expect(row).not.toBeNull();
    // The failing row uses the blocked colour class (var(--color-blocked)
    // = #D2A63C); the CSS handles the colour itself.
    expect((row as HTMLElement).className).toContain("failure-turn-size-row");
    // #FF3300 must never appear in the size card's DOM.
    expect(container.innerHTML).not.toContain("#FF3300");
    expect(container.innerHTML).not.toContain("#ff3300");
  });

  it("only the failing axis row gets the --failing modifier; passing and made-only rows stay neutral (issue #398, operator decision (c))", () => {
    // W: asked 60, made 66 — beyond tolerance (diff 6 > max(0.6, 0.5)) →
    // FAILING. D: asked 40, made 40.3 — within tolerance (diff 0.3 <
    // max(0.4, 0.5)) → passing, neutral. H: made only → neutral.
    const error: DisplayError = {
      message: copy.failure.reasons.bbox_out_of_tolerance,
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60, D: 40 },
      measuredAxes: { W: 66, D: 40.3, H: 12 },
    };
    const { container } = render(
      <FailureTurn error={error} inFlight={false} onAction={vi.fn()} />,
    );
    const wRow = container.querySelector("[data-testid='failure-turn-size-row-W']") as HTMLElement;
    const dRow = container.querySelector("[data-testid='failure-turn-size-row-D']") as HTMLElement;
    const hRow = container.querySelector("[data-testid='failure-turn-size-row-H']") as HTMLElement;
    // Only W is failing — it carries the modifier class; D (passing) and
    // H (made-only) do not.
    expect(wRow.className).toContain("failure-turn-size-row--failing");
    expect(dRow.className).not.toContain("failure-turn-size-row--failing");
    expect(hRow.className).not.toContain("failure-turn-size-row--failing");
    expect(dRow.className).toContain("failure-turn-size-row");
    // Exactly one failing row in the card.
    expect(container.querySelectorAll(".failure-turn-size-row--failing").length).toBe(1);
    // No marker hex anywhere on the card.
    expect(container.innerHTML).not.toContain("#FF3300");
  });

  it("the bboxCarried headline drops 'earlier' and uses the per-axis adjective on the card (issue #398)", () => {
    // The card's headline is the carried-axis sentence (from the mapped
    // error) — it says "you asked for", never "you set earlier", and the
    // "how …" slot takes the adjective.
    const error: DisplayError = {
      message: copy.failure.bboxCarried("width", 60),
      detail: "bbox_out_of_tolerance",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      carriedAxes: { W: 60 },
      measuredAxes: { W: 66 },
    };
    render(<FailureTurn error={error} inFlight={false} onAction={vi.fn()} />);
    const sentence = screen.getByTestId("failure-turn-sentence");
    expect(sentence.textContent).toContain("you asked for");
    expect(sentence.textContent).not.toContain("earlier");
    expect(sentence.textContent).toContain("how wide it should be");
    expect(sentence.textContent).not.toContain("how width");
  });

  it("a gate7-string frame still shows the bed card (issue #367 regression guard)", () => {
    // A genuine envelope-gate failure (a 664 mm model on a 320 mm bed)
    // carries the gate7/envelope string in the message — displayDesignLoopError
    // attaches `envelope`, so the bed card renders (not the size card).
    const error: DisplayError = {
      message: copy.failure.envelope.headline,
      detail: "bbox_out_of_tolerance: gate7/envelope: dimension 0 (664.0mm) exceeds envelope 320.0mm",
      retryable: true,
      reason: "bbox_out_of_tolerance",
      envelope: { measured: 664, limit: 320, axis: 0 },
      carriedAxes: { W: 664 },
      measuredAxes: { W: 664 },
    };
    const { container } = render(
      <FailureTurn error={error} envelope={ENVELOPE} inFlight={false} onAction={vi.fn()} />,
    );
    // Bed card artifacts present.
    expect(container.querySelector("[data-testid='failure-turn-bars']")).not.toBeNull();
    expect(screen.getByTestId("failure-action-split")).toBeTruthy();
    expect(screen.getByTestId("failure-action-scale")).toBeTruthy();
    expect(screen.getByTestId("failure-action-bigger")).toBeTruthy();
    // Size card rows absent.
    expect(container.querySelector("[data-testid='failure-turn-size-rows']")).toBeNull();
  });
});
