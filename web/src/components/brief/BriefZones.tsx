/**
 * BriefZones — the two-zone split for a Brief on a project that has an
 * imported part (issue #338, Screen 3).
 *
 * The zone logic lives in this small module so Brief.tsx does not grow.
 * When the design-state `part` block is present the Brief splits:
 *
 *   "The part you brought" — the W/D/H of the imported part, in measured
 *     provenance (hollow ring). Settled: the values come from the
 *     design-state `part.bbox_mm` (in mm, mono) with the part-zone note.
 *     Unsettled: the rows show the "waiting on units" control, never a
 *     number, and the note is not shown.
 *   "Your changes" — the normal stated / assumed rows (everything that is
 *     not the part's W/D/H). Its header renders only when it has at least
 *     one row.
 *
 * When the project has no part the Brief renders exactly as today — a
 * single merged list with no zone headers — so a no-part project is
 * byte-identical to before.
 *
 * The "Your changes" rows fold behind the SAME MAX_LIST_ROWS disclosure
 * the single list uses; the part rows (the measured W/D/H) are NEVER
 * folded — they always render.
 */

import copy from "../../copy";
import type { DesignStateEntry, PartReportInfo } from "../../lib/api";

/** The threshold the "Your changes" rows fold behind (the same constant
 *  the single list uses). */
export const MAX_LIST_ROWS = 7;

/** One row of the "Your changes" zone. */
export interface BriefChangeRow {
  entry: DesignStateEntry;
  /** `true` when the row was folded behind the collapse disclosure (the
   *  caller renders the count instead of the row). Never `true` for an
   *  axis / disagrees / unknown row — those always render. */
  collapsed: boolean;
}

/** One row of "The part you brought" (a W/D/H axis). */
export interface BriefPartRow {
  axis: "W" | "D" | "H";
  /** The mm value, or `null` while the part's units are unsettled (the
   *  row shows the waiting control, never a number). */
  value: number | null;
}

export interface BriefZones {
  /** `true` when a part block is present — the Brief splits into two
   *  zones. `false` when there is no part (single list, no headers). */
  hasPart: boolean;
  /** The W/D/H rows of "The part you brought". Present only when
   *  `hasPart`; the values are in mm (the settled unit) or `null` while
   *  unsettled. */
  partRows: BriefPartRow[];
  /** The part-zone note — rendered only for a settled part. `null` for an
   *  unsettled part (no note). */
  partNote: string | null;
  /** `true` when the "Your changes" header should render (it has at least
   *  one row). */
  showChangesHeader: boolean;
  /** The change rows in display order. */
  changeRows: BriefChangeRow[];
  /** The fold count for "Your changes", or `null` when no fold is needed
   *  (the count is the number of hidden settled param rows). */
  changesCount: number | null;
}

/** A W/D/H row that belongs to the part (an axis row, or a param row the
 *  dimension protocol named W / D / H) — NOT a change. */
function isPartRow(e: DesignStateEntry): boolean {
  return (
    (e.name === "W" || e.name === "D" || e.name === "H") &&
    (e.kind === "axis" || e.kind === "param")
  );
}

/** The COLLAPSIBLE predicate (the same one the single list uses): a
 *  settled, agreeing param row — stated / measured / assumed, never
 *  disagrees / axis / unknown. */
function isCollapsible(e: DesignStateEntry): boolean {
  return (
    e.kind === "param" &&
    e.provenance !== "disagrees" &&
    (e.provenance === "stated" ||
      e.provenance === "measured" ||
      e.provenance === "assumed")
  );
}

/**
 * Split the RESOLVED design-state entries into the two Brief zones.
 *
 * `resolved` is the non-unknown entries (the caller has already promoted
 * the unknowns). `part` is the design-state envelope's `part` block —
 * `null` when no part (the single-list shape).
 */
export function splitBriefZones(
  resolved: DesignStateEntry[],
  part: PartReportInfo | null,
): BriefZones {
  if (part === null) {
    // No part — the Brief renders exactly as today: a single list, no zone
    // headers. Every resolved row is a "change" row.
    const collapsible = resolved.filter(isCollapsible).length;
    const folded = collapsible > MAX_LIST_ROWS;
    return {
      hasPart: false,
      partRows: [],
      partNote: null,
      showChangesHeader: false,
      changeRows: resolved.map((entry) => ({
        entry,
        collapsed: folded && isCollapsible(entry),
      })),
      changesCount: folded ? collapsible : null,
    };
  }

  // Part present — the W/D/H of "The part you brought" come from the
  // design-state `part.bbox_mm` (the settled-unit mm extents). While the
  // part is unsettled `bbox_mm` is absent → the rows carry `null` (the
  // waiting control, never a number).
  const bbox = part.bbox_mm;
  const partRows: BriefPartRow[] = (["W", "D", "H"] as const).map((axis, i) => ({
    axis,
    value:
      bbox &&
      Array.isArray(bbox) &&
      bbox.length === 3 &&
      Number.isFinite(bbox[i])
        ? (bbox[i] as number)
        : null,
  }));
  const settled = part.unit_status === "settled";
  const partNote =
    settled && part.unit ? copy.brief.partBroughtNote(part.unit) : null;

  // The change rows are every resolved entry that is NOT the part's W/D/H.
  const changeEntries = resolved.filter((e) => !isPartRow(e));
  const collapsible = changeEntries.filter(isCollapsible).length;
  const folded = collapsible > MAX_LIST_ROWS;

  return {
    hasPart: true,
    partRows,
    partNote,
    showChangesHeader: changeEntries.length > 0,
    changeRows: changeEntries.map((entry) => ({
      entry,
      collapsed: folded && isCollapsible(entry),
    })),
    changesCount: folded ? collapsible : null,
  };
}
