/**
 * E2E: lasso selection on the dimension canvas (issue #49, spec 5 of 5).
 *
 * Operator flow under test:
 *   1. The app auto-creates a project on mount and loads the version
 *      timeline. The named-module GLB fixture is decoded and handed to the
 *      three.js ModelViewer; once the mesh is loaded, the
 *      `ViewportLassoOverlay` (data-testid="viewport-lasso-overlay")
 *      becomes enabled (it is `disabled` while no model is loaded).
 *   2. The operator draws a lasso by clicking >=3 points on the overlay.
 *      Closing the polygon (a click near the first vertex) calls
 *      `onLassoCompleted`, which raycasts the polygon through the live
 *      viewport and either:
 *        - resolves to a module id -> stores the pending selection and
 *          surfaces `data-testid="pending-selection-notice"`, or
 *        - hits nothing / fails -> surfaces `data-testid="selection-notice"`
 *          with a "nothing selected" / "selection failed" message.
 *   3. The spec asserts the notice (pending OR selection) appears.
 *
 * Determinism & WebGL policy (per issue #49):
 *   - No LLM calls, no render worker, no page.route interception — the
 *     lasso path is purely client-side (fixture GLB + three.js raycast).
 *   - The lasso overlay only accepts clicks once the fixture model has
 *     loaded (moduleGroup !== null). We wait for the overlay's
 *     `pointer-events: auto` style to flip, which happens exactly when
 *     the model finishes loading — this is a deterministic wait that does
 *     NOT depend on WebGL raycast results.
 *   - The spec deliberately does NOT assert WHICH notice appears (pending
 *     vs selection) — the outcome depends on whether the raycast hits the
 *     fixture geometry, which is WebGL-dependent (headless SwiftShader).
 *     Per the issue's out-of-scope note ("assert the notice, not the 3D
 *     pick"), we assert that one of the two notices appears.
 *
 * Selectors used (all pre-existing in App.tsx / ViewportLassoOverlay.tsx):
 *   - data-testid="app-shell"                 (app mounted)
 *   - data-testid="viewer-pane"               (the right-pane 3D viewport)
 *   - data-testid="viewport-lasso-overlay"    (the lasso surface)
 *   - data-testid="pending-selection-notice"  (lasso resolved to a module)
 *   - data-testid="selection-notice"          (lasso hit nothing / failed)
 */

import { expect, test, type Page } from "@playwright/test";

/**
 * Wait for the lasso overlay to become interactive.
 *
 * ViewportLassoOverlay sets `pointerEvents: "auto"` and `cursor:
 * "crosshair"` when `disabled` is false (i.e. the fixture model has
 * loaded). While disabled it is `pointer-events: none` and clicks are
 * ignored (the handler early-returns). Polling the computed style is a
 * deterministic wait that does not depend on the WebGL raycast outcome.
 */
async function waitForLassoActive(page: Page): Promise<void> {
  await expect
    .poll(
      async () => {
        return (
          await page.locator('[data-testid="viewport-lasso-overlay"]').evaluate((el) =>
            getComputedStyle(el).pointerEvents,
          )
        ) === "auto";
      },
      { timeout: 20_000, message: "lasso overlay did not become active (model load timeout)" },
    )
    .toBeTruthy();
}

/**
 * Draw a lasso polygon on the overlay by clicking points in viewport
 * coordinates (CSS pixels relative to the overlay's bounding rect).
 *
 * The polygon is a small triangle well inside the viewport so that, IF the
 * raycast hits anything, it is the fixture model (which fills most of the
 * viewport). The exact coordinates do not matter for the assertion — the
 * spec asserts the notice, not the pick — but the points must be inside the
 * overlay's rect so the click handler records them (points outside the rect
 * still register as negative/huge coords, which is fine for closing but
 * would not hit geometry if we ever asserted the pick).
 *
 * Closing: a 4th click within CLOSE_THRESHOLD_PX (12px) of the first vertex
 * closes the polygon and fires onLassoCompleted.
 */
async function drawLasso(page: Page): Promise<void> {
  const overlay = page.locator('[data-testid="viewport-lasso-overlay"]');
  const box = await overlay.boundingBox();
  if (!box) {
    throw new Error("viewport-lasso-overlay has no bounding box (not visible)");
  }

  // Viewport-relative points (CSS px, relative to the overlay's top-left).
  // A compact triangle in the upper-middle of the viewport.
  const p1 = { x: box.width / 2 - 40, y: box.height / 2 - 40 };
  const p2 = { x: box.width / 2 + 40, y: box.height / 2 - 40 };
  const p3 = { x: box.width / 2, y: box.height / 2 + 40 };

  // Place the three vertices.
  await overlay.click({ position: p1 });
  await overlay.click({ position: p2 });
  await overlay.click({ position: p3 });

  // Closing click near the first vertex (within the 12px threshold).
  await overlay.click({ position: { x: p1.x + 2, y: p1.y + 2 } });
}

test("lasso selection surfaces a notice after drawing a polygon", async ({ page }) => {
  // Navigate to the SPA root — the app auto-creates a project on mount
  // and loads the fixture GLB into the viewer.
  await page.goto("/");

  // The app shell must mount (project creation + SPA build are both live).
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible();

  // The 3D viewport pane is present (the lasso overlay lives inside it).
  await expect(page.locator('[data-testid="viewer-pane"]')).toBeVisible();

  // Wait until the fixture model has loaded — the overlay flips to
  // pointer-events: auto at exactly that point (deterministic wait, no
  // WebGL-raycast dependency).
  await waitForLassoActive(page);

  // Draw the lasso polygon (3 vertices + a closing click).
  await drawLasso(page);

  // Assert that a notice appears. The spec asserts the NOTICE, not the 3D
  // pick: either the pending-selection-notice (lasso resolved to a module
  // id) or the selection-notice (lasso hit nothing / selection failed).
  // Both are the app's user-visible response to a completed lasso.
  const pendingNotice = page.locator('[data-testid="pending-selection-notice"]');
  const selectionNotice = page.locator('[data-testid="selection-notice"]');

  await expect
    .poll(
      async () => (await pendingNotice.count()) > 0 || (await selectionNotice.count()) > 0,
      { timeout: 10_000, message: "no lasso notice appeared after drawing a polygon" },
    )
    .toBeTruthy();
});
