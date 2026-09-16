/**
 * E2E: single-click region picking on the 3D viewport (issue #98).
 *
 * Operator flow under test:
 *   1. The app auto-creates a project on mount and loads the version
 *      timeline. The named-module GLB fixture is decoded and handed to the
 *      three.js ModelViewer; once the mesh is loaded the pick layer
 *      (data-testid="viewer-pick-layer") flips its `data-ready` attribute
 *      to "true" (picking is live).
 *   2. The operator makes ONE click on the model. The pick raycast either:
 *        - hits geometry -> a red marker dot appears (data-testid=
 *          "viewer-pick-marker") and the inline region-edit bar
 *          (data-testid="region-edit-bar") opens, OR
 *        - misses geometry (a click on empty background) -> the
 *          selection-notice ("Click on the model to point at a part.")
 *          is shown and NO marker/bar appears.
 *   3. The spec asserts the bar OR notice appears.
 *
 * Determinism & WebGL policy (per issue #98):
 *   - No LLM calls, no render worker, no page.route interception — the
 *     pick path is purely client-side (fixture GLB + three.js raycast).
 *   - The pick layer only accepts clicks once the fixture model has
 *     loaded (`data-ready` flips to "true" at exactly that moment).
 *     Waiting on the attribute is a deterministic wait that does NOT
 *     depend on the WebGL raycast result.
 *   - The spec deliberately does NOT assert WHICH surface appears (bar
 *     vs selection notice) — the outcome depends on whether the raycast
 *     hits the fixture geometry, which is WebGL-dependent (headless
 *     SwiftShader). Per the issue's out-of-scope note ("assert the
 *     response surface, not the 3D pick"), we assert that one of the two
 *     surfaces appears.
 *
 * Selectors used (all in App.tsx / PickLayer.tsx):
 *   - data-testid="app-shell"              (app mounted)
 *   - data-testid="viewer-pane"            (the right-pane 3D viewport)
 *   - data-testid="viewer-pick-layer"      (the pick surface; data-ready)
 *   - data-testid="viewer-pick-marker"     (the red marker dot)
 *   - data-testid="region-edit-bar"        (pick hit geometry -> bar)
 *   - data-testid="selection-notice"       (pick missed -> notice)
 */

import { expect, test, type Page } from "@playwright/test";

/**
 * Wait for the pick layer to become live.
 *
 * PickLayer renders `data-ready={ready}`; App.tsx flips `ready` to true at
 * exactly the moment the fixture model finishes loading (ModelViewer's
 * onReady re-fires with a non-null modelRoot). Polling the attribute is a
 * deterministic wait that does NOT depend on the WebGL raycast outcome.
 */
async function waitForPickLayerReady(page: Page): Promise<void> {
  await expect
    .poll(
      async () => {
        return (
          await page
            .locator('[data-testid="viewer-pick-layer"]')
            .getAttribute("data-ready")
        ) === "true";
      },
      { timeout: 20_000, message: "pick layer did not become ready (model load timeout)" },
    )
    .toBeTruthy();
}

/**
 * Make a SINGLE click in the centre of the pick layer. A single click is
 * the whole interaction under #98 — no polygon, no closing click. The
 * centre is chosen so that, IF the raycast hits anything, it is the
 * fixture model (which fills most of the viewport).
 */
async function singleClickModel(page: Page): Promise<void> {
  const layer = page.locator('[data-testid="viewer-pick-layer"]');
  await layer.click();
}

test("single click on the model surfaces the region-edit bar or a selection notice", async ({
  page,
}) => {
  // Navigate to the SPA root — the app auto-creates a project on mount
  // and loads the fixture GLB into the viewer.
  await page.goto("/");

  // The app shell must mount (project creation + SPA build are both live).
  await expect(page.locator('[data-testid="app-shell"]')).toBeVisible();

  // The 3D viewport pane is present (the pick layer lives inside it).
  await expect(page.locator('[data-testid="viewer-pane"]')).toBeVisible();

  // Wait until the fixture model has loaded — the pick layer flips to
  // data-ready=true at exactly that point (deterministic wait, no
  // WebGL-raycast dependency).
  await waitForPickLayerReady(page);

  // Make the single click.
  await singleClickModel(page);

  // Assert that the region-edit bar OR the selection notice appears. The
  // spec asserts the user-visible response, not the 3D pick: either the
  // region-edit-bar (the pick hit geometry — marker + bar) or the
  // selection-notice (the pick missed — "Click on the model to point at a
  // part."). Both are the app's response to a completed pick.
  const regionBar = page.locator('[data-testid="region-edit-bar"]');
  const selectionNotice = page.locator('[data-testid="selection-notice"]');

  await expect
    .poll(
      async () => (await regionBar.count()) > 0 || (await selectionNotice.count()) > 0,
      { timeout: 10_000, message: "no region-edit bar or selection notice appeared after the single click" },
    )
    .toBeTruthy();
});
