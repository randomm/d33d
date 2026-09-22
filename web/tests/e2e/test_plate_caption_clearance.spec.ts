/**
 * E2E: first-run photo hint vs plate caption vertical clearance (issue #214).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_plate_caption_clearance.spec.ts`
 *
 * The ticket's acceptance criterion: at each of the enumerated viewports
 * (1024 × 640 — the documented responsive floor, 1280 × 720 — where the
 * 4px overlap was originally measured, 1280 × 900) the photo hint's bottom
 * edge (first-run-photo-hint, last child of the FirstRun card) must sit at
 * least 8px ABOVE the plate caption's top edge (plate-caption, in
 * PlateBackdrop's independently-centred column). Two elements in two
 * separate layout trees, so the 8px clearance must be measured in real
 * geometry — getBoundingClientRect — not a bare "not overlapping" check.
 *
 * Setup is a bare page.goto("/"): the SPA auto-creates a fresh project on
 * load (first-run screen present) and the live backend always serves
 * verified:true for GET /api/config/envelope (issue #134), so the plate
 * caption is always the short confirmed form — no envelope-route
 * interception is needed. Before measuring, the spec asserts plate-caption
 * is visible (a distinct precondition failure) so a failed/slow envelope
 * fetch — or a leftover version from a prior run that unmounted
 * first-run — fails as a precondition violation, not a confusing geometry
 * failure.
 */

import { expect, test, type Page } from "@playwright/test";

const VIEWPORTS: ReadonlyArray<{ w: number; h: number }> = [
  { w: 1024, h: 640 },
  { w: 1280, h: 720 },
  { w: 1280, h: 900 },
];

/** The required clearance between the photo hint's bottom edge and the
 *  plate caption's top edge (issue #214 acceptance criterion). */
const MIN_CLEARANCE_PX = 8;

/** Read both elements' bounding boxes (real getBoundingClientRect, not
 *  boundingBox() — the latter is already rounded) in a single
 * evaluate() so the measurement is atomic. */
async function measureClearance(page: Page): Promise<{
  hintBottom: number;
  captionTop: number;
}> {
  return page.evaluate(() => {
    const hint = document.querySelector<HTMLElement>(
      '[data-testid="first-run-photo-hint"]',
    );
    const caption = document.querySelector<HTMLElement>(
      '[data-testid="plate-caption"]',
    );
    if (!hint || !caption) {
      throw new Error(
        "missing first-run-photo-hint or plate-caption — both must exist before measuring clearance",
      );
    }
    const hintRect = hint.getBoundingClientRect();
    const captionRect = caption.getBoundingClientRect();
    return { hintBottom: hintRect.bottom, captionTop: captionRect.top };
  });
}

for (const { w, h } of VIEWPORTS) {
  test(`first-run photo hint clears the plate caption by ${MIN_CLEARANCE_PX}px at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition: the plate caption must be visible. This covers
    // (a) a failed/slow GET /api/config/envelope (the caption never
    // mounts) and (b) a leftover version from a prior run that
    // unmounted the first-run screen — both fail here as a
    // precondition violation, not as a geometry failure.
    const caption = page.getByTestId("plate-caption");
    await expect
      .soft(
        caption,
        `PRECONDITION (issue #214, ${w}x${h}): plate caption must be visible before measuring — a missing caption means the envelope fetch failed, timed out, or a prior run left a version behind that unmounted the first-run screen`,
      )
      .toBeVisible();

    const { hintBottom, captionTop } = await measureClearance(page);

    // The strict acceptance criterion: the photo hint's bottom edge must
    // be at least MIN_CLEARANCE_PX above the plate caption's top edge.
    // No tolerance on the margin itself — a 2px slack here would accept
    // a 6px real clearance.
    expect(
      captionTop - hintBottom,
      `at ${w}x${h} the photo hint (bottom=${hintBottom.toFixed(1)}px) must clear the plate caption (top=${captionTop.toFixed(1)}px) by at least ${MIN_CLEARANCE_PX}px — measured gap is ${(captionTop - hintBottom).toFixed(1)}px`,
    ).toBeGreaterThanOrEqual(MIN_CLEARANCE_PX);
  });
}
