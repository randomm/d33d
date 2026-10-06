/**
 * Shared id → timeline-ordinal map (issue #387).
 *
 * The ordinal is the 1-based position of a version in the loaded timeline
 * list — the number every user-facing "vN" label must show, as established
 * by the Filmstrip (issue #352). Computing it once here and sharing it
 * across every label site (PassCard, BranchGraph, Filmstrip, export
 * fallbacks) is what keeps them from drifting: the divergence the QA 2026-10-04
 * review found ("pass card shows v64 while the history shows v1") happened
 * because each site derived its own number from the DB row id.
 *
 * Semantics (must match Filmstrip's #352 map exactly):
 * - 1-based position in the oldest-first list;
 * - FIRST occurrence wins on a duplicate id (identical to `findIndex`): a
 *   later slot must not override the earlier ordinal.
 *
 * Only the label text changes — every internal id (data-testids, keys,
 * URLs, API calls) keeps using the real DB id.
 */

import type { VersionTimelineEntry } from "./api";

export function versionOrdinals(versions: VersionTimelineEntry[]): Map<number, number> {
  const ordinalById = new Map<number, number>();
  versions.forEach((v, i) => {
    if (!ordinalById.has(v.id)) {
      ordinalById.set(v.id, i + 1);
    }
  });
  return ordinalById;
}
