/**
 * E2E: first-run "Add a photo" button + hint vs plate caption AND note
 * clearance (issue #347; extended for issue #390 item 3 — the QA 2026-10-04
 * caption overlap, and its binding operator decision 3: the assertion covers
 * plate-caption against BOTH first-run-photo-btn AND first-run-photo-hint,
 * at 1280, 1440 and 1920; extended for issue #415: the plate note is added
 * as a fourth box — the note sits directly below the caption in the plate
 * column and is the element whose bottom edge was kissing the photo button
 * at 1920×1080 with the 44vh cap).
 *
 * MANUAL-ONLY — NOT run by CI. Run with:
 *   cd web && npx playwright test test_plate_caption_clearance.spec.ts
 *
 * The ticket's acceptance criterion: at each of the four gate viewports
 * (1024 × 640, 1280 × 800, 1440 × 900, 1920 × 1080 — the two docked and the
 * two floating conversation-pane layouts, split at
 * CONVERSATION_DOCK_MAX_HEIGHT_PX = 900, strict less-than) the plate caption
 * (plate-caption) AND the plate note (plate-note, the line directly below
 * the caption in PlateBackdrop's independently-centred column, canvas layer
 * z-index 0) must NOT intersect the first-run photo button
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

import { expect, test } from "@playwright/test";

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

/** The positive separation between two vertical boxes, in either order —
 *  the gap the boxes are apart, or 0 when they intersect or touch. */
function gap(a: Rect, b: Rect): number {
  return Math.max(0, a.top - b.bottom, b.top - a.bottom);
}

for (const { w, h } of VIEWPORTS) {
  test(`plate caption AND note clear the photo button AND hint by ${MIN_CLEARANCE_PX}px at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition (the "all boxes present" guard): the plate caption and
    // note must be visible — they are absent until GET /api/config/envelope
    // resolves — and the first-run photo button AND hint must be present
    // (a leftover version from a prior run unmounts the first-run card).
    // Any missing box fails LOUDLY here as a precondition violation, not as
    // a passing geometry (a missing box would otherwise make "no
    // intersection" trivially true).
    await expect
      .soft(
        page.getByTestId("plate-caption"),
        `PRECONDITION (issue #347/#390/#415, ${w}x${h}): plate caption must be visible before measuring — a missing caption means the envelope fetch failed or timed out`,
      )
      .toBeVisible();

    await expect
      .soft(
        page.getByTestId("plate-note"),
        `PRECONDITION (issue #415, ${w}x${h}): plate note must be visible before measuring — a missing note means the envelope fetch failed or timed out`,
      )
      .toBeVisible();

    await expect
      .soft(
        page.getByTestId("first-run-photo-btn"),
        `PRECONDITION (issue #347/#390/#415, ${w}x${h}): photo button must be present before measuring — a missing button means a leftover version unmounted the first-run card`,
      )
      .toBeVisible();

    await expect
      .soft(
        page.getByTestId("first-run-photo-hint"),
        `PRECONDITION (issue #347/#390/#415, ${w}x${h}): photo hint must be present before measuring — a missing hint means a leftover version unmounted the first-run card`,
      )
      .toBeVisible();

    // Measure all four boxes in a single evaluate() so the measurement is
    // atomic (no reflow between reads).
    const boxes = await page.evaluate(() => {
      const sel = (testId: string) => {
        const el = document.querySelector<HTMLElement>(`[data-testid="${testId}"]`);
        if (!el) return null;
        const r = el.getBoundingClientRect();
        return { top: r.top, bottom: r.bottom } as Rect;
      };
      return {
        caption: sel("plate-caption"),
        note: sel("plate-note"),
        btn: sel("first-run-photo-btn"),
        hint: sel("first-run-photo-hint"),
      };
    });

    const { caption, note, btn, hint } = boxes;
    expect(caption, `plate caption box missing at ${w}x${h}`).not.toBeNull();
    expect(note, `plate note box missing at ${w}x${h}`).not.toBeNull();
    expect(btn, `photo button box missing at ${w}x${h}`).not.toBeNull();
    expect(hint, `photo hint box missing at ${w}x${h}`).not.toBeNull();

    // The caption must clear the button by at least MIN_CLEARANCE_PX.
    const capBtnGap = gap(caption!, btn!);
    expect(
      capBtnGap,
      `at ${w}x${h} the plate caption (top=${caption!.top.toFixed(1)}px, bottom=${caption!.bottom.toFixed(1)}px) must clear the photo button (top=${btn!.top.toFixed(1)}px, bottom=${btn!.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${capBtnGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);

    // AND the hint line (the row below the button) — the box nearest the
    // caption's band (issue #390, operator decision 3).
    const capHintGap = gap(caption!, hint!);
    expect(
      capHintGap,
      `at ${w}x${h} the plate caption (top=${caption!.top.toFixed(1)}px, bottom=${caption!.bottom.toFixed(1)}px) must clear the photo hint (top=${hint!.top.toFixed(1)}px, bottom=${hint!.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${capHintGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);

    // The note (directly below the caption) must also clear the button and
    // hint (issue #415: the note's bottom edge was the actual overlap at
    // 1920×1080 with the 44vh cap).
    const noteBtnGap = gap(note!, btn!);
    expect(
      noteBtnGap,
      `at ${w}x${h} the plate note (top=${note!.top.toFixed(1)}px, bottom=${note!.bottom.toFixed(1)}px) must clear the photo button (top=${btn!.top.toFixed(1)}px, bottom=${btn!.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${noteBtnGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);

    const noteHintGap = gap(note!, hint!);
    expect(
      noteHintGap,
      `at ${w}x${h} the plate note (top=${note!.top.toFixed(1)}px, bottom=${note!.bottom.toFixed(1)}px) must clear the photo hint (top=${hint!.top.toFixed(1)}px, bottom=${hint!.bottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${noteHintGap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);
  });
}
