/**
 * Brief — the "what are we building" overlay (top-left).
 *
 * The always-visible answer to "what do you think we're building?": one row
 * per declared parameter with its provenance (issue #123 / W9). The data
 * arrives from the backend's design-state block (issue #120) via
 * `entries` — this component NEVER computes provenance client-side; it
 * renders the four states honestly:
 *
 *   stated     filled dot, --color-live        the number
 *   measured   hollow ring, --color-faint      the number
 *   unknown    dashed ring                     the "not established" control,
 *                                              NEVER a number, never 0, never a dash
 *   disagrees  filled dot, --color-blocked     the MEASURED number (that is
 *                                              what will print) + the sentence
 *
 * While a pass is in flight, a row whose value is changing shows
 * "old → new" (both numbers, the new one in --color-live); a row being
 * re-measured shows `copy.brief.remeasuring` — never the stale number as if
 * fresh.
 *
 * The Brief is NOT a list that grows. When `entries.length` exceeds
 * MAX_LIST_ROWS (7) the resolved rows are grouped by part behind ONE honest
 * count (`copy.brief.allParameters`) and the unknowns are PROMOTED OUT of
 * the list — they are the thing to act on.
 *
 * When a pass fails the Brief does NOT change (the failed candidate was
 * never accepted) — it gains one ochre footer (`copy.brief.failedFooter`),
 * which is also the link into the failure card.
 *
 * A LIVE REGION PIN collapses the Brief to its chip (the region bar is the
 * task; the Brief is reference). The chip retains the resolved rows so
 * nothing is lost.
 *
 * Presentational: the parent (App) owns the chip/full-mode decision (the
 * 1200px/820px window rule, issue #119), the fetch, and the in-flight
 * bookkeeping.
 */

import { useState } from "react";
import copy, { mm } from "../../copy";
import { MARKER_COLOR } from "../../lib/marker";
import type { DesignStateEntry } from "../../lib/api";

/** Above this many rows the resolved list collapses to one honest count
 *  (the "not a list that grows" rule — design answer 1). */
const MAX_LIST_ROWS = 7;

const MARKS = {
  stated: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    backgroundColor: "var(--color-live)",
  },
  measured: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    border: "1px solid var(--color-faint)",
    backgroundColor: "transparent",
  },
  unknown: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    border: "1px dashed var(--color-faint)",
    backgroundColor: "transparent",
  },
  disagrees: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    backgroundColor: "var(--color-blocked)",
  },
} as const;

interface BriefProps {
  /** True when the window is below the chip threshold — the Brief
   *  renders compact, not as a full panel (the rule itself is owned by
   *  issue #119; this item must work in both forms). */
  isChip: boolean;
  /** The top/left inset from the stage edge (px). */
  inset: number;
  /** True when the conversation pane is collapsed — threaded from App
   *  to keep the style object byte-identical to the pre-extraction
   *  inline style (issue #116: extract AS-IS). */
  conversationCollapsed: boolean;
  /** The design-state block (issue #120). NEVER computed client-side. */
  entries?: DesignStateEntry[];
  /** The parameters whose values the pass in flight is changing; `old` is
   *  the value the row held before the pass. */
  inFlight?: Record<string, { old: number; new: number }>;
  /** The parameters the pass in flight is re-measuring. */
  reMeasuring?: string[];
  /** When set (the pass just failed), the Brief gains the ochre footer —
   *  and nothing else. The failed candidate was never accepted. */
  failedPass?: string;
  /** A live region pin (the region bar owns the task) — the Brief
   *  collapses to its chip whatever the window size says. */
  hasLivePin?: boolean;
  /** The resolved module id from a pending region pick — the matching
   *  Brief row is outlined in the marker colour (taken as an inline style
   *  from MARKER_COLOR — no CSS token, by design / W17). The outline
   *  makes the click-on-pixels legibly a click-on-parameter. */
  highlightModuleId?: string | null;
  /** The unknown-value control's question — sent to the assistant. */
  onAsk?: (label: string) => void;
  /** The expanded row's "Change it" — routed to the assistant. */
  onChange?: (label: string) => void;
  /** The expanded row's "Show it on the model" — routed to the pick. */
  onShowOnModel?: (name: string) => void;
}

/** Format a design-state value for the value cell. `null` must never be
 *  formatted — the unknown cell is the control, and a `?? 0` / `String()`
 *  default here would re-introduce issue #91's null→0 defect. */
function formatValue(value: number | string | boolean | null): string | null {
  if (value === null) return null;
  if (typeof value === "number") return mm(value);
  return String(value);
}

/** The primary displayed value for a row: `disagrees` shows the MEASURED
 *  value (that is what will print), everything else shows its own value. */
function primaryValue(entry: DesignStateEntry): number | string | boolean | null {
  if (entry.provenance === "disagrees") return entry.value;
  return entry.value;
}

/** The row label — a parameter with no human label uses its NAME as the
 *  label. The block always carries one, but a defensive fallback keeps the
 *  contract honest. */
function rowLabel(entry: DesignStateEntry): string {
  return entry.label || entry.name;
}

export function Brief({
  isChip,
  inset,
  conversationCollapsed,
  entries,
  inFlight,
  reMeasuring,
  failedPass,
  hasLivePin,
  highlightModuleId,
  onAsk,
  onChange,
  onShowOnModel,
}: BriefProps) {
  const [expanded, setExpanded] = useState<string | null>(null);

  const safeEntries = entries ?? [];
  const chip = isChip || hasLivePin === true;

  const unknowns = safeEntries.filter((e) => e.provenance === "unknown");
  const resolved = safeEntries.filter((e) => e.provenance !== "unknown");
  const showGroups = !chip && resolved.length > MAX_LIST_ROWS;

  const renderRow = (entry: DesignStateEntry) => {
    const label = rowLabel(entry);
    const name = entry.name;
    const isExpanded = expanded === name;

    const inFlightEntry = inFlight?.[name];
    const isReMeasuring = reMeasuring?.includes(name) === true;

    // The value cell, in priority order: in-flight (both values) >
    // re-measuring (the stale number is never shown as fresh) >
    // awaiting first measure (a measured row with no value yet) >
    // unknown (the control — never a number) > the value itself.
    let valueNode;
    if (inFlightEntry) {
      valueNode = (
        <span className="brief-value" data-testid="brief-value">
          {mm(inFlightEntry.old)} →{" "}
          <span style={{ color: "var(--color-live)" }}>
            {mm(inFlightEntry.new)}
          </span>
        </span>
      );
    } else if (isReMeasuring) {
      valueNode = (
        <span className="brief-value" data-testid="brief-value">
          {copy.brief.remeasuring}
        </span>
      );
    } else if (entry.provenance === "measured" && entry.value === null) {
      valueNode = (
        <span className="brief-value" data-testid="brief-value">
          {copy.brief.awaitingFirstMeasure}
        </span>
      );
    } else if (entry.provenance === "unknown") {
      valueNode = (
        <button
          type="button"
          className="brief-unknown-btn"
          data-testid="brief-unknown-btn"
          onClick={() => onAsk?.(label)}
          style={{
            border: "1px dashed var(--color-faint)",
            borderRadius: 6,
            background: "transparent",
            color: "var(--color-fg-2)",
            cursor: "pointer",
            padding: "2px 8px",
            fontFamily: "var(--font-ui)",
            fontSize: 13,
          }}
        >
          {copy.brief.unknownValue}
        </button>
      );
    } else {
      const formatted = formatValue(primaryValue(entry));
      // `null` must never reach the number cell — the unknown branch above
      // is the only path that produces it, and a `?? 0` here would
      // re-introduce issue #91's null→0 defect (the W9 contract assertion
      // is the tripwire).
      valueNode =
        formatted === null ? (
          <span className="brief-value" data-testid="brief-value">
            {copy.brief.unknownValue}
          </span>
        ) : (
          <span
            className="brief-value"
            data-testid="brief-value"
            style={{ fontFamily: "var(--font-mono)" }}
          >
            {formatted}
          </span>
        );
    }

    // The disagreement sentence — rendered only on an expanded row; the
    // value cell keeps the measured number as the primary.
    const disagreementNode =
      entry.provenance === "disagrees" && isExpanded ? (
        <p
          className="brief-disagreement"
          data-testid="brief-disagreement"
          style={{
            margin: 0,
            color: "var(--color-fg-2)",
            fontSize: 13,
            lineHeight: 1.5,
          }}
        >
          {copy.brief.disagreement(
            Number(entry.stated_value ?? 0),
            Number(entry.value ?? 0),
          )}
        </p>
      ) : null;

    // The expanded row: the provenance in a sentence + the two actions.
    // The sentence names WHAT the value is; the value cell above it names
    // the value itself — the sentence is never the value.
    const expandedSentence =
      entry.provenance === "stated"
        ? `${formatValue(entry.value) ?? copy.brief.unknownValue} — ${copy.brief.legend.stated}. ${copy.brief.provenanceNoMeasurement(String(entry.value))}`
        : entry.provenance === "measured"
          ? `${formatValue(entry.value) ?? copy.brief.unknownValue} — ${copy.brief.legend.measured}. ${copy.brief.provenanceNoMeasurement(String(entry.value))}`
          : entry.provenance === "unknown"
            ? copy.brief.legend.unknown
            : copy.brief.legend.disagrees;

    const expandedNode = isExpanded ? (
      <div className="brief-row-expanded" data-testid="brief-row-expanded">
        <p style={{ margin: 0, color: "var(--color-fg-2)", fontSize: 13 }}>
          {expandedSentence}
        </p>
        <div style={{ display: "flex", gap: 8, marginTop: 4 }}>
          <button
            type="button"
            data-testid="brief-action-change"
            onClick={() => onChange?.(label)}
            style={{
              border: "1px solid var(--color-hairline)",
              borderRadius: 6,
              background: "transparent",
              color: "var(--color-fg)",
              cursor: "pointer",
              padding: "2px 8px",
            }}
          >
            {copy.brief.rowActions.change}
          </button>
          <button
            type="button"
            data-testid="brief-action-locate"
            onClick={() => onShowOnModel?.(name)}
            style={{
              border: "1px solid var(--color-hairline)",
              borderRadius: 6,
              background: "transparent",
              color: "var(--color-fg)",
              cursor: "pointer",
              padding: "2px 8px",
            }}
          >
            {copy.brief.rowActions.locate}
          </button>
        </div>
      </div>
    ) : null;

    const mark = MARKS[entry.provenance];

    return (
      <div
        className="brief-row"
        data-testid={`brief-row-${name}`}
        data-provenance={entry.provenance}
        // The resolved module id from a pending pick is outlined in the
        // marker colour (MARKER_COLOR, inline style — no CSS token by design).
        // A pick on pixels is a click on a parameter: the outline makes that
        // mapping legible. The outline is 2px so it reads as a marker, not a
        // subtle hover state.
        style={
          highlightModuleId === name
            ? {
                outline: `2px solid ${MARKER_COLOR}`,
                outlineOffset: -1,
                borderRadius: 6,
              }
            : undefined
        }
      >
        <div
          style={{ display: "flex", alignItems: "center", gap: 8, cursor: "pointer" }}
          onClick={() => setExpanded(isExpanded ? null : name)}
        >
          <span
            data-testid="brief-mark"
            style={{
              flex: "0 0 auto",
              display: "inline-block",
              ...mark,
            }}
          />
          <span
            style={{
              flex: "1 1 auto",
              color: "var(--color-fg)",
              fontSize: 14,
              overflow: "hidden",
              textOverflow: "ellipsis",
              whiteSpace: "nowrap",
            }}
          >
            {label}
          </span>
          {valueNode}
        </div>
        {disagreementNode}
        {expandedNode}
      </div>
    );
  };

  return (
    <div
      className="brief-panel"
      data-testid="brief-panel"
      data-mode={chip ? "chip" : "full"}
      style={{
        position: "absolute",
        top: inset,
        left: inset,
        ...(conversationCollapsed ? {} : { marginTop: 0 }),
        zIndex: 10,
        padding: chip ? "8px 12px" : "16px",
        borderRadius: 8,
        background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
        color: "var(--color-fg)",
        maxWidth: chip ? "none" : 420,
      }}
    >
      {copy.brief.eyebrow}

      {safeEntries.length === 0 ? (
        <p
          className="brief-empty"
          data-testid="brief-empty"
          style={{ margin: "8px 0 0", color: "var(--color-fg-2)", fontSize: 13 }}
        >
          {copy.brief.emptyBody}
        </p>
      ) : chip ? (
        // The chip keeps the resolved rows (name · value) so nothing is
        // lost — and the unknown count, because the unknowns are the thing
        // to act on. The failed footer is NOT in the chip (it is a
        // full-panel footnote); a live pin already suppresses the chip's
        // own content anyway, so this branch only fires at small windows.
        <div className="brief-chip" data-testid="brief-chip">
          {resolved.length > 0 && (
            <span className="brief-chip-resolved" data-testid="brief-chip-resolved">
              {resolved
                .map((e) => {
                  const v = formatValue(primaryValue(e));
                  return v === null ? null : `${rowLabel(e)} · ${v}`;
                })
                .filter(Boolean)
                .join(" · ")}
            </span>
          )}
          {unknowns.length > 0 && (
            <span className="brief-chip-unknowns" data-testid="brief-chip-unknowns">
              {copy.brief.collapsedUnknowns(unknowns.length)}
            </span>
          )}
        </div>
      ) : (
        <div className="brief-body" data-testid="brief-body">
          {/* Unknowns are promoted OUT of the list — they are the thing to
              act on. Rendered above whatever the resolved list does. */}
          {unknowns.length > 0 && (
            <div className="brief-unknowns" data-testid="brief-unknowns">
              {unknowns.map((e) => renderRow(e))}
            </div>
          )}

          {showGroups ? (
            // Above MAX_LIST_ROWS the resolved list does not grow — it
            // collapses to ONE honest count, grouped by part.
            <div className="brief-groups" data-testid="brief-groups">
              <span
                className="brief-groups-count"
                data-testid="brief-groups-count"
                style={{ color: "var(--color-fg-2)", fontSize: 13 }}
              >
                {copy.brief.allParameters(resolved.length)}
              </span>
              <span
                className="brief-groups-summary"
                data-testid="brief-groups-summary"
                style={{ color: "var(--color-muted)", fontSize: 12, marginLeft: 8 }}
              >
                {copy.brief.groupSummary(1, copy.brief.groupCount(1))}
              </span>
            </div>
          ) : (
            <div className="brief-rows" data-testid="brief-rows">
              {resolved.map((e) => renderRow(e))}
            </div>
          )}
        </div>
      )}

      {failedPass !== undefined && failedPass !== null && (
        <div
          className="brief-failed-footer"
          data-testid="brief-failed-footer"
          style={{
            marginTop: 8,
            padding: "4px 8px",
            background: "color-mix(in srgb, var(--color-blocked) 18%, transparent)",
            color: "var(--color-blocked)",
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          {copy.brief.failedFooter(failedPass)}
        </div>
      )}
    </div>
  );
}
