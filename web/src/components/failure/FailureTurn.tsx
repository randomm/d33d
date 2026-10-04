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
import { SIZE_AXIS_WORDS, type DisplayError } from "../../lib/errorMapping";

/** The model pre-flight frame's `env_var` field (issue #303) as carried
 *  on the mapped `DisplayError` (the `envVar` field — the mapping copies
 *  it from the frame). Absent → `null`: the helper is the unresolved
 *  variant, never a fabricated name. */
function envVarOf(error: DisplayError): string | null {
  return error.envVar ?? null;
}

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
  /** True when the export is actually available (issue #352, operator
   *  decision 4: the part's units are settled AND no design pass is in
   *  flight — the same 409 gate the export button renders). When false
   *  the survived line names the survivor but omits the "still
   *  exportable" claim (the button being disabled is a contradiction
   *  the copy must not make). Absent defaults to true (the pre-#352
   *  behaviour). */
  exportable?: boolean;
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
  exportable = true,
  inFlight,
  onAction,
}: FailureTurnProps) {
  // Card selection (issue #367): one value names the card.
  // Producer: the `envelope` discriminator value is parsed in `displayDesignLoopError` from the `gate7/envelope` string d33d/print_validation.py emits via `_GATE7_ENVELOPE_PREFIX` (pinned by the bed-card regression test).
  //   "bed"    — the envelope gate is established, i.e. `displayDesignLoopError`
  //              attached `envelope` (the gate-7 string was parsed). Never
  //              keyed on the reason alone: a stated-size gate failure also
  //              arrives as `bbox_out_of_tolerance` but carries no gate-7
  //              string, so `envelope` is absent.
  //   "size"   — a `bbox_out_of_tolerance` frame WITHOUT the gate-7 string
  //              (the stated-size gate, operator decision 2).
  //   "generic"— everything else, or a size frame with no carried/measured
  //              axes (rule 2(b): the reason sentence, no rows).
  const isEnvelope = error.envelope !== undefined;
  const hasSizeData =
    !isEnvelope &&
    error.reason === "bbox_out_of_tolerance" &&
    (error.carriedAxes !== undefined || error.measuredAxes !== undefined);
  const card: "bed" | "size" | "generic" = isEnvelope
    ? "bed"
    : hasSizeData
      ? "size"
      : "generic";
  const envelopeData: DisplayError["envelope"] | null =
    error.envelope ?? null;

  // The model pre-flight helper (issue #303): the terminal
  // `model_unconfigured` frame's `env_var` field selects the second
  // sentence — the named-variable sentence when it is established,
  // the model-settings sentence when the model did not resolve (null).
  // Rendered in the mono face: the variable name is a measurement, not
  // prose. No other reason renders a helper (only the pre-flight frame
  // carries `env_var`).
  const modelEnvVar = envVarOf(error);
  const modelHelper =
    error.reason === "model_unconfigured"
      ? modelEnvVar !== null
        ? copy.failure.modelUnconfiguredHelper(modelEnvVar)
        : copy.failure.modelUnresolved
      : null;

  // The renderer image pre-flight (issue #346): the terminal
  // `renderer_image_stale` frame's `renderer_detail` field is carried
  // STRUCTURED on the mapped error (`rendererDetail` — the mapping
  // validates the frame field and copies it through; the disclosure
  // `detail` stays the plain reason string). The reason line (image
  // missing, or the label mismatch naming both values) and the exact
  // rebuild command (a measurement, mono face) render only when the
  // structured field is established. Absent → nothing extra is rendered;
  // the collapsed disclosure still shows whatever `detail` carried. No
  // number, no value, no command the SPA has not established.
  const rendererDetail =
    error.reason === "renderer_image_stale" ? error.rendererDetail : undefined;
  const faultLine =
    rendererDetail !== undefined
      ? rendererDetail.reason === "image_missing"
        ? copy.failure.rendererImageMissing
        : copy.failure.rendererImageLabelMismatch(
            rendererDetail.actual ?? "(unlabeled)",
            rendererDetail.expected ?? "(unknown)",
          )
      : null;
  const rebuildCommand = rendererDetail?.rebuild_command ?? null;

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

  // The size-mismatch rows (issue #367): one mono row per axis, asked →
  // made. The axis union is the W/D/H axes present in either carriedAxes
  // (asked) or measuredAxes (made). Each row renders:
  //   asked + made  → "width: 60.0 mm → 66.0 mm"
  //   made only     → "depth: 42.0 mm"
  //   asked only    → "height: not measured yet"
  const sizeRows: Array<{ key: string; text: string }> | null =
    card === "size"
      ? (["W", "D", "H"] as const)
          .filter((a) => {
            const c = error.carriedAxes?.[a];
            const m = error.measuredAxes?.[a];
            return c !== undefined || m !== undefined;
          })
          .map((a) => {
            const c = error.carriedAxes?.[a];
            const m = error.measuredAxes?.[a];
            const word = SIZE_AXIS_WORDS[a];
            const text =
              c !== undefined && m !== undefined
                ? copy.failure.sizeMismatch.row(word, c, m)
                : m !== undefined
                  ? copy.failure.sizeMismatch.madeOnly(word, m)
                  : copy.failure.envelope.axisNotMeasured(word);
            return { key: a, text };
          })
      : null;

  // The follow-up question (issue #367, operator decision 3): at most
  // ONE per card — the FIRST axis in W, D, H order that has BOTH an
  // asked and a made value, AND only if they differ beyond the gate
  // tolerance (max(1%, 0.5 mm) — the same gate the failure frame came
  // from, re-derived here from the two numbers the frame carries; the
  // backend marks no per-axis tolerance, so the SPA owns this last step).
  // If no axis qualifies (the first with-both axis is within tolerance —
  // i.e. only a LATER axis actually diverged, or the frame is malformed),
  // no follow-up is offered.
  const sizeFollowUp: string | null = (() => {
    for (const a of ["W", "D", "H"] as const) {
      const c = error.carriedAxes?.[a];
      const m = error.measuredAxes?.[a];
      if (c === undefined || m === undefined) continue;
      const tolerance = Math.max(0.01 * c, 0.5);
      if (Math.abs(m - c) > tolerance) {
        return copy.failure.sizeMismatch.whichMeasurement(c, SIZE_AXIS_WORDS[a]);
      }
    }
    return null;
  })();

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

      {/* Part 1c — the model pre-flight helper (issue #303): the env-var
          name (mono, a measurement) or the model-settings nudge, when the
          terminal model_unconfigured frame established it. */}
      {modelHelper !== null && (
        <p className="failure-turn-body" data-testid="failure-turn-model-helper">
          {modelHelper}
        </p>
      )}

      {/* Part 1d — the renderer image pre-flight detail (issue #346):
          the verified fault (image missing / label X vs expected Y) and
          the exact rebuild command in the mono face. Only when the frame
          carried the structured `renderer_detail` field — never a
          fabricated command or value. */}
      {faultLine !== null && (
        <p className="failure-turn-body" data-testid="failure-turn-renderer-fault">
          {faultLine}
        </p>
      )}
      {rebuildCommand !== null && (
        <code
          className="failure-turn-body"
          data-testid="failure-turn-rebuild-command"
        >
          {rebuildCommand}
        </code>
      )}

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
          measurement renders its not-established phrase, never a number.
          Rendered ONLY for the envelope gate (bed card); the size-mismatch
          card (issue #367) has its own rows below. */}
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

      {/* Part 2b — the size-mismatch rows (issue #367): one mono row per
          axis, asked → made. Rendered only when the frame is a stated-size
          gate failure (bbox_out_of_tolerance, no gate-7 string) AND at
          least one axis has data (carried or measured). */}
      {sizeRows !== null && sizeRows.length > 0 && (
        <ul className="failure-turn-size-rows" data-testid="failure-turn-size-rows">
          {sizeRows.map((row) => (
            <li key={row.key} className="failure-turn-size-row" data-testid={`failure-turn-size-row-${row.key}`}>
              {row.text}
            </li>
          ))}
        </ul>
      )}

      {/* Always say what survived — but claim exportability only when
          the export is actually available (issue #352, operator decision
          4): when the export button is disabled (unsettled units or a
          pass in flight) the line names the survivor without the
          export claim. */}
      {keptVersion !== null && keptVersion !== undefined && (
        <p className="failure-turn-survived" data-testid="failure-turn-survived">
          {exportable
            ? copy.failure.survived(keptVersion)
            : copy.failure.survivedNoExport(keptVersion)}
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
        ) : card === "size" && sizeFollowUp !== null ? (
          // The size-mismatch card offers ONE follow-up question (issue
          // #367, operator decision 3): the first axis in W/D/H order
          // that has both asked and made. Clicking prefills the composer
          // via the existing onAction→onSend path.
          <button
            type="button"
            className="failure-turn-action"
            data-testid="failure-action-which-measurement"
            disabled={inFlight}
            onClick={() => onAction(sizeFollowUp)}
          >
            {sizeFollowUp}
          </button>
        ) : (
          // The retry control is offered only when the failure is
          // retryable: pre-flight configuration failures (issue #303's
          // `model_unconfigured`, issue #346's `renderer_image_stale`)
          // are configuration states — retrying just fails again — so
          // they render NO retry button (the helper sentence in part 1
          // says what to do instead).
          error.retryable ? (
            <button
              type="button"
              className="failure-turn-action"
              data-testid="failure-action-retry"
              disabled={inFlight}
              onClick={() => onAction(copy.failure.retryAction)}
            >
              {copy.failure.retryAction}
            </button>
          ) : null
        )}
      </div>

      {/* Part 4 — what the checker actually said, collapsed. The raw
          reason (or the raw frame message on infra failures), never
          reworded. */}
      {error.detail !== undefined && error.detail !== "" && (
        <details className="failure-turn-raw" data-testid="failure-turn-raw">
          <summary>{copy.failure.rawDisclosure}</summary>
          <code data-testid="failure-turn-raw-code">{error.detail}</code>
          {rebuildCommand !== null && (
            <code data-testid="failure-turn-raw-rebuild">
              {copy.failure.rebuildLabel}: {rebuildCommand}
            </code>
          )}
        </details>
      )}
    </div>
  );
}
