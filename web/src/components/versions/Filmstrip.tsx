/**
 * Filmstrip — the horizontal version strip (bottom-left overlay, ~96px).
 *
 * Horizontal reads as time: the four most recent versions occupy four fixed
 * slots, everything older collapses into one honest count (`+N earlier`),
 * and the version being built appears immediately as a dashed slot — so the
 * strip is never behind the conversation. The strip is the lineage you are ON:
 * a version with siblings carries a fork mark and their count; branch risers
 * and the compare live in the expanded sheet (W16), which has room to draw a
 * graph. The strip selects only — restore / pin / branch actions are W16's.
 *
 * ABSENT, NOT EMPTY: when a project has no versions and no pass is in flight,
 * the filmstrip renders nothing at all (the old "No versions yet" branch is
 * gone). A pass in flight with zero versions renders just the dashed pending
 * slot — the strip is never behind the conversation.
 *
 * Never shows git: no commit hashes, no branch names — the same invariant the
 * backend asserts.
 *
 * Presentational: the parent (App) owns the version state, the pass-in-flight
 * flag, and the compare-select callback. The four-slot rule is about COST, not
 * truncation: a version selected via the expanded sheet (W16) that is no longer
 * in the four visible slots has no DOM target here — the strip does not grow.
 */

import type { VersionTimelineEntry } from "../../lib/api";
import copy from "../../copy";

interface FilmstripProps {
  /** The version timeline entries (oldest first). */
  versions: VersionTimelineEntry[];
  /** True while a design-loop pass is in flight — renders the dashed slot. */
  passInFlight: boolean;
  /** The intended name of the version being built (the pending slot's label). */
  pendingName: string | null;
  /** The top/left inset from the stage edge (px). */
  inset: number;
  /** Called with the version id (compare-select). The strip selects only. */
  onCompareSelect: (versionId: number) => void;
  /** Called when a version's expand mark is clicked (opens the sheet). */
  onOpenSheet: (versionId: number) => void;
  /** True while the sheet is open (the open slot's mark is active). */
  sheetOpenFor: number | null;
}

const SLOTS = 4;

/**
 * The ids of every version that shares a parent with another — the fork set.
 * A version "has siblings" when two or more versions in the list point at the
 * same parent (or all point at no parent, i.e. the root fork). Siblings are
 * computed from the list itself, so the mark is honest for whatever the backend
 * returns and never invents a branch that is not in the data.
 */
function siblingCounts(versions: VersionTimelineEntry[]): Map<number, number> {
  const byParent = new Map<string, number[]>();
  for (const v of versions) {
    const key = v.parent === null ? "root" : `p${v.parent}`;
    const ids = byParent.get(key) ?? [];
    ids.push(v.id);
    byParent.set(key, ids);
  }
  const counts = new Map<number, number>();
  for (const ids of byParent.values()) {
    if (ids.length > 1) {
      for (const id of ids) counts.set(id, ids.length);
    }
  }
  return counts;
}

/**
 * The slot label's diff fragment — `v4 · D 30→45.0`. Renders only when a
 * param changed vs the version's own parent (`diff_count > 0`); the first
 * version (no parent, `diff_count` 0) shows the version label alone. The diff
 * summary is the single most-changed param, read from the version's params vs
 * the parent's; with a parent id in hand we look the parent up in the list.
 */
function diffFragment(
  v: VersionTimelineEntry,
  all: VersionTimelineEntry[],
): string | null {
  if (v.diff_count === 0 || v.parent === null) return null;
  const parent = all.find((p) => p.id === v.parent);
  if (!parent) return null;
  // The one param that changed most is the one the operator cares about.
  const changed = (Object.keys(v.params) as Array<keyof typeof v.params>).filter(
    (k) => k in parent.params && parent.params[k] !== v.params[k],
  );
  if (changed.length === 0) return null;
  const k = changed[0];
  const a = parent.params[k];
  const b = v.params[k];
  return `${String(k)} ${a}→${b}`;
}

export function Filmstrip({
  versions,
  passInFlight,
  pendingName,
  inset,
  onCompareSelect,
  onOpenSheet,
  sheetOpenFor,
}: FilmstripProps) {
  // ABSENT, NOT EMPTY: no versions and no in-flight pass → nothing at all.
  if (versions.length === 0 && !passInFlight) {
    return null;
  }

  const siblings = siblingCounts(versions);
  const visible = versions.slice(-SLOTS);
  const earlierCount = versions.length - visible.length;
  const latestId = versions.length > 0 ? versions[versions.length - 1].id : null;

  return (
    <div
      className="version-tail-pane filmstrip"
      data-testid="version-filmstrip"
      style={{
        position: "absolute",
        bottom: inset,
        left: inset,
        zIndex: 10,
      }}
    >
      <div
        className="filmstrip-track"
        data-testid="filmstrip-track"
        style={{ display: "flex", alignItems: "center", gap: 8, height: 96 }}
      >
        {/* Everything older than the four slots collapses into one honest
            count — the strip costs the same at v4 and at v400. */}
        {earlierCount > 0 && (
          <span
            className="filmstrip-earlier"
            data-testid="filmstrip-earlier"
            aria-label={`${copy.history.earlierCount(earlierCount)} ${copy.history.earlierLabel}`}
          >
            {/* The collapsed count carries the mark if ANY of the hidden
                versions was exported — the mark stays discoverable when its
                version sits outside the four slots (issue #126 edge case).
                A hidden version's mark is real server state, so the count
                is never invented; it is augmented only when the data says
                so. */}
            {versions.some((v) => !visible.includes(v) && v.exported_at !== null) ? (
              <span data-testid="filmstrip-earlier-exported">{copy.history.exported} </span>
            ) : null}
            {copy.history.earlierCount(earlierCount)} {copy.history.earlierLabel}
          </span>
        )}

        {visible.map((v) => {
          const isCurrent = v.id === latestId;
          const fork = siblings.get(v.id);
          const diff = diffFragment(v, versions);
          return (
            <span
              key={v.id}
              className={`filmstrip-slot${isCurrent ? " filmstrip-slot--current" : ""}`}
            >
              {/* The slot is a real <button> (issue #127): natively focusable
                  and keyboard-activatable (Enter/Space fire the browser's
                  native click bridge). The expand control is a SIBLING, not
                  a child — no nested interactive elements. */}
              <button
                type="button"
                className="filmstrip-slot-btn"
                data-testid={`filmstrip-slot-${v.id}`}
                aria-current={isCurrent ? "true" : undefined}
                title={
                  v.exported_at !== null
                    ? copy.history.exportedAt(v.exported_at)
                    : undefined
                }
                style={
                  isCurrent
                    ? { outline: "2px solid var(--color-live)", outlineOffset: 2 }
                    : undefined
                }
                onClick={() => onCompareSelect(v.id)}
              >
                {v.thumbnail && (
                  <img
                    className="filmstrip-thumb"
                    src={v.thumbnail}
                    alt={`${v.name} thumbnail`}
                    data-testid={`filmstrip-thumb-${v.id}`}
                  />
                )}
                <span className="filmstrip-name" data-testid={`filmstrip-name-${v.id}`}>
                  {v.name}
                </span>
                <span className="filmstrip-pos" data-testid={`filmstrip-pos-${v.id}`}>
                  {` · v${v.id}`}
                  {diff !== null ? ` · ${diff}` : ""}
                </span>
                {fork !== undefined && (
                  <span
                    className="filmstrip-fork"
                    data-testid={`filmstrip-fork-${v.id}`}
                    aria-label={copy.history.variantCount(fork)}
                  >
                    ⑂ {copy.history.variantCount(fork)}
                  </span>
                )}
                {/* The exported mark (issue #126): which version was actually
                    handed out as a 3MF. Server-side state — it survives a
                    page reload. The mark is a WORD, not a colour: the motion
                    budget and the marker-colour invariant leave no room for
                    a badge colour here, and the time rides the title (the
                    deck's exportedAt) rather than a new string. */}
                {v.exported_at !== null && (
                  <span
                    className="filmstrip-exported"
                    data-testid={`filmstrip-exported-${v.id}`}
                  >
                    {copy.history.exported}
                  </span>
                )}
              </button>
              {/* The expand mark (W16): the sheet is reached FROM the
                  filmstrip — this is the affordance that opens it for the
                  slot's version. It is a SIBLING of the slot button, not a
                  child — no nested interactive elements. Its click does not
                  propagate to the slot (compare-select). */}
              <button
                type="button"
                className="filmstrip-expand"
                data-testid={`filmstrip-expand-${v.id}`}
                aria-label={`Expand ${v.name} in the history sheet`}
                aria-pressed={sheetOpenFor === v.id ? "true" : undefined}
                onClick={(e) => {
                  e.stopPropagation();
                  onOpenSheet(v.id);
                }}
              >
                ⌕
              </button>
            </span>
          );
        })}

        {/* The version being built appears IMMEDIATELY as a dashed slot — the
            strip is never behind the conversation. Rendered from the in-flight
            flag, not the versions list, so it appears before the version
            exists (before the #114 refetch lands). */}
        {passInFlight && (
          <span
            className="filmstrip-slot filmstrip-slot--pending"
            data-testid="filmstrip-pending"
            style={{ border: "2px dashed var(--color-live)" }}
          >
            <span className="filmstrip-name" data-testid="filmstrip-pending-name">
              {pendingName ?? copy.history.building}
            </span>
          </span>
        )}
      </div>
    </div>
  );
}
