/**
 * E2E: first-run "Add a photo" button + hint vs plate caption clearance
 * (issue #347; extended for issue #390 item 3 — the QA 2026-10-04 caption
 * overlap, and its binding operator decision 3: the assertion covers
 * plate-caption against BOTH first-run-photo-btn AND first-run-photo-hint,
 * at 1280, 1440 and 1920).
 *
 * MANUAL-ONLY — NOT run by CI. Run with:
 *   cd web && npx playwright test test_plate_caption_clearance.spec.ts
 *
 * The ticket's acceptance criterion: at each of the four gate viewports
 * (1024 × 640, 1280 × 800, 1440 × 900, 1920 × 1080 — the two docked and the
 * two floating conversation-pane layouts, split at
 * CONVERSATION_DOCK_MAX_HEIGHT_PX = 900, strict less-than) the plate caption
 * (plate-caption, in PlateBackdrop's independently-centred column, canvas
 * layer z-index 0) must NOT intersect the first-run photo button
 * (first-run-photo-btn) OR the photo hint line (first-run-photo-hint, the
 * row directly below the button in the FirstRun card at z-index 10), and
 * must clear BOTH by at least 8px (MIN_CLEARANCE_PX).
 *
 * Why the hint as well as the button (issue #390, operator decision 3): the
 * hint sits one row under the button in the card's centred column, so it is
 * the element whose box sits nearest the plate caption's band. A caption
 * that clears the button by 8px can still kiss the hint line a row below —
 * the overlap the 2026-10-04 QA observed. Both rows are asserted in the
 * same atomic measurement so the gate protects the full photo line, not
 * only its top edge.
 *
 * Why "gap or separation" (not a fixed vertical order): the plate column
 * and the card are two INDEPENDENTLY centred flex columns. Depending on
 * viewport and card height the caption can sit above the button (caption
 * bottom + 8px ≤ button top) or the button can sit above the caption (button
 * bottom + 8px ≤ caption top) — both are clearances. Asserting one fixed
 * order would false-fail a re-arrangement that still clears. The invariant
 * the ticket protects is "no intersection AND ≥ 8px between the boxes",
 * and gap = max(0, captionTop − elBottom, elTop − captionBottom) is exactly
 * that: gap ≥ MIN_CLEARANCE_PX passes iff the boxes are separated by at
 * least 8px in either order, and intersects (or touch) fail with gap 0.
 *
 * The "all boxes present" precondition (a distinct failure, not a
 * geometric one): a missing plate-caption means the envelope fetch
 * failed/timed out; a missing photo button or hint means a leftover version
 * from a prior run unmounted the first-run card. Any missing box fails
 * LOUDLY here instead of passing by envelope-size coincidence (a missing
 * box would otherwise make any "no intersection" reading trivially true).
 *
 * Geometry is measured in real getBoundingClientRect (not the rounded
 * boundingBox()) in a single evaluate() so the measurement is atomic, and
 * the spec is MANUAL-ONLY under the issue #207 guard in playwright.config
 * (never runs against the operator's live server without D33D_DATA_DIR).
 */

import { expect, test, type Page } from "@playwright/test";

const VIEWPORTS: ReadonlyArray<{ w: number; h: number }> = [
  { w: 1024, h: 640 },
  { w: 1280, h: 800 },
  { w: 1440, h: 900 },
  { w: 1920, h: 1080 },
];

/** The required clearance between the plate caption's box and each photo
 *  line element's box (issue #347 / #390 acceptance criterion). */
const MIN_CLEARANCE_PX = 8;

interface Rect {
  top: number;
  bottom: number;
}

/** Read the plate caption's and both photo-line elements' bounding boxes
 *  (real getBoundingClientRect) in a single evaluate() so the measurement
 *  is atomic. Throws if any of the three boxes is missing (the precondition
 *  is enforced in the caller's soft-expect too, so a missing box is never
 *  a silent pass). */
async function measureClearance(page: Page): Promise<{
  caption: Rect;
  btn: Rect;
  hint: Rect;
}> {
  return page.evaluate(() => {
    const caption = document.querySelector<HTMLElement>('[data-testid="plate-caption"]');
    const btn = document.querySelector<HTMLElement>('[data-testid="first-run-photo-btn"]');
    const hint = document.querySelector<HTMLElement>('[data-testid="first-run-photo-hint"]');
    if (!caption) {
      throw new Error(
        "missing plate-caption — the envelope fetch failed or timed out; both boxes must exist before measuring clearance",
      );
    }
    if (!btn) {
      throw new Error(
        "missing first-run-photo-btn — a leftover version unmounted the first-run card; the box must exist before measuring clearance",
      );
    }
    if (!hint) {
      throw new Error(
        "missing first-run-photo-hint — a leftover version unmounted the first-run card; the box must exist before measuring clearance",
      );
    }
    const captionRect = caption.getBoundingClientRect();
    const btnRect = btn.getBoundingClientRect();
    const hintRect = hint.getBoundingClientRect();
    return {
      caption: { top: captionRect.top, bottom: captionRect.bottom },
      btn: { top: btnRect.top, bottom: btnRect.bottom },
      hint: { top: hintRect.top, bottom: hintRect.bottom },
    };
  });
}

/** The positive separation between two vertical boxes, in either order —
 *  the gap the boxes are apart, or 0 when they intersect or touch. */
function gap(a: Rect, b: Rect): number {
  return Math.max(0, a.top - b.bottom, b.top - a.bottom);
}

for (const { w, h } of VIEWPORTS) {
  test(`plate caption clears the photo button AND hint by ${MIN_CLEARANCE_PX}px at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition (the "all boxes present" guard): the plate caption must
    // be visible — it is absent until GET /api/config/envelope resolves —
    // and the first-run photo button AND hint must be present (a leftover
    // version from a prior run unmounts the first-run card). Any missing
    // box fails LOUDLY here as a precondition violation, not as a passing
    // geometry (a missing box would otherwise make "no intersection"
    // trivially true).
    await expect
      .soft(
        page.getByTestId("plate-caption"),
        `PRECONDITION (issue #347/#390, ${w}x${h}): plate caption must be visible before measuring — a missing caption means the envelope fetch failed or timed out`,
      )
      .toBeVisible();

    await expect
      .soft(
        page.getByTestId("first-run-photo-btn"),
        `PRECONDITION (issue #347/#390, ${w}x${h}): photo button must be present before measuring — a missing button means a leftover version unmounted the first-run card`,
      )
      .toBeVisible();

    await expect
      .soft(
        page.getByTestId("first-run-photo-hint"),
        `PRECONDITION (issue #347/#390, ${w}x${h}): photo hint must be present before measuring — a missing hint means a leftover version unmounted the first-run card`,
      )
      .toBeVisible();

    const { caption, btn, hint } = await measureClearance(page);

    // The caption must clear the button by at least MIN_CLEARANCE_PX.
    const btnGap = gap(caption, btn);
    expect(
      btnGap,
      `at ${w}x${h} the plate caption (top=${caption.top.toFixed(1)}px, bottom=${caption.bottom.toFixed(1)}px) must clear the photo button (top=${btn.top.toFixed(1)}px, bottom=${btn.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${btnGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);

    // AND the hint line (the row below the button) — the box nearest the
    // caption's band (issue #390, operator decision 3).
    const hintGap = gap(caption, hint);
    expect(
      hintGap,
      `at ${w}x${h} the plate caption (top=${caption.top.toFixed(1)}px, bottom=${caption.bottom.toFixed(1)}px) must clear the photo hint (top=${hint.top.toFixed(1)}px, bottom=${hint.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${hintGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);
  });
}
