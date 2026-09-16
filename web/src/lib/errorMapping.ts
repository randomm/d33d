/**
 * Plain-language mapping of design-loop failure reasons (issue #82).
 *
 * The terminal `error` frame carries a STRUCTURED `reason` (from
 * `d33d/design_loop_events.py`) whose value is closed: the four
 * `GATE_REASON_BITS` from `d33d/design_loop.py` plus the seven render-worker
 * `ErrorClass` values. `plainFailureMessage` maps that closed set to
 * human-readable copy — a design-loop failure that the loop itself reported
 * (exhausted after 3 iterations) is worded as a retryable design problem,
 * never as a system crash.
 *
 * The free-text `message` field (e.g. "Design loop exhausted:
 * error_class_not_ok") is never shown as the primary copy when a mapping
 * exists: NN/g heuristic #9 — plain language, no error codes.
 */

/** The four GATE_REASON_BITS (d33d/design_loop.py) + the seven render-worker
 *  ErrorClass values, mapped to plain-language sentences. */
const FAILURE_REASON_COPIES: Record<string, string> = {
  // GATE_REASON_BITS
  error_class_not_ok:
    "The model could not be generated — the design step produced no usable output.",
  views_blank_or_missing:
    "The model rendered but the preview images came out blank.",
  bbox_out_of_tolerance: "The model does not fit the printer's build volume.",
  stated_dims_not_named_parameters:
    "The stated dimensions were not used as named parameters in the model.",
  // render ErrorClass values
  ok: "The model could not be generated — the design step produced no usable output.",
  syntax_error:
    "The generated design had a syntax error — the model could not be built.",
  empty_model: "The generated design produced an empty model — nothing to print.",
  artifact_error: "The rendered model file could not be read — the output was unusable.",
  timeout: "The render timed out — try a simpler shape.",
  oom: "The model was too heavy to render — try a simpler shape.",
  container_error: "The render environment failed — this is temporary, please try again.",
};

/** The generic fallback for a reason code outside the closed set. */
const UNKNOWN_REASON_COPY =
  "The design could not be generated. You can retry, or describe the part in more detail.";

const GENERIC_INFRA_COPY =
  "Something went wrong while working on your design. Please try again — if it keeps happening, the details below may help.";

export interface DisplayError {
  /** The primary plain-language sentence. */
  message: string;
  /** The raw technical detail, shown in a collapsed <details>. */
  detail?: string;
  /** True when a Retry control is offered (design-loop failures only). */
  retryable: boolean;
}

/** Map a design-loop error frame payload to display copy.
 *
 * - A string `reason` present in the closed map → the mapped sentence
 *   (exhausted loop, retryable), raw reason as the secondary detail.
 * - A string `reason` OUTSIDE the map → the generic sentence, raw reason as
 *   the secondary detail (debuggability survives).
 * - No `reason` (infra-failure frames carry no DesignResult, nor do legacy
 *   frames) → generic copy, NOT a gate mapping. Still retryable: a plain
 *   chat request that fails for an infra reason is safe to resend.
 */
export function displayDesignLoopError(data: {
  message?: string;
  reason?: unknown;
}): DisplayError {
  const rawMessage = typeof data.message === "string" ? data.message : "Stream error";
  const reason = typeof data.reason === "string" ? data.reason : undefined;
  if (reason !== undefined) {
    const mapped = FAILURE_REASON_COPIES[reason];
    return {
      message: mapped ?? UNKNOWN_REASON_COPY,
      detail: reason,
      retryable: true,
    };
  }
  return {
    message: GENERIC_INFRA_COPY,
    detail: rawMessage,
    retryable: true,
  };
}
