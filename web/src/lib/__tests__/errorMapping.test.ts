/**
 * errorMapping — totality and the envelope gate's measured number.
 *
 * The map must stay TOTAL: every GATE_REASON_BITS value (five), every
 * render-worker ErrorClass value, and the loop-level pre-flight reason
 * (`renderer_unavailable`, issue #277) maps to copy (part 1) — a reason
 * with no sentence renders nothing, the worst possible failure surface. The
 * envelope gate's part 2 (the measured value beside the limit) is parsed
 * from the raw gate string produced by d33d/print_validation.py
 * (_GATE7_ENVELOPE_PREFIX) — the actual format, never a hand-typed
 * variant.
 */

import { describe, it, expect } from "vitest";
import {
  FAILURE_REASONS,
  displayDesignLoopError,
  parseEnvelopeGateDetail,
} from "../errorMapping";
import { copy } from "../../copy";

/** The five GATE_REASON_BITS (d33d/design_loop.py, bit order). */
const GATE_REASON_BITS = [
  "error_class_not_ok",
  "views_blank_or_missing",
  "bbox_out_of_tolerance",
  "stated_dims_not_named_parameters",
  "axis_params_mismatch",
];

/** The seven render-worker ErrorClass values (d33d/render_worker.py). */
const ERROR_CLASSES = [
  "ok",
  "syntax_error",
  "empty_model",
  "artifact_error",
  "timeout",
  "oom",
  "container_error",
];

/** The design-loop-level timeout reason (d33d/design_loop_events.py, issue
 *  #221) — distinct from the render-worker "timeout" ErrorClass. */
const LOOP_FAILURE_REASONS = [
  "design_loop_timed_out",
  // Loop-level pre-flight reasons (d33d/design_loop.py): the renderer
  // reachability check (issue #277) and the model pre-flight (issue #303)
  // failed before any work ran; NOT render-worker ErrorClasses.
  "renderer_unavailable",
  // Loop-level pre-flight reason (d33d/config/preflight.py, issue #303) —
  // the configured LLM model could not be used before the first iteration;
  // NOT a render-worker ErrorClass (nothing ran). Terminal (no retry).
  "model_unconfigured",
  // Loop-level pre-flight reason (d33d/design_loop.py, issue #346) — the
  // renderer pre-flight verified the render-worker image is missing or its
  // build-hash label mismatches, before any work ran. Terminal (no retry).
  "renderer_image_stale",
  // Loop-level unchanged-mesh post-check (d33d/unchanged_mesh_check.py,
  // issue #419) — a v2+ edit whose rendered mesh equals the parent's
  // (geometry fingerprint identical) exhausted without the change
  // taking; the terminal frame carries `reason: "mesh_unchanged"`.
  "mesh_unchanged",
  // Issue #432: the post-check reasons (each has its own copy sentence).
  "through_hole_missing",
  "screw_clearance_wrong",
  "stack_height_mismatch",
];

const CLOSED_SET = [...GATE_REASON_BITS, ...ERROR_CLASSES, ...LOOP_FAILURE_REASONS];

describe("errorMapping", () => {
  it("every GATE_REASON_BITS value (five), every ErrorClass value, and the loop-level pre-flight reason maps to copy (totality)", () => {
    for (const reason of CLOSED_SET) {
      // The exported closed set contains the reason.
      expect(FAILURE_REASONS).toContain(reason);
      // The copy deck has a sentence for it (part 1).
      expect(
        (copy.failure.reasons as Record<string, string>)[reason],
        `copy.failure.reasons must have an entry for ${reason}`,
      ).toBeTruthy();
      // The mapping returns that sentence, not the fallback.
      const display = displayDesignLoopError({
        message: `Design loop exhausted: ${reason}`,
        reason,
      });
      // The headline is the reason-keyed `reasons` entry for every reason
      // (the env-var helper sentence is rendered by the failure turn from
      // the frame's `env_var`, never folded into the mapped headline).
      expect(display.message).toBe(
        (copy.failure.reasons as Record<string, string>)[reason],
      );
      // The raw reason is present for part 4 (collapsed).
      expect(display.detail).toBe(reason);
    }
  });

  it("the closed set is exactly the five bits plus the seven classes plus the loop-level reasons", () => {
    expect([...CLOSED_SET].sort()).toEqual([...FAILURE_REASONS].sort());
  });

  it("the model_unconfigured frame maps to the pre-flight sentence and is NOT retryable (issue #303)", () => {
    // The terminal frame carries `reason: "model_unconfigured"` plus an
    // `env_var` field (the missing variable's name, null when the model
    // alias/role does not resolve). The headline sentence is the
    // reason-keyed `copy.failure.reasons` entry; the env-var helper
    // sentence (`copy.failure.modelUnconfiguredHelper`) is rendered by the
    // failure turn from the frame's `env_var`, never folded into the
    // mapped sentence. The reason is terminal — no retry button, because
    // retrying changes nothing until the operator sets the variable or
    // fixes the model settings.
    const display = displayDesignLoopError({
      message: "Design loop exhausted: model_unconfigured",
      reason: "model_unconfigured",
    });
    expect(display.message).toBe(copy.failure.reasons.model_unconfigured);
    expect(display.detail).toBe("model_unconfigured");
    expect(display.retryable).toBe(false);
    expect(display.reason).toBe("model_unconfigured");

    // The frame's env_var field is irrelevant to the MAPPING (the sentence
    // is reason-keyed); it only selects the helper sentence downstream.
    const withEnvVar = displayDesignLoopError({
      message: "Design loop exhausted: model_unconfigured",
      reason: "model_unconfigured",
      env_var: "TRAIL_OPENERS_LLM_KEY",
    });
    expect(withEnvVar.message).toBe(copy.failure.reasons.model_unconfigured);
    expect(withEnvVar.retryable).toBe(false);
  });

  it("the model_unconfigured helper sentences name the env var, or the settings (issue #303)", () => {
    // The frame's `env_var` field selects the helper: a named variable gets
    // the set-the-variable sentence (the name in the mono face, rendered
    // by the failure turn); an unresolved alias/role (env_var null) gets
    // the model-settings sentence. The two are distinct honest statements.
    const withVar = copy.failure.modelUnconfiguredHelper("TRAIL_OPENERS_LLM_KEY");
    expect(withVar).toBe("Set TRAIL_OPENERS_LLM_KEY where the server runs, then restart it.");
    expect(copy.failure.modelUnresolved).toBe("Check the model settings.");
    expect(withVar).not.toBe(copy.failure.modelUnresolved);
    // The helper is a SEPARATE key from the closed `reasons` map — the
    // headline and the helper are two sentences the turn renders in order,
    // and the headline stays the reason-keyed map entry.
    expect(copy.failure.reasons.model_unconfigured).not.toContain("Set ");
  });

  it("the renderer_unavailable frame maps to the pre-flight sentence (issue #277)", () => {
    // The pre-flight failure travels the standard SSE error-frame shape:
    // a `reason` field the SPA maps like any other closed-set reason.
    const display = displayDesignLoopError({
      message: "Design loop exhausted: renderer_unavailable",
      reason: "renderer_unavailable",
    });
    expect(display.message).toBe(copy.failure.reasons.renderer_unavailable);
    expect(display.message).toBe(
      "The renderer isn't running, so nothing was designed. Start Docker and try again.",
    );
    expect(display.detail).toBe("renderer_unavailable");
    expect(display.retryable).toBe(true);
  });

  it("the renderer_image_stale frame carries the STRUCTURED rendererDetail and is NOT retryable (issue #346)", () => {
    // The terminal frame carries `reason: "renderer_image_stale"` plus an
    // `renderer_detail` field (omit-not-null: the verified reason — image
    // missing or label mismatch — and the exact rebuild command). The
    // headline is the reason-keyed `copy.failure.reasons` entry; the
    // structured `rendererDetail` is copied onto the mapped error (the
    // failure turn renders the reason line + the mono rebuild command
    // from it). `detail` stays the plain reason string for the generic
    // disclosure. The reason is terminal — no retry button, because
    // retrying changes nothing until the operator rebuilds the image.
    const detail = {
      reason: "image_missing" as const,
      rebuild_command: "docker build -t d33d/render-worker:local .",
    };
    const display = displayDesignLoopError({
      message: "Design loop exhausted: renderer_image_stale",
      reason: "renderer_image_stale",
      renderer_detail: detail,
    });
    expect(display.message).toBe(copy.failure.reasons.renderer_image_stale);
    expect(display.message).toBe("The renderer needs rebuilding.");
    expect(display.retryable).toBe(false);
    expect(display.reason).toBe("renderer_image_stale");
    // The structured detail rides on the mapped error (the turn renders
    // the reason line + the mono rebuild command from this field).
    expect(display.rendererDetail).toEqual(detail);
    // `detail` stays the plain reason string — the generic disclosure.
    expect(display.detail).toBe("renderer_image_stale");
    // The label_mismatch variant carries both values structurally.
    const mismatchInput = {
      reason: "label_mismatch" as const,
      expected: "abc123",
      actual: "def456",
      rebuild_command: "docker build -t d33d/render-worker:local .",
    };
    const mismatch = displayDesignLoopError({
      message: "Design loop exhausted: renderer_image_stale",
      reason: "renderer_image_stale",
      renderer_detail: mismatchInput,
    });
    expect(mismatch.rendererDetail).toEqual(mismatchInput);
    expect(mismatch.detail).toBe("renderer_image_stale");
    expect(mismatch.retryable).toBe(false);

    // A malformed renderer_detail is DROPPED — the plain reason string
    // disclosure stands, and no half-established detail is carried.
    const malformed = displayDesignLoopError({
      message: "Design loop exhausted: renderer_image_stale",
      reason: "renderer_image_stale",
      renderer_detail: "not an object",
    });
    expect(malformed.rendererDetail).toBeUndefined();
    expect(malformed.detail).toBe("renderer_image_stale");

    // No renderer_detail at all → no structured field, plain reason
    // disclosure, as before.
    const headless = displayDesignLoopError({
      message: "Design loop exhausted: renderer_image_stale",
      reason: "renderer_image_stale",
    });
    expect(headless.rendererDetail).toBeUndefined();
    expect(headless.detail).toBe("renderer_image_stale");
  });

  it("an unknown or missing reason stays retryable (issue #303 pins the inversion)", () => {
    // Every OTHER closed-set reason keeps the retry control — the
    // non-retryable branch is exclusive to `model_unconfigured` and
    // `renderer_image_stale` (issues #303 / #346).
    const generic = displayDesignLoopError({ message: "boom" });
    expect(generic.retryable).toBe(true);
    const unknown = displayDesignLoopError({ reason: "totally_unknown" });
    expect(unknown.retryable).toBe(true);
    const timeout = displayDesignLoopError({ reason: "timeout" });
    expect(timeout.retryable).toBe(true);
  });

  it("the axis_params_mismatch headline carries no numbers (issue #276)", () => {
    // The headline is the reason-code sentence — the per-parameter numbers
    // ride in the failure detail (copy.failure.axisMismatchLine), never in
    // a headline the SPA cannot have established.
    const display = displayDesignLoopError({
      message: "Design loop exhausted: axis_params_mismatch",
      reason: "axis_params_mismatch",
    });
    expect(display.message).toBe(copy.failure.reasons.axis_params_mismatch);
    expect(display.message).not.toMatch(/\d/);
    expect(display.detail).toBe("axis_params_mismatch");
    expect(display.retryable).toBe(true);
  });

  it("the axis_params_mismatch frame carries structured mismatches, one entry per param (issue #276)", () => {
    // The per-param numbers ride in the error frame's `mismatches`
    // (STRUCTURED — the server's own gate evidence: label + the model's
    // declared value + the measured extent). The SPA owns the formatting:
    // each entry renders via copy.failure.axisMismatchLine (the detail is
    // the joined lines). The headline stays number-free.
    const single = displayDesignLoopError({
      message: "Design loop exhausted: axis_params_mismatch",
      reason: "axis_params_mismatch",
      mismatches: [{ label: "Tray height", model: 20, measured: 102, axis: "H" }],
    });
    expect(single.message).toBe(copy.failure.reasons.axis_params_mismatch);
    expect(single.mismatches).toEqual([
      { label: "Tray height", model: 20, measured: 102, axis: "H" },
    ]);
    expect(single.detail).toBe(copy.failure.axisMismatchLine("Tray height", 20, 102));

    const multi = displayDesignLoopError({
      message: "Design loop exhausted: axis_params_mismatch",
      reason: "axis_params_mismatch",
      mismatches: [
        { label: "Tray height", model: 20, measured: 102, axis: "H" },
        { label: "Width", model: 60, measured: 64, axis: "W" },
      ],
    });
    expect(multi.mismatches).toHaveLength(2);
    expect(multi.detail).toBe(
      [
        copy.failure.axisMismatchLine("Tray height", 20, 102),
        copy.failure.axisMismatchLine("Width", 60, 64),
      ].join("\n"),
    );

    // Malformed entries are dropped; no valid entries → the raw reason
    // (an honest headless detail), as before.
    const malformed = displayDesignLoopError({
      message: "Design loop exhausted: axis_params_mismatch",
      reason: "axis_params_mismatch",
      mismatches: ["nope", { label: "H", model: "x", measured: 1 }],
    });
    expect(malformed.mismatches).toBeUndefined();
    expect(malformed.detail).toBe("axis_params_mismatch");

    const headless = displayDesignLoopError({
      message: "Design loop exhausted: axis_params_mismatch",
      reason: "axis_params_mismatch",
    });
    expect(headless.detail).toBe("axis_params_mismatch");
  });

  it("the design_loop_timed_out frame with measured values renders the slow-model copy (issue #417)", () => {
    // The server's measured per-attempt latency and attempt count are on
    // the frame (omit-not-null). When both are present, the headline is
    // the templated "model is slow right now" copy with the measured
    // values filled in — the words "stopped responding" no longer appear.
    // The values are LOOP-SOURCED (the loop's own wall clock): the
    // attempt count is the number of attempts STARTED (including the
    // one killed mid-LLM-call), the latency is the mean of the loop's
    // per-attempt seconds, rounded up to a whole second by the adapter.
    const display = displayDesignLoopError({
      message: "Design loop timed out after 360s",
      reason: "design_loop_timed_out",
      attempt_latency_seconds: 60,
      attempt_count: 2,
    });
    expect(display.message).toBe(copy.failure.slowModelTimeout(60, 2));
    expect(display.message).not.toContain("stopped responding");
    expect(display.message).toContain("about 60s an attempt");
    expect(display.message).toContain("stopped after 2 tries");
    // The raw reason is still present for part 4 (collapsed).
    expect(display.detail).toBe("design_loop_timed_out");
    expect(display.retryable).toBe(true);
  });

  it("the loop-sourced slow-model case (attempt 1 fast, attempt 2 killed) renders the slow-model copy (issue #417)", () => {
    // The PRIMARY slow-model path: the loop's own per-attempt deadline
    // fires on attempt 2 mid-LLM-call. The loop starts 2 attempts and
    // measures both (a killed attempt keeps the budget it burned, a
    // completed one its full duration); the adapter's mean (rounded up
    // to a whole second) and the attempt count ride the terminal frame.
    // The SPA renders the slow-model copy with those values — "about Ns
    // an attempt" (N = the mean) and "after N tries" (N = the count of
    // STARTED attempts, 2, not the count that completed, 1). A sub-second
    // budget rounds up to 1 s — never "about 0s an attempt".
    const display = displayDesignLoopError({
      message: "Design loop timed out after 360s",
      reason: "design_loop_timed_out",
      attempt_latency_seconds: 1,
      attempt_count: 2,
    });
    expect(display.message).toBe(copy.failure.slowModelTimeout(1, 2));
    expect(display.message).not.toContain("stopped responding");
    expect(display.message).toContain("about 1s an attempt");
    expect(display.message).toContain("stopped after 2 tries");
    expect(display.detail).toBe("design_loop_timed_out");
  });

  it("the design_loop_timed_out frame without measured values falls back to the generic reason copy (issue #417)", () => {
    // A stall that never rendered carries no measured values — the
    // headline falls back to the generic `design_loop_timed_out` reason
    // sentence (which still says "ran past its time limit" — the cause
    // is stated, just without a number the SPA has not established).
    const display = displayDesignLoopError({
      message: "Design loop timed out after 360s",
      reason: "design_loop_timed_out",
    });
    expect(display.message).toBe(copy.failure.reasons.design_loop_timed_out);
    expect(display.detail).toBe("design_loop_timed_out");
  });

  it("the design_loop_timed_out copy no longer contains 'stopped responding' when measured values are present (issue #417)", () => {
    // The words "stopped responding" must not appear in the slow-model
    // copy — the cause is now stated as "the model is slow right now".
    const slowCopy = copy.failure.slowModelTimeout(60, 2);
    expect(slowCopy).not.toContain("stopped responding");
    expect(slowCopy).toContain("The model is slow right now");
  });
});

describe("parseEnvelopeGateDetail", () => {
  // The ACTUAL gate string shape from d33d/print_validation.py:
  // f"{_GATE7_ENVELOPE_PREFIX}: dimension {i} ({bbox_mm[i]}mm) exceeds envelope {env[i]}mm"
  // where bbox_mm[i] and env[i] are floats (f-string, no precision
  // specifier) — e.g. 380.0, 320.0.
  const limits: [number, number, number] = [320, 320, 300];

  it("parses the real gate string for a 380mm X-axis overshoot", () => {
    const raw = "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm";
    expect(parseEnvelopeGateDetail(raw, limits)).toEqual({
      measured: 380,
      limit: 320,
      axis: 0,
    });
  });

  it("parses the real gate string for a Y-axis overshoot", () => {
    const raw = "gate7/envelope: dimension 1 (321.0mm) exceeds envelope 320.0mm";
    expect(parseEnvelopeGateDetail(raw, limits)).toEqual({
      measured: 321,
      limit: 320,
      axis: 1,
    });
  });

  it("parses the real gate string for a Z-axis overshoot", () => {
    const raw = "gate7/envelope: dimension 2 (305.5mm) exceeds envelope 300.0mm";
    expect(parseEnvelopeGateDetail(raw, limits)).toEqual({
      measured: 305.5,
      limit: 300,
      axis: 2,
    });
  });

  it("parses integer-formatted values (the f-string renders 380.0, but 380 is the same number)", () => {
    const raw = "gate7/envelope: dimension 0 (380mm) exceeds envelope 320mm";
    expect(parseEnvelopeGateDetail(raw, limits)).toEqual({
      measured: 380,
      limit: 320,
      axis: 0,
    });
  });

  it("returns null for the keep-out branch (not an axis failure)", () => {
    const raw =
      "gate7/keep-out: post-centre position (-9.50, -13.00) enters the keep-out zone";
    expect(parseEnvelopeGateDetail(raw, limits)).toBeNull();
  });

  it("returns null for non-envelope gate strings", () => {
    expect(
      parseEnvelopeGateDetail("gate5/volume: 0 faces", limits),
    ).toBeNull();
    expect(
      parseEnvelopeGateDetail("Design loop exhausted: bbox_out_of_tolerance", limits),
    ).toBeNull();
  });

  it("returns null when the API envelope is not available (no limit established)", () => {
    const raw = "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm";
    expect(parseEnvelopeGateDetail(raw, undefined)).toBeNull();
  });

  it("returns null when the gate's limit disagrees with the API's (no number the SPA has not established)", () => {
    const raw = "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm";
    expect(parseEnvelopeGateDetail(raw, [310, 320, 300])).toBeNull();
  });

  it("the limit rendered is the API's value, not the gate's", () => {
    // The gate and the API agree here — the API's number is what prints.
    const api: [number, number, number] = [320.5, 320, 300];
    const raw = "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.5mm";
    expect(parseEnvelopeGateDetail(raw, api)).toEqual({
      measured: 380,
      limit: 320.5,
      axis: 0,
    });
  });
});

describe("displayDesignLoopError — the envelope gate (part 2)", () => {
  const limits: [number, number, number] = [320, 320, 300];

  it("attaches the measured value beside the limit for a bbox_out_of_tolerance frame", () => {
    // The real frame shape: reason "bbox_out_of_tolerance" is the GATE bit;
    // the raw gate string arrives on `message` ONLY when the backend puts
    // it there — but the current frame carries the reason string as the
    // detail. The measured number is therefore parsed from the detail when
    // it IS the raw gate string, and otherwise the envelope is absent (no
    // number invented).
    const display = displayDesignLoopError(
      {
        message:
          "gate7/envelope: dimension 0 (380.0mm) exceeds envelope 320.0mm",
        reason: "bbox_out_of_tolerance",
      },
      limits,
    );
    expect(display.message).toBe(copy.failure.reasons.bbox_out_of_tolerance);
    // The measured value is parsed from the raw gate string; the limit is
    // the API's per-axis value (never a literal).
    expect(display.envelope).toEqual({ measured: 380, limit: 320, axis: 0 });
    // Part 4's content carries the reason and the checker's actual words.
    expect(display.detail).toContain("bbox_out_of_tolerance");
    expect(display.detail).toContain("380.0mm");
    expect(display.detail).toContain("320.0mm");
  });

  it("no envelope data is invented when the frame carries only the reason (the current shape)", () => {
    // The current terminal frame: message is the loop's result prose and
    // the gate's raw output is not in the frame. No number is rendered —
    // it is not invented.
    const display = displayDesignLoopError(
      {
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
      },
      limits,
    );
    expect(display.envelope).toBeUndefined();
    expect(display.detail).toBe("bbox_out_of_tolerance");
  });

  it("a bbox failure with carried_axes says which held value was enforced (issue #261)", () => {
    // The carried-axis variant: the frame's `carried_axes` carries the
    // axis the gate enforced from the user's EARLIER statement (the
    // carry-forward merge held it) — the failure copy names the held
    // value (formatted by `mm`) instead of the generic "came out a
    // different size" sentence.
    const display = displayDesignLoopError(
      {
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
        carried_axes: { D: 12.0 },
      },
    );
    expect(display.message).toBe(copy.failure.bboxCarried("D", 12.0));
    expect(display.message).toContain("12.0\u202Fmm");
    // The raw reason is still present for part 4 (collapsed).
    expect(display.detail).toBe("bbox_out_of_tolerance");
  });

  it("the carried-axis sentence drops 'earlier' and uses the per-axis adjective (issue #398)", () => {
    // The sentence never says "you set earlier" — the carried axis may have
    // been stated this turn — and the "how …" slot takes the adjective
    // ("deep"), not the noun ("depth").
    const display = displayDesignLoopError({
      message: "Design loop exhausted: bbox_out_of_tolerance",
      reason: "bbox_out_of_tolerance",
      carried_axes: { D: 12.0 },
    });
    expect(display.message).not.toContain("earlier");
    expect(display.message).toContain("you asked for");
    expect(display.message).toContain("how deep it should be");
    expect(display.message).not.toContain("how depth");
  });

  it("the carried-axis sentence uses 'wide' for a width axis (issue #398)", () => {
    const display = displayDesignLoopError({
      message: "Design loop exhausted: bbox_out_of_tolerance",
      reason: "bbox_out_of_tolerance",
      carried_axes: { W: 60 },
    });
    expect(display.message).toContain("how wide it should be");
    expect(display.message).not.toContain("how width");
    expect(display.message).not.toContain("earlier");
  });

  it("a multi-axis carried_axes frame names only the FIRST axis (issue #398)", () => {
    // The gate enforced several carried axes; the sentence names the first
    // one (the largest, the same axis whose value it renders) with its own
    // adjective — never a joined label that falls through to a third
    // axis's word.
    const display = displayDesignLoopError({
      message: "Design loop exhausted: bbox_out_of_tolerance",
      reason: "bbox_out_of_tolerance",
      carried_axes: { W: 60, D: 40 },
    });
    expect(display.message).toContain("width");
    expect(display.message).toContain("how wide it should be");
    expect(display.message).not.toContain("tall");
    expect(display.message).not.toContain("height");
    expect(display.message).not.toContain("and depth");
  });

  it("the generic bbox sentence stands alone when the frame carries no carried_axes", () => {
    // No `carried_axes` on the frame → the reason-code sentence, not the
    // carried variant (a value the SPA has not established is not
    // rendered).
    const display = displayDesignLoopError({
      message: "Design loop exhausted: bbox_out_of_tolerance",
      reason: "bbox_out_of_tolerance",
    });
    expect(display.message).toBe(copy.failure.reasons.bbox_out_of_tolerance);
  });

  it("carried_axes is ignored for non-bbox reasons", () => {
    const display = displayDesignLoopError({
      message: "Design loop exhausted: empty_model",
      reason: "empty_model",
      carried_axes: { H: 12.0 },
    });
    expect(display.message).toBe(copy.failure.reasons.empty_model);
  });

  it("measured_axes is threaded onto the mapped error for a stated-size frame (issue #367)", () => {
    // The new terminal frame shape: the stated-size gate (a 60 mm tray made
    // at 66 mm) emits the ASKED set on carried_axes and the MADE set on
    // measured_axes — the extents the gate actually compared. The mapping
    // threads both through, so the size card can render asked → made.
    const display = displayDesignLoopError(
      {
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
        carried_axes: { W: 60 },
        measured_axes: { W: 66, D: 40, H: 12 },
      },
      limits,
    );
    expect(display.carriedAxes).toEqual({ W: 60 });
    expect(display.measuredAxes).toEqual({ W: 66, D: 40, H: 12 });
    // The bboxCarried sentence (issue #261) still stands alongside the
    // structured data — the mapping changed threading, not the message.
    expect(display.message).toBe(copy.failure.bboxCarried("W", 60));
    // No gate7 string in the message → the bed card's envelope is still
    // absent (the size card's discriminator, downstream).
    expect(display.envelope).toBeUndefined();
    expect(display.detail).toBe("bbox_out_of_tolerance");
  });

  it("measured_axes alone threads (import projects: carried_axes empty → made-only rows)", () => {
    // Import projects carry no carried_axes (no user-stated set), but the
    // gate still MEASURED the part — measured_axes is emitted and threaded
    // on its own; the size card shows made-only rows, no asked label.
    const display = displayDesignLoopError(
      {
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
        measured_axes: { W: 66, D: 40, H: 12 },
      },
      limits,
    );
    expect(display.carriedAxes).toBeUndefined();
    expect(display.measuredAxes).toEqual({ W: 66, D: 40, H: 12 });
    // The headline stays the reason sentence — no carried value to name.
    expect(display.message).toBe(copy.failure.reasons.bbox_out_of_tolerance);
  });

  it("measured_axes is ignored for non-bbox reasons and dropped when malformed", () => {
    const other = displayDesignLoopError({
      message: "Design loop exhausted: empty_model",
      reason: "empty_model",
      measured_axes: { W: 66 },
    });
    expect(other.measuredAxes).toBeUndefined();

    // Malformed shapes are dropped (absent), never rendered half-
    // established: non-objects, empty objects, non-numeric / non-positive
    // / non-finite values.
    for (const bad of ["nope", [], null, {}, { W: 0 }, { W: -1 }, { W: NaN }, { W: Infinity }, { W: "66" }, { X: 1 }]) {
      const display = displayDesignLoopError({
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
        measured_axes: bad,
      });
      expect(display.measuredAxes, `measured_axes=${JSON.stringify(bad)}`).toBeUndefined();
    }
  });

  it("carried_axes is dropped when malformed (never a half-established asked row)", () => {
    for (const bad of ["nope", [], null, {}, { W: 0 }, { W: -1 }, { W: NaN }]) {
      const display = displayDesignLoopError({
        message: "Design loop exhausted: bbox_out_of_tolerance",
        reason: "bbox_out_of_tolerance",
        carried_axes: bad,
      });
      expect(display.carriedAxes, `carried_axes=${JSON.stringify(bad)}`).toBeUndefined();
    }
  });
});
