/**
 * BriefRow — one design-state row in the Brief list (issue #338, extracted
 * from Brief.tsx so the two-zone addition does not grow that file).
 *
 * The row renders the four provenance states honestly (stated / measured /
 * assumed / unknown / disagrees), the in-flight "old → new" cell, the
 * re-measuring control, the disagreement sentence, the expanded provenance
 * sentence + the two actions, and the pending-pick outline. All of that
 * behaviour is unchanged from the single-list row — this is a pure
 * extraction, not a re-implementation.
 */

import { MARKER_COLOR } from "../../lib/marker";
import copy, { mm } from "../../copy";
import type { DesignStateEntry } from "../../lib/api";
import { rowLabel, rowIdentity, rowTestId, formatValue, primaryValue, MARKS } from "./briefRowHelpers";

/** One row of the Brief list with its shared list context (the expanded
 *  identity, in-flight / re-measuring bookkeeping, the pending-pick
 *  highlight, and the row actions). Used by every list surface in Brief.tsx
 *  so the key + prop-threading lives in one place. */
export function BriefRowList({
  entries,
  expanded,
  onToggleExpanded,
  inFlight,
  reMeasuring,
  highlightModuleId,
  onAsk,
  onChange,
  onShowOnModel,
  sendInFlight,
}: {
  entries: DesignStateEntry[];
  expanded: string | null;
  onToggleExpanded: (identity: string) => void;
  inFlight?: Record<string, { old: number; new: number }>;
  reMeasuring?: string[];
  highlightModuleId?: string | null;
  onAsk?: (label: string) => void;
  onChange?: (label: string) => void;
  onShowOnModel?: (name: string) => void;
  /** Issue #388: true while a design run is in flight — the programmatic
   *  send buttons (unknown-value control, "Change it") are disabled. */
  sendInFlight?: boolean;
}) {
  return entries.map((entry) => (
    <BriefRow
      key={rowIdentity(entry)}
      entry={entry}
      expanded={expanded}
      onToggleExpanded={onToggleExpanded}
      inFlight={inFlight}
      reMeasuring={reMeasuring}
      highlightModuleId={highlightModuleId}
      onAsk={onAsk}
      onChange={onChange}
      onShowOnModel={onShowOnModel}
      sendInFlight={sendInFlight}
    />
  ));
}

export function BriefRow({
  entry,
  expanded,
  onToggleExpanded,
  inFlight,
  reMeasuring,
  highlightModuleId,
  onAsk,
  onChange,
  onShowOnModel,
  sendInFlight,
}: {
  entry: DesignStateEntry;
  expanded: string | null;
  onToggleExpanded: (identity: string) => void;
  inFlight?: Record<string, { old: number; new: number }>;
  reMeasuring?: string[];
  highlightModuleId?: string | null;
  onAsk?: (label: string) => void;
  onChange?: (label: string) => void;
  onShowOnModel?: (name: string) => void;
  /** Issue #388: true while a design run is in flight — the programmatic
   *  send buttons (unknown-value control, "Change it") are disabled. */
  sendInFlight?: boolean;
}) {
  const label = rowLabel(entry);
  const name = entry.name;
  const identity = rowIdentity(entry);
  const isExpanded = expanded === identity;

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
    // Issue #388: disabled while a run is in flight (programmatic sends
    // are never queued — decision 2).
    const unknownDisabled = sendInFlight === true;
    valueNode = (
      <button
        type="button"
        className="brief-unknown-btn"
        data-testid="brief-unknown-btn"
        onClick={() => onAsk?.(label)}
        title={unknownDisabled ? copy.shell.disabledReason : undefined}
        aria-describedby={
          unknownDisabled ? `brief-disabled-reason-${name}` : undefined
        }
        disabled={unknownDisabled}
        style={{
          border: "1px dashed var(--color-faint)",
          borderRadius: 6,
          background: "transparent",
          color: "var(--color-fg-2)",
          cursor: unknownDisabled ? "not-allowed" : "pointer",
          padding: "2px 8px",
          fontFamily: "var(--font-ui)",
          fontSize: 13,
          opacity: unknownDisabled ? 0.5 : 1,
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
        {entry.disagrees_source === "model"
          ? copy.brief.disagreementModel(
              label,
              Number(entry.stated_value ?? 0),
              Number(entry.value ?? 0),
            )
          : copy.brief.disagreement(
              Number(entry.stated_value ?? 0),
              Number(entry.value ?? 0),
            )}
      </p>
    ) : null;

  // The expanded row: the provenance in a sentence + the two actions.
  // The sentence names WHAT the value is; the value cell above it names
  // the value itself — the sentence is never the value.
  // The assumed row: "Nobody said this. I picked {value}." — issue #248
  // adds the "because {reason}" clause when the model carried a reason
  // for the value (the reason is the model's own words, never invented).
  const expandedSentence =
    entry.provenance === "stated"
      ? `${formatValue(entry.value) ?? copy.brief.unknownValue} — ${copy.brief.legend.stated}. ${copy.brief.provenanceNoMeasurement(String(entry.value))}`
      : entry.provenance === "measured"
        ? `${formatValue(entry.value) ?? copy.brief.unknownValue} — ${copy.brief.legend.measured}. ${copy.brief.provenanceNoMeasurement(String(entry.value))}`
        : entry.provenance === "assumed"
          ? entry.reason
            ? copy.brief.provenanceAssumedWithReason(
                formatValue(entry.value) ?? copy.brief.unknownValue,
                entry.reason,
              )
            : copy.brief.provenanceAssumed(
                formatValue(entry.value) ?? copy.brief.unknownValue,
              )
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
          title={sendInFlight === true ? copy.shell.disabledReason : undefined}
          aria-describedby={
            sendInFlight === true ? `brief-disabled-reason-${name}` : undefined
          }
          disabled={sendInFlight === true}
          style={{
            border: "1px solid var(--color-hairline)",
            borderRadius: 6,
            background: "transparent",
            color: "var(--color-fg)",
            cursor: sendInFlight === true ? "not-allowed" : "pointer",
            padding: "2px 8px",
            opacity: sendInFlight === true ? 0.5 : 1,
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

  // The mark. A disagrees row is NOT always ochre: the backend decides
  // its severity (`disagrees_major` — |measured − model| beyond
  // max(20% of the model value, 5 mm), issue #274) and the SPA reads
  // the flag, never recomputes it. User-source and axis disagreements
  // are always ochre; a model-source disagreement within the threshold
  // renders the quiet neutral measured mark. The region marker colour is
  // never used here (the tripwire pins the hex to lib/marker.ts).
  const mark =
    entry.provenance === "disagrees" &&
    entry.kind === "param" &&
    entry.disagrees_source === "model" &&
    entry.disagrees_major !== true
      ? MARKS.measured
      : MARKS[entry.provenance];

  return (
    <div
      className="brief-row"
      // Stable identity across re-renders and refetches: the row's
      // data-testid and the expanded state are both keyed off
      // kind:name — `name` alone is NOT unique within a block (a param
      // row and an axis row can both be `W`), so `kind`+`name` is the
      // row identity the list key, the testid, and the expanded state
      // all share (issue #246 review: duplicate identity for W/D/H
      // rows; issue #196: the unkeyed lists were the "unique key" warning).
      data-testid={rowTestId(entry)}
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
        onClick={() => onToggleExpanded(identity)}
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
            // Issue #248: a model label renders in the UI face; a raw
            // SCAD identifier (no model label) renders in the mono face
            // (mono = machine value — the user can tell a human label
            // from an identifier at a glance).
            fontFamily: entry.label_is_identifier
              ? "var(--font-mono)"
              : "var(--font-ui)",
          }}
        >
          {label}
        </span>
        {valueNode}
      </div>
      {disagreementNode}
      {expandedNode}
      {sendInFlight === true && (
        <span
          id={`brief-disabled-reason-${name}`}
          data-testid={`brief-disabled-reason-${name}`}
          style={{ fontSize: 12, color: "var(--color-muted)" }}
        >
          {copy.shell.disabledReason}
        </span>
      )}
    </div>
  );
}
