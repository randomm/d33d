/**
 * errorMapping — totality and the envelope gate's measured number.
 *
 * The map must stay TOTAL: every GATE_REASON_BITS value and every
 * render-worker ErrorClass value maps to copy (part 1) — a class with no
 * sentence renders nothing, the worst possible failure surface. The
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

/** The four GATE_REASON_BITS (d33d/design_loop.py, bit order). */
const GATE_REASON_BITS = [
  "error_class_not_ok",
  "views_blank_or_missing",
  "bbox_out_of_tolerance",
  "stated_dims_not_named_parameters",
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
const LOOP_FAILURE_REASONS = ["design_loop_timed_out"];

const CLOSED_SET = [...GATE_REASON_BITS, ...ERROR_CLASSES, ...LOOP_FAILURE_REASONS];

describe("errorMapping", () => {
  it("every GATE_REASON_BITS value and every ErrorClass value maps to copy (totality)", () => {
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
      expect(display.message).toBe(
        (copy.failure.reasons as Record<string, string>)[reason],
      );
      // The raw reason is present for part 4 (collapsed).
      expect(display.detail).toBe(reason);
    }
  });

  it("the closed set is exactly the four bits plus the seven classes plus the loop timeout", () => {
    expect([...CLOSED_SET].sort()).toEqual([...FAILURE_REASONS].sort());
  });

  it("an unknown reason code maps to the generic sentence, raw code in detail", () => {
    const display = displayDesignLoopError({
      message: "Design loop exhausted: totally_unknown",
      reason: "totally_unknown",
    });
    expect(display.message).not.toBe("totally_unknown");
    expect(display.message).toContain("The design could not be generated");
    expect(display.detail).toBe("totally_unknown");
    expect(display.retryable).toBe(true);
  });

  it("a missing reason maps to the generic infra sentence with the raw message", () => {
    const display = displayDesignLoopError({ message: "design loop infra failure: boom" });
    expect(display.message).toContain("Something went wrong");
    expect(display.detail).toBe("design loop infra failure: boom");
    expect(display.retryable).toBe(true);
    expect(display.reason).toBeUndefined();
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
    expect(display.message).toBe(copy.failure.bboxCarried("depth", 12.0));
    expect(display.message).toContain("12.0\u202Fmm");
    // The raw reason is still present for part 4 (collapsed).
    expect(display.detail).toBe("bbox_out_of_tolerance");
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
});
