/**
 * E2E: plate caption and note are hidden during first run (issue #434).
 *
 * MANUAL-ONLY — NOT run by CI, not a merge gate (vitest is the gate). Run with:
 *   cd web && npx playwright test test_plate_caption_clearance.spec.ts
 *
 * The first-run card owns the centre; the plate caption and note are its
 * neighbours and must not fragment between its two cards. The plate
 * drawing (plate-backdrop-svg) stays mounted. Precondition: the envelope
 * has resolved (plate-backdrop-svg visible) so absence is not vacuous.
 */

import { expect, test } from "@playwright/test";

const VIEWPORTS: ReadonlyArray<{ w: number; h: number }> = [
  { w: 1024, h: 640 },
  { w: 1280, h: 800 },
  { w: 1440, h: 900 },
  { w: 1920, h: 1080 },
];

for (const { w, h } of VIEWPORTS) {
  test(`plate caption and note are hidden during first run at ${w}x${h}`, async ({ page }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition: the plate drawing is up (the envelope resolved) and the
    // first-run photo button is present.
    await expect
      .soft(
        page.getByTestId("plate-backdrop-svg"),
        `PRECONDITION (issue #434, ${w}x${h}): plate drawing must be visible — a missing plate means the envelope fetch failed`,
      )
      .toBeVisible();
    await expect
      .soft(
        page.getByTestId("first-run-photo-btn"),
        `PRECONDITION (issue #434, ${w}x${h}): photo button must be present — a missing button means the first-run card is not up`,
      )
      .toBeVisible();

    await expect(page.getByTestId("plate-caption")).toHaveCount(0);
    await expect(page.getByTestId("plate-note")).toHaveCount(0);
  });
}
