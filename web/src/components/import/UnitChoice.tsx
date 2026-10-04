/**
 * UnitChoice — the shared unit choice (issue #350 fix round).
 *
 * The three-unit option list (when `options` is present) plus the W/D/H
 * measurement escape + settle button. Rendered from BOTH the assumed
 * branch ("Change the units" opens it) and the unsettled branch — one
 * component, no duplication. The `testIdPrefix` distinguishes the two
 * call sites (`import-report-assumed` / `import-report`), so every
 * data-testid the existing tests read stays byte-identical.
 */

import type { PartOption } from "../../lib/api";
import copy, { mm } from "../../copy";

export interface UnitChoiceProps {
  /** `import-report-assumed` (assumed branch) or `import-report`
   *  (unsettled branch) — prefixes the option/escape/axis/settle testids. */
  testIdPrefix: string;
  /** The unit options (the `import-report-option-{unit}` rows). */
  options: PartOption[] | null;
  /** The selected escape axis (shared state — both call sites read/write
   *  the same value). */
  escapeAxis: "W" | "D" | "H";
  onEscapeAxis: (axis: "W" | "D" | "H") => void;
  /** The escape number field (a string — validated client-side). */
  escapeMm: string;
  onEscapeMm: (value: string) => void;
  /** The settle actions (a unit choice / the escape measurement). */
  onSettleUnit: (unit: "mm" | "cm" | "inch") => void;
  onSettleAxis: () => void;
  busy: boolean;
}

export function UnitChoice({
  testIdPrefix,
  options,
  escapeAxis,
  onEscapeAxis,
  escapeMm,
  onEscapeMm,
  onSettleUnit,
  onSettleAxis,
  busy,
}: UnitChoiceProps) {
  return (
    <>
      {options && options.length > 0 && (
        <div
          data-testid={`${testIdPrefix}-options`}
          style={{
            display: "flex",
            flexDirection: "column",
            gap: 8,
            marginTop: 12,
          }}
        >
          {options.map((opt) => (
            <button
              key={opt.unit}
              type="button"
              data-testid={`import-report-option-${opt.unit}`}
              onClick={() => onSettleUnit(opt.unit)}
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
        data-testid={`${testIdPrefix}-escape`}
        style={{ marginTop: 12, display: "flex", flexDirection: "column", gap: 8 }}
      >
        <span style={{ fontSize: "var(--font-size-xs)", color: "var(--color-fg-2)" }}>
          {copy.partReport.escapeLine}
        </span>
        <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
          <div
            data-testid={`${testIdPrefix}-escape-axis`}
            style={{ display: "inline-flex", gap: 4 }}
          >
            {(["W", "D", "H"] as const).map((axis) => (
              <button
                key={axis}
                type="button"
                data-testid={`${testIdPrefix}-axis-${axis}`}
                onClick={() => onEscapeAxis(axis)}
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
            data-testid={`${testIdPrefix}-escape-mm`}
            min="0"
            step="any"
            value={escapeMm}
            onChange={(e) => onEscapeMm(e.target.value)}
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
            data-testid={`${testIdPrefix}-settle-btn`}
            onClick={onSettleAxis}
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
    </>
  );
}
