/**
 * FailureTurn — the failure as a turn in the conversation (issue #124, W12).
 *
 * Four parts, always in this order:
 *   1. What happened — the plain-language sentence (mapped from the closed
 *      failure reason; for the envelope gate the measured value and the
 *      limit are named together in the sentence).
 *   2. The number that matters — per-axis bars, the measured value beside
 *      the limit, the failing axis in the blocked colour. The measured
 *      value comes from the raw gate string (the frame's only source of
 *      the number); the limit comes from the API envelope — never a
 *      literal in this surface.
 *   3. What you can do — the concrete actions, which prefill the composer
 *      (they execute nothing).
 *   4. What the checker actually said — the raw reason, collapsed.
 *
 * Always says what survived. Every failure renders identically, however
 * many times it repeats — no attempt counting, no goal state.
 */

import { copy } from "../../copy";
import type { DisplayError } from "../../lib/errorMapping";

interface EnvelopeAxisRow {
  /** The axis letter from the API envelope's x/y/z — "x", "y" or "z". */
  label: string;
  /** The measured extent in mm, or `null` when not established (the
   *  gate measures one axis per failure; the others are unknown). */
  measured: number | null;
  /** The limit in mm, from the API envelope (never a literal). */
  limit: number;
  /** True for the axis the gate reported as failing. */
  failing: boolean;
}

interface FailureTurnProps {
  /** The structured failure to display. */
  error: DisplayError;
  /** The API-reported build envelope (GET /api/config/envelope). When
   *  present and the failure is the envelope gate, the bars render
   *  beside the sentence. Absent (fetch failed) → no bars, no numbers —
   *  nothing is invented. */
  envelope?: { x: number; y: number; z: number } | null;
  /** The version label that survived the failed pass (the latest version
   *  in the timeline — the failure was never a version). Absent when no
   *  version exists yet. */
  keptVersion?: string | null;
  /** The in-flight flag — disables the action buttons while a loop runs. */
  inFlight: boolean;
  /** An action click: the text is sent through the composer (it prefills
   *  and sends; it executes nothing by itself). */
  onAction: (text: string) => void;
}

const AXIS_LABELS = ["x", "y", "z"] as const;

export function FailureTurn({
  error,
  envelope,
  keptVersion,
  inFlight,
  onAction,
}: FailureTurnProps) {
  const isEnvelope = error.reason === "bbox_out_of_tolerance";
  const envelopeData = isEnvelope && error.envelope !== undefined ? error.envelope : null;

  // Part 1 — the sentence. For the envelope gate with a measurement, the
  // deck's body names measured and limit together (the headline stays as
  // the lead line).
  const headline = isEnvelope ? copy.failure.envelope.headline : error.message;
  const body =
    isEnvelope && envelopeData !== null
      ? copy.failure.envelope.body(envelopeData.measured, envelopeData.limit)
      : isEnvelope
        ? error.message
        : null;

  // Part 2 — the per-axis bars. Rendered when the envelope gate measured
  // an axis AND the API envelope is available (both numbers established).
  const rows: EnvelopeAxisRow[] | null =
    isEnvelope && envelope !== null && envelope !== undefined
      ? ([
          {
            label: AXIS_LABELS[0],
            measured: envelopeData?.axis === 0 ? envelopeData.measured : null,
            limit: envelope.x,
            failing: envelopeData?.axis === 0,
          },
          {
            label: AXIS_LABELS[1],
            measured: envelopeData?.axis === 1 ? envelopeData.measured : null,
            limit: envelope.y,
            failing: envelopeData?.axis === 1,
          },
          {
            label: AXIS_LABELS[2],
            measured: envelopeData?.axis === 2 ? envelopeData.measured : null,
            limit: envelope.z,
            failing: envelopeData?.axis === 2,
          },
        ] as EnvelopeAxisRow[])
      : null;

  return (
    <div
      className="failure-turn"
      data-testid="failure-turn"
      role="alert"
      aria-label={headline}
    >
      {/* Part 1 — what happened. */}
      <p className="failure-turn-sentence" data-testid="failure-turn-sentence">
        {headline}
        {body !== null && <span className="failure-turn-body">{body}</span>}
      </p>

      {/* Part 1b — the per-param mismatch detail (issue #276): one line
          per mismatching parameter, formatted by the SPA's own copy
          helper (label + the model's declared value + the measured
          extent, both mm-formatted), in the mono face, under the
          number-free headline. The numbers are the server's own gate
          evidence — structured on the frame, formatted here, never
          re-derived. */}
      {error.mismatches !== undefined && error.mismatches !== null && (
        <ul className="failure-turn-mismatches" data-testid="failure-turn-mismatches">
          {error.mismatches.map((m, i) => (
            <li key={`${m.label}-${i}`} className="failure-turn-mismatch-line">
              {copy.failure.axisMismatchLine(m.label, m.model, m.measured)}
            </li>
          ))}
        </ul>
      )}

      {/* Part 2 — the number that matters, shown. The per-axis bars: track
          is the limit, fill is the part, the failing axis in the blocked
          colour. One component at every severity — an axis without a
          measurement renders its not-established phrase, never a number. */}
      {rows !== null && (
        <div className="failure-turn-bars" data-testid="failure-turn-bars">
          {rows.map((row) => {
            const text =
              row.measured !== null
                ? row.failing
                  ? copy.failure.envelope.axisRow(row.label, row.measured, row.limit)
                  : copy.failure.envelope.axisRowFits(row.label, row.measured, row.limit)
                : copy.failure.envelope.axisNotMeasured(row.label);
            const failed = row.failing;
            const fillPct =
              row.measured !== null && row.limit > 0
                ? Math.min(100, (row.measured / row.limit) * 100)
                : 0;
            return (
              <div
                key={row.label}
                className={`failure-turn-bar${failed ? " failure-turn-bar--failing" : ""}`}
                data-testid={`failure-turn-bar-${row.label}`}
              >
                <span className="failure-turn-bar-text">{text}</span>
                <span className="failure-turn-bar-track" aria-hidden="true">
                  <span
                    className="failure-turn-bar-fill"
                    style={{ width: `${fillPct}%` }}
                  />
                </span>
              </div>
            );
          })}
        </div>
      )}

      {/* Always say what survived. */}
      {keptVersion !== null && keptVersion !== undefined && (
        <p className="failure-turn-survived" data-testid="failure-turn-survived">
          {copy.failure.survived(keptVersion)}
        </p>
      )}

      {/* Part 3 — what you can do. Concrete actions that prefill the
          composer; they execute nothing by themselves. */}
      <div className="failure-turn-actions" data-testid="failure-turn-actions">
        {isEnvelope ? (
          <>
            <button
              type="button"
              className="failure-turn-action"
              data-testid="failure-action-split"
              disabled={inFlight}
              onClick={() => onAction(copy.failure.envelope.actions.split)}
            >
              {copy.failure.envelope.actions.split}
            </button>
            <button
              type="button"
              className="failure-turn-action"
              data-testid="failure-action-scale"
              disabled={inFlight}
              onClick={() => onAction(copy.failure.envelope.actions.scale)}
            >
              {copy.failure.envelope.actions.scale}
            </button>
            <button
              type="button"
              className="failure-turn-action"
              data-testid="failure-action-bigger"
              disabled={inFlight}
              onClick={() => onAction(copy.failure.envelope.actions.biggerPrinter)}
            >
              {copy.failure.envelope.actions.biggerPrinter}
            </button>
          </>
        ) : (
          <button
            type="button"
            className="failure-turn-action"
            data-testid="failure-action-retry"
            disabled={inFlight}
            onClick={() => onAction(copy.failure.retryAction)}
          >
            {copy.failure.retryAction}
          </button>
        )}
      </div>

      {/* Part 4 — what the checker actually said, collapsed. The raw
          reason (or the raw frame message on infra failures), never
          reworded. */}
      {error.detail !== undefined && error.detail !== "" && (
        <details className="failure-turn-raw" data-testid="failure-turn-raw">
          <summary>{copy.failure.rawDisclosure}</summary>
          <code data-testid="failure-turn-raw-code">{error.detail}</code>
        </details>
      )}
    </div>
  );
}
