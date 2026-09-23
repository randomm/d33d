/**
 * Shared scroll-container and rectangle-intersection helpers (issue #220).
 *
 * The photo-upload scroll-overlap spec (web/tests/e2e/test_photo_upload_scroll.spec.ts)
 * and its DOM-fixture unit test (web/src/__tests__/scroll-container.test.ts)
 * share this module so the intersection math is verified by a passing unit
 * test independent of the full e2e run.
 */

/** Find the scroll container starting from (and including) `start`,
 *  walking up the ancestor chain. Returns the first ancestor whose
 *  computed overflowY is "auto" or "scroll" AND whose scrollHeight
 *  exceeds its clientHeight. Falls back to `start` itself. */
export function findScrollContainer(start: HTMLElement): HTMLElement {
  let el: HTMLElement | null = start;
  while (el) {
    const oy = getComputedStyle(el).overflowY;
    if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight) {
      return el;
    }
    el = el.parentElement;
  }
  return start;
}

/** The maximum scrollTop for a scroll container (0 when no overflow). */
export function maxScroll(sc: HTMLElement): number {
  return sc.scrollHeight - sc.clientHeight;
}

/** Park a scroll container at `fraction` of its max scrollTop
 *  (0 = top, 1 = bottom). */
export function parkAt(sc: HTMLElement, fraction: number): void {
  sc.scrollTop = fraction * maxScroll(sc);
}

export interface Rect {
  left: number;
  top: number;
  right: number;
  bottom: number;
}

export interface IntersectionResult {
  overlapX: number;
  overlapY: number;
  detail: string;
}

/** Compute the maximum true 2D rectangle intersection between `label`
 *  and every rect in `cards`. A card only counts if it overlaps on
 *  BOTH axes simultaneously (x > 0 AND y > 0). Among qualifying cards,
 *  the one with the largest overlap area (x * y) is reported; ties
 *  break on larger x, then larger y. Returns zero for no true
 *  intersection. */
export function maxIntersection(
  label: Rect,
  cards: ReadonlyArray<Rect>,
): IntersectionResult {
  let bestX = 0;
  let bestY = 0;
  let bestArea = 0;
  let detail = "";
  for (const c of cards) {
    const x = Math.min(label.right, c.right) - Math.max(label.left, c.left);
    const y = Math.min(label.bottom, c.bottom) - Math.max(label.top, c.top);
    if (x > 0 && y > 0) {
      const area = x * y;
      if (area > bestArea || (area === bestArea && (x > bestX || (x === bestX && y > bestY)))) {
        bestX = x;
        bestY = y;
        bestArea = area;
        detail = `card [${c.left.toFixed(1)},${c.top.toFixed(1)} → ${c.right.toFixed(1)},${c.bottom.toFixed(1)}] vs label [${label.left.toFixed(1)},${label.top.toFixed(1)} → ${label.right.toFixed(1)},${label.bottom.toFixed(1)}]`;
      }
    }
  }
  return { overlapX: bestX, overlapY: bestY, detail };
}
