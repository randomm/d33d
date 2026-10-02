/**
 * briefRowHelpers — the row-identity, label, testid, and mark helpers
 * shared by Brief.tsx (the list) and BriefRow.tsx (one row). Extracted so
 * the two modules do not duplicate the helpers (issue #338: the two-zone
 * split must not grow Brief.tsx; the row + its helpers live alongside it).
 */

import copy, { mm } from "../../copy";
import type { DesignStateEntry } from "../../lib/api";

/** The provenance mark styles (issue #246 assumed half-dot; #274 measured
 *  mark for a within-threshold model disagreement). */
export const MARKS = {
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
  /** Half-filled dot, faint (issue #246): the top half is filled in the
   *  faint token, the bottom half is transparent — a dot with a value in
   *  it, but a value nobody said. Rendered with a linear-gradient so the
   *  "half" is exact and the whole mark stays one element. */
  assumed: {
    width: 8,
    height: 8,
    borderRadius: "50%",
    border: "1px solid var(--color-faint)",
    background: "linear-gradient(to bottom, var(--color-faint) 0%, var(--color-faint) 50%, transparent 50%, transparent 100%)",
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

/** The row label — a parameter with no human label uses its NAME as the
 *  label. An AXIS row (`kind: "axis"` — the dimension protocol's own W/D/H
 *  axis, never a parameter) renders its axis word from `copy.brief.axisLabel`,
 *  not the bare letter: the row the user stated. The `label || name` fallback
 *  stays for defensive honesty (a missing label falls back to the name, never
 *  an invented string). */
export function rowLabel(entry: DesignStateEntry): string {
  if (entry.kind === "axis") return copy.brief.axisLabel[entry.name] ?? entry.name;
  return entry.label || entry.name;
}

/** The row's stable identity: `kind`+`name` — `name` alone is NOT unique
 *  within a block (a param row and an axis row can both be named `W`), so
 *  the key, the testid, and the expanded state are all keyed off
 *  `kind:name` (issue #246 review: duplicate identity for W/D/H rows). */
export function rowIdentity(entry: DesignStateEntry): string {
  return `${entry.kind}:${entry.name}`;
}

/** The row's `data-testid`: param rows keep `brief-row-<name>` (existing
 *  tests and consumers), axis rows use `brief-row-axis-<name>` — the two
 *  can never collide, even for the same letter. */
export function rowTestId(entry: DesignStateEntry): string {
  return entry.kind === "axis" ? `brief-row-axis-${entry.name}` : `brief-row-${entry.name}`;
}

/** Format a design-state value for the value cell. `null` must never be
 *  formatted — the unknown cell is the control, and a `?? 0` / `String()`
 *  default here would re-introduce issue #91's null→0 defect. */
export function formatValue(value: number | string | boolean | null): string | null {
  if (value === null) return null;
  if (typeof value === "number") return mm(value);
  return String(value);
}

/** The primary displayed value for a row: `disagrees` shows the MEASURED
 *  value (that is what will print), everything else shows its own value. */
export function primaryValue(entry: DesignStateEntry): number | string | boolean | null {
  if (entry.provenance === "disagrees") return entry.value;
  return entry.value;
}
