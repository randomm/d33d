/**
 * Plain-language mapping of design-loop failure reasons (issue #82, W12).
 *
 * The terminal `error` frame carries a STRUCTURED `reason` (from
 * `d33d/design_loop_events.py`) whose value is closed: the five
 * `GATE_REASON_BITS` from `d33d/design_loop.py` plus the seven render-worker
 * `ErrorClass` values plus the design-loop-level timeout reason
 * (`design_loop_timed_out`, emitted by the server-side loop deadline —
 * distinct from the render-worker `timeout` ErrorClass). `displayDesignLoopError` maps that closed set to the
 * failure turn's part 1 — a sentence a person would say — plus the raw
 * detail for the turn's part 4.
 *
 * The free-text `message` field (e.g. "Design loop exhausted:
 * error_class_not_ok") is never shown as the primary copy when a mapping
 * exists: plain language, no error codes.
 *
 * The map must stay TOTAL: a failure class with no sentence renders
 * nothing, which is the worst possible failure surface. The table-driven
 * test iterates the closed sets below and fails if any member lacks an
 * entry.
 */

import { copy } from "../copy";

/** The five GATE_REASON_BITS (d33d/design_loop.py) + the seven render-worker
 *  ErrorClass values + the design-loop-level timeout reason — the closed set
 *  the terminal error frame's `reason` field can hold.
 *  `copy.failure.reasons` holds the sentences; this is the key set totality
 *  is asserted against. */
export const FAILURE_REASONS: readonly string[] = [
  // GATE_REASON_BITS (d33d/design_loop.py, bit order)
  "error_class_not_ok",
  "views_blank_or_missing",
  "bbox_out_of_tolerance",
  "stated_dims_not_named_parameters",
  "axis_params_mismatch",
  // render-worker ErrorClass values (d33d/render_worker.py)
  "ok",
  "syntax_error",
  "empty_model",
  "artifact_error",
  "timeout",
  "oom",
  "container_error",
  // design-loop-level timeout (d33d/design_loop_events.py, issue #221) —
  // the overall loop's wall-clock deadline fired; distinct from the
  // render-worker "timeout" ErrorClass above.
  "design_loop_timed_out",
];

/** The generic fallback for a reason code outside the closed set. */
const UNKNOWN_REASON_COPY =
  "The design could not be generated. You can retry, or describe the part in more detail.";

const GENERIC_INFRA_COPY =
  "Something went wrong while working on your design. Please try again — if it keeps happening, the details below may help.";

/** The failure turn the conversation renders for a design-loop error.
 *
 * Four parts, always in this order (W12): the sentence (part 1), the
 * measured number beside the limit (part 2 — envelope gate only; the
 * per-axis data the bars render), the raw reason (part 4's content,
 * collapsed), and — in the card — the actions (part 3, which prefill the
 * composer). */
export interface DisplayError {
  /** The primary plain-language sentence (part 1). */
  message: string;
  /** The raw technical detail, shown in a collapsed <details> (part 4). */
  detail?: string;
  /** True when a Retry control is offered (design-loop failures only). */
  retryable: boolean;
  /** The structured reason code, when the frame carried one. */
  reason?: string;
  /** Part 2 — the measured number beside the limit. Present ONLY for the
   *  envelope gate; measured is parsed from the raw gate string (the
   *  frame's only source of the number — never invented). */
  envelope?: {
    /** The measured extent, mm (from the raw gate string). */
    measured: number;
    /** The limit, mm (from the API-reported envelope, by axis). */
    limit: number;
    /** 0 = X, 1 = Y, 2 = Z (the gate's dimension index). */
    axis: 0 | 1 | 2;
  };
}

/** Parse the gate-7 envelope failure string produced by
 *  `d33d/print_validation.py` (`_GATE7_ENVELOPE_PREFIX`):
 *  `gate7/envelope: dimension {i} ({bbox}mm) exceeds envelope {limit}mm`.
 *
 *  Returns the measured extent, the limit and the axis when the raw
 *  string is an envelope gate-7a failure and an axis limit is available;
 *  `null` for any other shape (keep-out branch, non-envelope gate,
 *  missing limit) — a number that is not established is not rendered. */
export function parseEnvelopeGateDetail(
  detail: string,
  limits: [number, number, number] | undefined,
): { measured: number; limit: number; axis: 0 | 1 | 2 } | null {
  const m = /^gate7\/envelope: dimension ([0-2]) \((\d+(?:\.\d+)?)mm\) exceeds envelope (\d+(?:\.\d+)?)mm$/.exec(
    detail,
  );
  if (m === null) return null;
  const axis = Number(m[1]) as 0 | 1 | 2;
  const measured = Number(m[2]);
  const gateLimit = Number(m[3]);
  if (!Number.isFinite(measured) || !Number.isFinite(gateLimit)) return null;
  // The displayed limit is the API's number for the failing axis (the
  // envelope the API reports, never a literal); the gate's own limit must
  // agree with it, otherwise the frame and the API disagree and no
  // number is shown rather than a number the SPA has not established.
  const apiLimit = limits?.[axis];
  if (apiLimit === undefined) return null;
  if (Math.abs(apiLimit - gateLimit) > 0.001) return null;
  return { measured, limit: apiLimit, axis };
}

/** Map a design-loop error frame payload to the failure turn.
 *
 * - A string `reason` present in the closed map → the mapped sentence
 *   (exhausted loop, retryable), raw reason as the secondary detail.
 * - A string `reason` OUTSIDE the map → the generic sentence, raw reason as
 *   the secondary detail (debuggability survives).
 * - No `reason` (infra-failure frames carry no DesignResult, nor do legacy
 *   frames) → generic copy, NOT a gate mapping. Still retryable: a plain
 *   chat request that fails for an infra reason is safe to resend.
 *
 * `envelopeLimits` (from GET /api/config/envelope) enables part 2 when
 * the frame's `message` is the raw gate-7 envelope string (the only
 * frame shape in which the measured number is established): the measured
 * value is parsed from that string, the limit is the API's per-axis
 * value. When the raw string is absent (the current frame shape carries
 * the reason, not the gate output), NO number is rendered — it is not
 * invented.
 *
 * `carriedAxes` (the frame's `carried_axes`, present only on a
 * bbox-gate failure) names the axes the gate enforced from the user's
 * own earlier statements (issue #261 fix batch): a bbox failure whose
 * enforced set is non-empty gets the carried-axis sentence (the held
 * value, formatted by `mm`, in the message) instead of the generic
 * "came out a different size" — the gate holds the user's earlier
 * number, and the copy says which one and how to override it. */
export function displayDesignLoopError(
  data: {
    message?: string;
    reason?: unknown;
    carried_axes?: unknown;
  },
  envelopeLimits?: [number, number, number],
): DisplayError {
  const rawMessage = typeof data.message === "string" ? data.message : "Stream error";
  const reason = typeof data.reason === "string" ? data.reason : undefined;
  if (reason !== undefined) {
    const mapped = (copy.failure.reasons as Record<string, string>)[reason];
    let message = mapped ?? UNKNOWN_REASON_COPY;
    // The carried-axis variant: the frame's `carried_axes` is the set the
    // gate enforced (the user's earlier statements, held by the carry-
    // forward merge). A bbox failure with at least one enforced axis says
    // which value was held, in the axis's own words — the value is the
    // user's own number, safe to render; `mm` decides the formatting.
    if (reason === "bbox_out_of_tolerance" && typeof data.carried_axes === "object" && data.carried_axes !== null) {
      const carried = data.carried_axes as Record<string, unknown>;
      const axes: Array<[string, number]> = [
        ["W", carried.W],
        ["D", carried.D],
        ["H", carried.H],
      ]
        .filter((entry): entry is [string, number] => typeof entry[1] === "number" && entry[1] > 0)
        .sort((a, b) => b[1] - a[1]);
      if (axes.length > 0) {
        const axisLabels: Record<string, string> = { W: "width", D: "depth", H: "height" };
        const label = axes.map(([a]) => axisLabels[a]).join(" and ");
        message = copy.failure.bboxCarried(label, axes[0][1]);
      }
    }
    let detail = reason;
    let envelope: DisplayError["envelope"];
    if (reason === "bbox_out_of_tolerance" && envelopeLimits !== undefined) {
      const parsed = parseEnvelopeGateDetail(rawMessage, envelopeLimits);
      if (parsed !== null) {
        envelope = parsed;
        // The raw gate string is the checker's actual words — it becomes
        // part 4's content, with the reason alongside it.
        detail = `${reason}: ${rawMessage}`;
      }
    }
    return {
      message,
      detail,
      retryable: true,
      reason,
      ...(envelope !== undefined ? { envelope } : {}),
    };
  }
  return {
    message: GENERIC_INFRA_COPY,
    detail: rawMessage,
    retryable: true,
  };
}
