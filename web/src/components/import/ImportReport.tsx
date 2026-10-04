/**
 * ImportReport — Screen 2 (issue #334, sub-issue D6/D7).
 *
 * The part read report + unit settlement. Rendered ONLY from the
 * design-state envelope's `part` (the settle response body is never
 * authoritative — the App refetches on a 200 settle and re-renders from
 * the fresh envelope, the D8 contract).
 *
 * Report (D6):
 *   - filename in mono (with ellipsis)
 *   - `{triangles} triangles · {bodies} body/bodies` (Intl.NumberFormat
 *     "en-US" grouping; the watertight flag renders three ways — never an
 *     invented number)
 *   - units per the table:
 *       - 3MF-settled → measured W×D×H
 *       - STL-assumed → "I read it as millimetres…" + one-tap change
 *       - STL-unsettled → "waiting on units" (no number), the options from
 *         `part.options` in the given order with labels + real mm extents,
 *         and the escape input (an explicit W/D/H segmented choice + a
 *         number field, finite > 0 validated client-side — never POSTed
 *         otherwise, no lexicon).
 *
 * All strings from copy.partReport — nothing inlined. Numbers render in
 * the mono face; prose in the UI face.
 */

import { useState, type CSSProperties } from "react";
import { ApiClient, ApiError, type PartReportInfo } from "../../lib/api";
import copy, { mm } from "../../copy";
import { UnitChoice } from "./UnitChoice";

const numberFmt = new Intl.NumberFormat("en-US");

// The card geometry hoisted out of the render (issue #352 tidy): the two
// shapes the card takes — the centred viewport overlay (full) and the
// compact top-left line (collapsed). Named consts so the JSX reads the
// intent, not the geometry.
const GEOMETRY_COLLAPSED: CSSProperties = {
  inset: "auto auto auto 0",
  display: "block",
  padding: 24,
};
const GEOMETRY_FULL: CSSProperties = {
  inset: 0,
  display: "flex",
  alignItems: "center",
  justifyContent: "center",
};

interface ImportReportProps {
  /** The design-state envelope's `part` (the ONLY source of truth). */
  part: PartReportInfo;
  /** The project id (for the settle POSTs). */
  projectId: number | null;
  /** Injectable API client (test seam). */
  client?: ApiClient;
  /** Fires after a 200 settle — the App refetches the design state so the
   *  fresh envelope (the new unit_status / scale) re-renders this surface. */
  onSettled: () => void;
  /** The plate is visible (units settled). The unsettled caption renders
   *  when this is false (D7). */
  showPlate: boolean;
  /** Issue #352 (operator decision 2): true once the units are settled or
   *  assumed, or the first design turn has started — the full viewport
   *  overlay collapses to a compact line (the settle controls stay in the
   *  card only while the part is unsettled, which is the only state where
   *  the card is full). The full report stays reachable from the Brief's
   *  "The part you brought" zone. */
  collapsed?: boolean;
}

export function ImportReport({
  part,
  projectId,
  client,
  onSettled,
  showPlate,
  collapsed = false,
}: ImportReportProps) {
  const api = client;
  const [settleError, setSettleError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  // The unit choice for an assumed part (issue #350, operator decision 1):
  // "Change the units" opens the SAME three-option unit choice plus the
  // measurement escape the unsettled path uses — it never silently settles.
  // Choosing an option settles the part (a 200 settle refetches the
  // envelope, the card re-renders settled with the confirmed note).
  const [unitChoiceOpen, setUnitChoiceOpen] = useState(false);

  // Escape-input state (the one-real-measurement path): an explicit W/D/H
  // segmented choice + a number field. Validated client-side (finite > 0)
  // — never POSTed otherwise, and the lexicon is not used here (the spec
  // forbids a free-text measurement sentence on this surface).
  const [escapeAxis, setEscapeAxis] = useState<"W" | "D" | "H">("W");
  const [escapeMm, setEscapeMm] = useState("");

  const report = part.report;
  const triangles = report ? report.triangles : null;
  const bodies = report ? report.bodies : null;
  const bodiesBefore = report ? report.bodies_before ?? null : null;
  const watertight = report ? report.watertight : null;
  const fileBbox = report ? report.bbox_file_units : null;

  const status = part.unit_status;
  const isSettled = status === "settled";
  const isAssumed = status === "assumed";

  const runSettle = async (
    fn: (pid: number) => Promise<unknown>,
  ): Promise<void> => {
    if (projectId === null || !api) {
      setSettleError(copy.partUpload.commitFailed);
      return;
    }
    setBusy(true);
    setSettleError(null);
    try {
      await fn(projectId);
      // D8: on a 200 settle, refetch the design state — the settle body is
      // never authoritative; the fresh envelope re-renders the report /
      // options / W-D-H.
      onSettled();
    } catch (e) {
      // A failed settle shows its detail verbatim and changes nothing.
      let detail: string;
      if (e instanceof ApiError) {
        detail = typeof e.detail === "string" ? e.detail : e.message;
      } else {
        detail = e instanceof Error ? e.message : String(e);
      }
      setSettleError(detail);
    } finally {
      setBusy(false);
    }
  };

  const settleUnit = (unit: "mm" | "cm" | "inch") => {
    void runSettle((pid) => api!.setPartUnit(pid, unit));
  };

  const settleAxis = () => {
    const v = Number(escapeMm);
    if (!Number.isFinite(v) || v <= 0) return;
    void runSettle((pid) => api!.setPartAxisMeasurement(pid, escapeAxis, v));
  };

  // The mm extents to display. For a settled/assumed part the file bbox ×
  // scale (the mm bbox) is known; for an unsettled part it is not (the
  // waiting state — never a number). The server always writes a 3-component
  // bbox; anything else degrades to the no-number state (a component never
  // displays a value it hasn't established — a 2-length bbox would render
  // a `NaN mm` otherwise).
  const scale = part.scale;
  const mmBbox =
    fileBbox && fileBbox.length === 3 && scale !== null && scale !== undefined
      ? fileBbox.map((e) => e * scale)
      : null;

  const gapsClosed = report ? report.gaps_closed : 0;
  const watertightLine =
    watertight === null
      ? null
      : watertight
        ? gapsClosed > 0
          ? copy.partReport.watertightGaps(gapsClosed)
          : "watertight"
        : "not watertight";

  return (
    <div
      className="import-report"
      data-testid="import-report"
      data-collapsed={collapsed || undefined}
      style={{
        position: "absolute",
        // Issue #352 (operator decision 2): the collapsed card stops
        // covering the viewport — a compact top-left line instead of the
        // centred inset-0 overlay (the model, the plate and the other
        // panels are no longer hidden behind it). The full form keeps
        // the centred overlay while the user settles the units.
        ...(collapsed ? GEOMETRY_COLLAPSED : GEOMETRY_FULL),
        zIndex: 10,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "stretch",
          gap: 16,
          width: collapsed ? "min(560px, 80vw)" : "min(720px, 92vw)",
          maxWidth: "100%",
          padding: 24,
          borderRadius: "var(--radius)",
          background: "color-mix(in srgb, var(--color-panel) 40%, transparent)",
          border: "1px solid var(--color-hairline)",
          color: "var(--color-fg)",
          pointerEvents: "auto",
        }}
      >
        {/* The report header: "I read {filename}" (filename in mono).
            Collapsed: a smaller line — the hero text, nothing else. */}
        <h1
          className="import-report-title"
          data-testid="import-report-title"
          style={{
            margin: 0,
            fontSize: collapsed ? "var(--font-size-sm)" : "var(--font-size-lg)",
            fontWeight: 500,
            textAlign: "center",
          }}
        >
          <span data-testid="import-report-iRead">{copy.partReport.iRead(part.filename)}</span>
        </h1>
        {/* The filename in mono (with ellipsis) — the report's hero string.
            Full form only: the compact line keeps the title. */}
        {!collapsed && (
          <span
            data-testid="import-report-filename"
            style={{
              fontFamily: "var(--font-mono)",
              overflow: "hidden",
              textOverflow: "ellipsis",
              display: "block",
              textAlign: "center",
              color: "var(--color-fg-2)",
            }}
          >
            {part.filename}
          </span>
        )}

        {/* The triangle / body / watertight line (numbers in mono). */}
        {!collapsed && triangles !== null && bodies !== null && (
          <p
            className="import-report-metrics"
            data-testid="import-report-metrics"
            style={{ margin: 0, textAlign: "center", color: "var(--color-fg-2)" }}
          >
            <span style={{ fontFamily: "var(--font-mono)" }}>
              {numberFmt.format(triangles)} triangles
            </span>{" "}
            ·{" "}
            <span style={{ fontFamily: "var(--font-mono)" }}>
              {numberFmt.format(bodies)} {bodies === 1 ? "body" : "bodies"}
            </span>
            {/* Issue #375: the dropped-body line — only when the report
                carries `bodies_before` (repair dropped a body). Numbers in
                the mono face. */}
            {bodiesBefore !== null && (
              <>
                {" "}·{" "}
                <span
                  data-testid="import-report-bodies-after-repair"
                  style={{ fontFamily: "var(--font-mono)" }}
                >
                  {copy.partReport.bodiesAfterRepair(bodiesBefore, bodies)}
                </span>
              </>
            )}
            {watertightLine !== null && (
              <>
                {" "}·{" "}
                <span
                  style={{
                    fontFamily: "var(--font-mono)",
                    color: watertight ? undefined : "var(--color-blocked)",
                  }}
                >
                  {watertightLine}
                </span>
              </>
            )}
          </p>
        )}

        {/* Units (per the table). Full form only: the compact line is the
            title (the part's facts stay in the Brief's part zone). */}
        {!collapsed && isSettled && mmBbox && (
          <div data-testid="import-report-settled">
            <p data-testid="import-report-wdh" style={{ margin: 0, textAlign: "center" }}>
              <span style={{ fontFamily: "var(--font-mono)" }}>
                {mmBbox.map((e) => mm(e)).join(" × ")}
              </span>
            </p>
          </div>
        )}
        {!collapsed && isAssumed && mmBbox && (
          <div data-testid="import-report-assumed">
            <p style={{ margin: 0, textAlign: "center" }}>
              {copy.partReport.assumedLine(
                mm(mmBbox[0]),
                mm(mmBbox[1]),
                mm(mmBbox[2]),
              )}
            </p>
            <button
              type="button"
              data-testid="import-report-change-units"
              onClick={() => setUnitChoiceOpen(true)}
              disabled={busy}
              style={{
                alignSelf: "center",
                marginTop: 8,
                padding: "6px 16px",
                border: "1px solid var(--color-hairline)",
                borderRadius: "var(--radius-sm)",
                background: "var(--color-recess)",
                color: "var(--color-fg)",
                cursor: busy ? "not-allowed" : "pointer",
              }}
            >
              {copy.partReport.changeUnits}
            </button>
            {/* Issue #350 (operator decision 1): the assumed part's unit
                choice is the SAME shared UnitChoice the unsettled path
                uses — never a silent settle to one unit. When the server
                sent no options, the shared choice renders just the
                measurement escape, and the one-line note explains that
                only the measurement option is available (no bare escape
                with no explanation). */}
            {unitChoiceOpen && (
              <>
                {!part.options && (
                  <p
                    data-testid="import-report-assumed-no-options"
                    style={{
                      margin: "12px 0 0",
                      textAlign: "center",
                      color: "var(--color-fg-2)",
                      fontSize: "var(--font-size-xs)",
                    }}
                  >
                    {copy.partReport.assumedNoOptionsLine}
                  </p>
                )}
                <UnitChoice
                  testIdPrefix="import-report-assumed"
                  options={part.options}
                  escapeAxis={escapeAxis}
                  onEscapeAxis={setEscapeAxis}
                  escapeMm={escapeMm}
                  onEscapeMm={setEscapeMm}
                  onSettleUnit={settleUnit}
                  onSettleAxis={settleAxis}
                  busy={busy}
                />
              </>
            )}
          </div>
        )}
        {status === "unsettled" && !collapsed && (
          <div data-testid="import-report-unsettled">
            <p data-testid="import-report-waiting" style={{ margin: 0, textAlign: "center" }}>
              {copy.partReport.waitingOnUnits}
            </p>
            <UnitChoice
              testIdPrefix="import-report"
              options={part.options}
              escapeAxis={escapeAxis}
              onEscapeAxis={setEscapeAxis}
              escapeMm={escapeMm}
              onEscapeMm={setEscapeMm}
              onSettleUnit={settleUnit}
              onSettleAxis={settleAxis}
              busy={busy}
            />
          </div>
        )}

        {/* The settle error (a failed settle's detail verbatim — blocked). */}
        {settleError !== null && (
          <p
            data-testid="import-report-settle-error"
            role="alert"
            style={{
              margin: 0,
              textAlign: "center",
              color: "var(--color-blocked)",
              fontFamily: "var(--font-mono)",
            }}
          >
            {settleError}
          </p>
        )}

        {/* The unsettled viewport caption (D7): the plate is hidden, the
            size is unknown. Rendered only while unsettled. */}
        {!showPlate && !collapsed && (
          <p
            data-testid="import-report-unsettled-caption"
            style={{
              margin: "8px 0 0",
              textAlign: "center",
              color: "var(--color-fg-2)",
              fontSize: "var(--font-size-xs)",
            }}
          >
            {copy.partReport.unsettledCaption}
          </p>
        )}
      </div>
    </div>
  );
}
