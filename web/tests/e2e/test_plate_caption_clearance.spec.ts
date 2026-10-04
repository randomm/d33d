/**
 * E2E: first-run "Add a photo" button vs plate caption clearance (issue #347).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_plate_caption_clearance.spec.ts`
 *
 * The ticket's acceptance criterion: at each of the four gate viewports
 * (1024 × 640, 1280 × 800, 1440 × 900, 1920 × 1080 — the two docked and the
 * two floating conversation-pane layouts, split at
 * CONVERSATION_DOCK_MAX_HEIGHT_PX = 900, strict less-than) the plate caption
 * (plate-caption, in PlateBackdrop's independently-centred column, canvas
 * layer z-index 0) must NOT intersect the first-run photo button
 * (first-run-photo-btn, in the FirstRun card at z-index 10) AND must clear
 * it by at least 8px (MIN_CLEARANCE_PX).
 *
 * Why these two elements: the plate backdrop is the only non-card element in
 * the same centred band. The card's photo button and photo hint are its two
 * lowest rows, sitting at the bottom of the card's centred column — exactly
 * where the plate column (plate SVG, caption, note) terminates. The plate
 * caption is the element in that bottom band (the caption's text is the
 * plate's dimension string, and the spec's named pair is caption vs button).
 *
 * Why "gap or separation" (not a fixed vertical order): the plate column
 * and the card are two INDEPENDENTLY centred flex columns. Depending on
 * viewport and card height the caption can sit above the button (caption
 * bottom + 8px ≤ button top) or the button can sit above the caption (button
 * bottom + 8px ≤ caption top) — both are clearances. Asserting one fixed
 * order would false-fail a re-arrangement that still clears. The invariant
 * the ticket protects is "no intersection AND ≥ 8px between the boxes",
 * and gap = max(0, …) is exactly that: gap ≥ MIN_CLEARANCE_PX passes
 * iff the boxes are separated by at least 8px in either order, and
 * intersects fail with gap 0.
 *
 * The "both boxes present" precondition (a distinct failure, not a
 * geometric one): a missing plate-caption means the envelope fetch
 * failed/timed out; a missing photo button means a leftover version from a
 * prior run unmounted the first-run card. Both fail LOUDLY here instead of
 * passing by envelope-size coincidence (a missing box would otherwise make
 * any "no intersection" reading trivially true).
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

/** The required clearance between the plate caption's box and the photo
 *  button's box (issue #347 acceptance criterion). */
const MIN_CLEARANCE_PX = 8;

/** Read both elements' bounding boxes (real getBoundingClientRect) in a
 *  single evaluate() so the measurement is atomic. */
async function measureClearance(page: Page): Promise<{
  btnTop: number;
  btnBottom: number;
  captionTop: number;
  captionBottom: number;
}> {
  return page.evaluate(() => {
    const btn = document.querySelector<HTMLElement>(
      '[data-testid="first-run-photo-btn"]',
    );
    const caption = document.querySelector<HTMLElement>(
      '[data-testid="plate-caption"]',
    );
    if (!btn || !caption) {
      throw new Error(
        "missing first-run-photo-btn or plate-caption — both boxes must exist before measuring clearance",
      );
    }
    const btnRect = btn.getBoundingClientRect();
    const captionRect = caption.getBoundingClientRect();
    return {
      btnTop: btnRect.top,
      btnBottom: btnRect.bottom,
      captionTop: captionRect.top,
      captionBottom: captionRect.bottom,
    };
  });
}

for (const { w, h } of VIEWPORTS) {
  test(`plate caption clears the photo button by ${MIN_CLEARANCE_PX}px at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition (the "both boxes present" guard): the plate caption must
    // be visible — it is absent until GET /api/config/envelope resolves —
    // and the first-run photo button must be present (a leftover version
    // from a prior run unmounts the first-run card). Either missing fails
    // LOUDLY here as a precondition violation, not as a passing geometry.
    const caption = page.getByTestId("plate-caption");
    await expect
      .soft(
        caption,
        `PRECONDITION (issue #347, ${w}x${h}): plate caption must be visible before measuring — a missing caption means the envelope fetch failed or timed out`,
      )
      .toBeVisible();

    const btn = page.getByTestId("first-run-photo-btn");
    await expect
      .soft(
        btn,
        `PRECONDITION (issue #347, ${w}x${h}): photo button must be present before measuring — a missing button means a leftover version unmounted the first-run card`,
      )
      .toBeVisible();

    const { btnTop, btnBottom, captionTop, captionBottom } =
      await measureClearance(page);

    // Gap in either order (see the header for why order-agnostic): the
    // positive value, if any, of the space between the two boxes.
    const gap = Math.max(
      0,
      Math.max(captionBottom - btnBottom, btnBottom - captionTop),
    );

    // No intersection AND ≥ MIN_CLEARANCE_PX of separation — measured in
    // real geometry, no tolerance on the margin itself (a 2px slack here
    // would accept a 6px real clearance).
    expect(
      gap,
      `at ${w}x${h} the plate caption (top=${captionTop.toFixed(1)}px, bottom=${captionBottom.toFixed(1)}px) must clear the photo button (top=${btnTop.toFixed(1)}px, bottom=${btnBottom.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured clearance is ${gap.toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);
  });
}
