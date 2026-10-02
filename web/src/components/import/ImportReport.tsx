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

import { useState, type ChangeEvent } from "react";
import { ApiClient, ApiError, type PartReportInfo } from "../../lib/api";
import copy, { mm } from "../../copy";

const numberFmt = new Intl.NumberFormat("en-US");

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
}

export function ImportReport({
  part,
  projectId,
  client,
  onSettled,
  showPlate,
}: ImportReportProps) {
  const api = client;
  const [settleError, setSettleError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  // Escape-input state (the one-real-measurement path): an explicit W/D/H
  // segmented choice + a number field. Validated client-side (finite > 0)
  // — never POSTed otherwise, and the lexicon is not used here (the spec
  // forbids a free-text measurement sentence on this surface).
  const [escapeAxis, setEscapeAxis] = useState<"W" | "D" | "H">("W");
  const [escapeMm, setEscapeMm] = useState("");

  const report = part.report;
  const triangles = report ? report.triangles : null;
  const bodies = report ? report.bodies : null;
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

  const watertightLine =
    watertight === null
      ? null
      : watertight
        ? "watertight"
        : "not watertight";

  return (
    <div
      className="import-report"
      data-testid="import-report"
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
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
          width: "min(720px, 92vw)",
          maxWidth: "100%",
          padding: 24,
          borderRadius: "var(--radius)",
          background: "color-mix(in srgb, var(--color-panel) 40%, transparent)",
          border: "1px solid var(--color-hairline)",
          color: "var(--color-fg)",
          pointerEvents: "auto",
        }}
      >
        {/* The report header: "I read {filename}" (filename in mono). */}
        <h1
          className="import-report-title"
          data-testid="import-report-title"
          style={{ margin: 0, fontSize: "var(--font-size-lg)", fontWeight: 500, textAlign: "center" }}
        >
          <span data-testid="import-report-iRead">{copy.partReport.iRead(part.filename)}</span>
        </h1>
        {/* The filename in mono (with ellipsis) — the report's hero string. */}
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

        {/* The triangle / body / watertight line (numbers in mono). */}
        {triangles !== null && bodies !== null && (
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

        {/* Units (per the table). */}
        {isSettled && mmBbox && (
          <div data-testid="import-report-settled">
            <p data-testid="import-report-wdh" style={{ margin: 0, textAlign: "center" }}>
              <span style={{ fontFamily: "var(--font-mono)" }}>
                {mmBbox.map((e) => mm(e)).join(" × ")}
              </span>
            </p>
          </div>
        )}
        {isAssumed && mmBbox && (
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
              onClick={() => settleUnit("inch")}
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
          </div>
        )}
        {status === "unsettled" && (
          <div data-testid="import-report-unsettled">
            <p data-testid="import-report-waiting" style={{ margin: 0, textAlign: "center" }}>
              {copy.partReport.waitingOnUnits}
            </p>
            {part.options && part.options.length > 0 && (
              <div
                data-testid="import-report-options"
                style={{
                  display: "flex",
                  flexDirection: "column",
                  gap: 8,
                  marginTop: 12,
                }}
              >
                {part.options.map((opt) => (
                  <button
                    key={opt.unit}
                    type="button"
                    data-testid={`import-report-option-${opt.unit}`}
                    onClick={() => settleUnit(opt.unit as "mm" | "cm" | "inch")}
                    disabled={busy}
                    style={{
                      display: "flex",
                      justifyContent: "space-between",
                      alignItems: "center",
                      padding: "10px 14px",
                      border: "1px solid var(--color-hairline)",
                      borderRadius: "var(--radius-sm)",
                      background: "var(--color-recess)",
                      color: "var(--color-fg)",
                      cursor: busy ? "not-allowed" : "pointer",
                    }}
                  >
                    <span>{copy.partReport.unitLabels[opt.unit] ?? opt.unit}</span>
                    <span style={{ fontFamily: "var(--font-mono)" }}>
                      {opt.extents_mm.map((e) => mm(e)).join(" × ")}
                    </span>
                  </button>
                ))}
              </div>
            )}
            {/* The escape input: explicit W/D/H segmented choice + number. */}
            <div
              data-testid="import-report-escape"
              style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}
            >
              <span style={{ fontSize: "var(--font-size-xs)", color: "var(--color-fg-2)" }}>
                {copy.partReport.escapeLine}
              </span>
              <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                <div
                  data-testid="import-report-escape-axis"
                  style={{ display: "inline-flex", gap: 4 }}
                >
                  {(["W", "D", "H"] as const).map((axis) => (
                    <button
                      key={axis}
                      type="button"
                      data-testid={`import-report-axis-${axis}`}
                      onClick={() => setEscapeAxis(axis)}
                      style={{
                        padding: "4px 10px",
                        border: `1px solid ${
                          escapeAxis === axis ? "var(--color-live)" : "var(--color-hairline)"
                        }`,
                        borderRadius: "var(--radius-sm)",
                        background: escapeAxis === axis ? "var(--color-live)" : "var(--color-recess)",
                        color: escapeAxis === axis ? "var(--color-canvas)" : "var(--color-fg)",
                        cursor: "pointer",
                      }}
                    >
                      {axis}
                    </button>
                  ))}
                </div>
                <input
                  type="number"
                  data-testid="import-report-escape-mm"
                  min="0"
                  step="any"
                  value={escapeMm}
                  onChange={(e: ChangeEvent<HTMLInputElement>) => setEscapeMm(e.target.value)}
                  placeholder={copy.partReport.escapePlaceholder}
                  aria-label={copy.partReport.axisLabels[escapeAxis]}
                  style={{
                    flex: "1 1 0",
                    minWidth: 0,
                    padding: "8px 12px",
                    fontSize: "var(--font-size-base)",
                    color: "var(--color-fg)",
                    background: "var(--color-recess)",
                    border: "1px solid var(--color-hairline)",
                    borderRadius: "var(--radius-sm)",
                    fontFamily: "var(--font-mono)",
                  }}
                />
                <button
                  type="button"
                  data-testid="import-report-settle-btn"
                  onClick={settleAxis}
                  disabled={busy || !Number.isFinite(Number(escapeMm)) || Number(escapeMm) <= 0}
                  style={{
                    padding: "8px 16px",
                    border: "none",
                    borderRadius: "var(--radius-sm)",
                    background: "var(--color-live)",
                    color: "var(--color-canvas)",
                    fontWeight: 500,
                    cursor: "pointer",
                  }}
                >
                  {copy.partReport.settle}
                </button>
              </div>
            </div>
          </div>
        )}

        {/* The settle error (a failed settle's detail verbatim — blocked). */}
        {settleError && (
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
        {!showPlate && (
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
