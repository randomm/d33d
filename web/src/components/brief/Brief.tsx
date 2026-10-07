/**
 * Brief — the "what are we building" overlay (top-right in full mode,
 * top-left as the chip — issue #209: the full panel mirrors the
 * conversation pane on the right so the two overlays never overlap).
 *
 * The always-visible answer to "what do you think we're building?": one row
 * per declared parameter with its provenance (issue #123 / W9). The data
 * arrives from the backend's design-state block (issue #120) via
 * `entries` — this component NEVER computes provenance client-side; it
 * renders the four states honestly:
 *
 *   stated     filled dot, --color-live        the number
 *   measured   hollow ring, --color-faint      the number
 *   assumed    half-filled dot, faint          the number (the model picked
 *                                              it — nobody said it, issue #246)
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
 * The Brief is NOT a list that grows. When the number of COLLAPSIBLE rows
 * (settled, agreeing param rows — provenance stated / measured / assumed,
 * `kind: "param"`) exceeds MAX_LIST_ROWS (7), those rows fold into ONE
 * honest disclosure (`copy.brief.moreParameters`), behind an expand
 * button. Axis rows, disagrees rows, and unknowns are NEVER grouped —
 * the unknowns are PROMOTED OUT of the list because they are the thing
 * to act on.
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

import { useMemo, useState } from "react";
import copy from "../../copy";
import type { DesignStateEntry, PartReportInfo, ProjectStorage } from "../../lib/api";
import { splitBriefZones, BriefZoneLayout } from "./BriefZones";
import { BriefRow, BriefRowList } from "./BriefRow";
import { formatValue, primaryValue, rowLabel } from "./briefRowHelpers";

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
  /** True when the design-state refetch failed twice in a row (issue #237):
   *  the last-known block stays visible (never wiped) and this line
   *  surfaces the failure — the stale-empty "Nothing yet" state must not
   *  persist silently. Clears on the next successful fetch. */
  refreshFailed?: boolean;
  /** Issue #316: the design-state envelope's `history_missing` flag (true
   *  when the project's repo directory is absent — the same predicate as
   *  `storage.repo_present`). ORed with `storage.repo_present === false`
   *  into the single `brief-saved-missing` banner, rendered once — the two
   *  signals are the same underlying condition by construction. */
  historyMissing?: boolean;
  /** The project's live storage signal (issue #295) — the SPA reads it,
   *  never recomputes. `repo_present === false` → the "saved design
   *  missing" banner; `photo_present === false` (NOT null) → the
   *  "reference photo missing" marker. Both in var(--color-blocked). */
  storage?: ProjectStorage;
  /** A live region pin (the region bar owns the task) — the Brief
   *  collapses to its chip whatever the window size says. */
  hasLivePin?: boolean;
  /** Issue #338: the design-state `part` block. When present the Brief
   *  splits into "The part you brought" (W/D/H, never folded) and
   *  "Your changes" (the normal stated/assumed rows). When absent the
   *  Brief renders exactly as before — a single list, no zone headers. */
  part?: PartReportInfo | null;
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
  /** Issue #388: true while a design run is in flight — the programmatic
   *  send buttons (the unknown-value "What is the…?" control and the
   *  row's "Change it" action) are disabled, because they are NOT
   *  composer sends (decision 2): they are never queued, and a fast click
   *  during a run would race the in-flight POST (409). The drawn selection
   *  and any in-progress text are kept — the controls simply close while
   *  the run is in flight. */
  sendInFlight?: boolean;
}

export function Brief({
  isChip,
  inset,
  conversationCollapsed,
  entries,
  inFlight,
  reMeasuring,
  failedPass,
  refreshFailed,
  hasLivePin,
  highlightModuleId,
  storage,
  historyMissing,
  part,
  onAsk,
  onChange,
  onShowOnModel,
  sendInFlight,
}: BriefProps) {
  const [expanded, setExpanded] = useState<string | null>(null);
  // Issue #274: the collapsed-params group is hidden behind a disclosure
  // button; `groupsOpen` remembers whether the user opened it.
  const [groupsOpen, setGroupsOpen] = useState(false);

  const safeEntries = entries ?? [];
  const chip = isChip || hasLivePin === true;

  // Issue #295: the storage-degradation states. `savedDesignMissing` fires
  // only when the server says the repo (and therefore the source) is gone —
  // the persisted params/bbox still render underneath, as-is, so the banner
  // is the ONLY thing that changes. `photoMissing` fires ONLY on `false`
  // (a path was stored, the file was lost): `null` (never had a photo) and
  // `true` show nothing.

  const unknowns = safeEntries.filter((e) => e.provenance === "unknown");
  const assumed = safeEntries.filter((e) => e.provenance === "assumed");
  const resolved = safeEntries.filter((e) => e.provenance !== "unknown");

  // Issue #316: one banner from two signals — the design-state
  // `history_missing` flag (the wire source, computed per request) OR the
  // project-GET `storage.repo_present === false`. Same underlying
  // condition by construction; the OR makes either signal sufficient (a
  // failed storage GET no longer hides the banner when the design-state
  // fetch established the repo is gone), and the single `{... &&}` below
  // guarantees it renders exactly once.
  const savedDesignMissing =
    historyMissing === true || storage?.repo_present === false;
  const photoMissing = storage?.photo_present === false;

  // Issue #338: the two-zone split. When `part` is present the resolved
  // list splits into "The part you brought" (W/D/H from part.bbox_mm, never
  // folded) and "Your changes" (the normal stated/assumed rows, which DO
  // fold behind MAX_LIST_ROWS). When `part` is null the zone has no part
  // rows and `hasPart` is false — the single-list shape is byte-identical
  // to the pre-#338 layout.
  const zones = useMemo(() => splitBriefZones(resolved, part ?? null), [
    resolved,
    part,
  ]);

  // The change row renderer, now the extracted <BriefRow> component (issue
  // #338: the two-zone addition must not grow Brief.tsx, so the row and its
  // helpers live in BriefRow.tsx / briefRowHelpers.ts). The wrapper threads
  // the list's shared props (expanded identity, in-flight, re-measuring,
  // highlight) so the row renders with the full single-list behaviour.
  const renderRow = (entry: DesignStateEntry) => (
    <BriefRow
      entry={entry}
      expanded={expanded}
      onToggleExpanded={(identity) =>
        setExpanded(expanded === identity ? null : identity)
      }
      inFlight={inFlight}
      reMeasuring={reMeasuring}
      highlightModuleId={highlightModuleId}
      onAsk={onAsk}
      onChange={onChange}
      onShowOnModel={onShowOnModel}
      sendInFlight={sendInFlight}
    />
  );

  return (
    <div
      className="brief-panel"
      data-testid="brief-panel"
      data-mode={chip ? "chip" : "full"}
      style={{
        position: "absolute",
        top: inset,
        // Full panel: right-anchored (issue #209); chip: left-anchored as
        // before — the conversation pane keeps the top-left corner.
        ...(chip ? { left: inset } : { right: inset }),
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

      {savedDesignMissing && (
        <div
          className="brief-saved-missing"
          data-testid="brief-saved-missing"
          style={{
            marginTop: 8,
            padding: "4px 8px",
            background: "color-mix(in srgb, var(--color-blocked) 18%, transparent)",
            color: "var(--color-blocked)",
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          {copy.brief.savedDesignMissing}
        </div>
      )}

      {photoMissing && (
        <span
          className="brief-photo-missing"
          data-testid="brief-photo-missing"
          style={{
            display: "inline-block",
            marginTop: 8,
            padding: "2px 8px",
            background: "color-mix(in srgb, var(--color-blocked) 18%, transparent)",
            color: "var(--color-blocked)",
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          {copy.brief.referencePhotoMissing}
        </span>
      )}

      {/* Issue #352: "Nothing yet" is the no-part empty state. When the
          design-state `part` block is present the part zone ("The part
          you brought" — W/D/H "waiting on units" while the units are
          unsettled) IS the content, so the empty body never renders,
          even with zero design-state entries. The two-zone layout below
          renders the part zone in both cases (it does not depend on
          `safeEntries`). */}
      {safeEntries.length === 0 && !part ? (
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
          {/* Assumed values are counted SEPARATELY from unknowns (issue
              #246): an assumed value IS established (the model picked it),
              an unknown one is not. The chip only — full mode shows the
              per-row half-dot marks instead. */}
          {assumed.length > 0 && (
            <span className="brief-chip-assumed" data-testid="brief-chip-assumed">
              {copy.brief.collapsedAssumed(assumed.length)}
            </span>
          )}
        </div>
      ) : (
        <div className="brief-body" data-testid="brief-body">
          {/* Unknowns are promoted OUT of the list — they are the thing to
              act on. Rendered above whatever the resolved list does. */}
          {unknowns.length > 0 && (
            <div className="brief-unknowns" data-testid="brief-unknowns">
              <BriefRowList
                entries={unknowns}
                expanded={expanded}
                onToggleExpanded={(identity) =>
                  setExpanded(expanded === identity ? null : identity)
                }
                inFlight={inFlight}
                reMeasuring={reMeasuring}
                highlightModuleId={highlightModuleId}
                onAsk={onAsk}
                onChange={onChange}
                onShowOnModel={onShowOnModel}
                sendInFlight={sendInFlight}
              />
            </div>
          )}

          {/* Issue #338, operator decision 1: two zones when `part` is
            present — the presentation lives in BriefZones.tsx (the split
            AND the layout) so Brief.tsx does not grow. "The part you
            brought" (W/D/H, NEVER folded) first; "Your changes" folds
            behind its own MAX_LIST_ROWS count. No part — single list,
            the pre-#338 shape; splitBriefZones folds the collapsible
            rows behind ONE honest count above MAX_LIST_ROWS (issue
            #274) via the same layout. */}
          <BriefZoneLayout
              zones={zones}
              groupsOpen={groupsOpen}
              onToggleGroups={() => setGroupsOpen((open) => !open)}
              renderChangeRow={renderRow}
            />
        </div>
      )}

      {refreshFailed === true && (
        <div
          className="brief-refresh-failed"
          data-testid="brief-refresh-failed"
          style={{
            marginTop: 8,
            padding: "4px 8px",
            background: "color-mix(in srgb, var(--color-blocked) 18%, transparent)",
            color: "var(--color-blocked)",
            borderRadius: 4,
            fontSize: 13,
          }}
        >
          {copy.brief.refreshFailed}
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
