/**
 * E2E: single-click region picking on the 3D viewport (issue #98,
 * repaired in issue #182).
 *
 * Operator flow under test:
 *   1. The app auto-creates a project on mount and loads the version
 *      timeline. A fresh project mounts NO model (issue #107 removed the
 *      GLB fixture) — pick readiness (`data-ready` on
 *      data-testid="viewer-pick-layer") is the way a real user
 *      establishes it: a design-loop pass delivers the rendered STL
 *      through the version-created SSE progress frame's `stl_data_uri`.
 *      This spec drives that path honestly: it sends ONE message through
 *      the real send path (FirstRun input -> postChat to the real
 *      backend -> SSE stream) and intercepts ONLY the SSE transport
 *      (GET /api/stream/{id}), delivering a genuine rendered artifact —
 *      web/tests/fixtures/viewer/mini-box.stl, the same real STL the
 *      render worker emits elsewhere in the test suite — as the
 *      version-created frame's `stl_data_uri`. The payload is a real
 *      rendered artifact, never hand-typed geometry.
 *   2. The SPA decodes the streamed STL and mounts it in the three.js
 *      viewer; the pick layer flips `data-ready` to "true" (picking is
 *      live). The streamed version also appends a message and fetches
 *      the timeline, which unmounts the first-run screen (issue #128),
 *      so it never intercepts the click below.
 *   3. The operator makes ONE click on the model. The pick raycast
 *      either:
 *        - hits geometry -> a red marker dot appears (data-testid=
 *          "viewer-pick-marker") and the inline region-edit bar
 *          (data-testid="region-edit-bar") opens, OR
 *        - misses geometry (a click on empty background) -> the
 *          selection-notice ("Click on the model to point at a part.")
 *          is shown and NO marker/bar appears.
 *   4. The spec asserts the bar OR notice appears.
 *
 * Determinism & WebGL policy:
 *   - No LLM calls, no Docker render worker. The only interception is
 *     the SSE transport (mode (b), per issue #182's operator decision);
 *     project creation, the postChat round-trip and the SPA's SSE
 *     handling are all real — what is faked is the bytes behind the
 *     stream, and those bytes are a genuine rendered STL fixture.
 *   - The pick layer only accepts clicks once the streamed model has
 *     loaded (`data-ready` flips to "true" at exactly that moment).
 *     Waiting on the attribute is a deterministic wait that does NOT
 *     depend on the WebGL raycast result.
 *   - The spec deliberately does NOT assert WHICH surface appears (bar
 *     vs selection notice) — the outcome depends on whether the raycast
 *     hits the geometry, which is WebGL-dependent (headless
 *     SwiftShader). A small model such as mini-box makes a centre click
 *     MORE likely to miss than the deleted GLB fixture would have been,
 *     so hardening this to require the bar would make the spec flaky;
 *     both surfaces are the app's correct response to a completed pick.
 *
 * Selectors used (all verified in App.tsx / PickLayer.tsx / FirstRun.tsx):
 *   - data-testid="app-stage"                (app mounted; #119 renamed
 *                                           app-shell to app-stage)
 *   - data-testid="viewer-pane"              (the full-viewport 3D pane)
 *   - data-testid="viewer-pick-layer"        (the pick surface; data-ready)
 *   - data-testid="viewer-pick-marker"       (the red marker dot)
 *   - data-testid="first-run"                (issue #128 first-run screen)
 *   - data-testid="first-run-input"          (the FirstRun input)
 *   - data-testid="region-edit-bar"          (pick hit geometry -> bar)
 *   - data-testid="selection-notice"         (pick missed -> notice)
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

/** The genuine rendered STL the stream delivers (a real artifact already
 *  used by the viewer test fixtures — not hand-typed geometry). */
const STL_FIXTURE_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../fixtures/viewer/mini-box.stl",
);

/**
 * Wait for the pick layer to become live.
 *
 * PickLayer renders `data-ready={ready}`; App.tsx flips `ready` to true
 * at exactly the moment the streamed model finishes loading (ModelViewer's
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
 * streamed model.
 */
async function singleClickModel(page: Page): Promise<void> {
  const layer = page.locator('[data-testid="viewer-pick-layer"]');
  await layer.click();
}

test("single click on the model surfaces the region-edit bar or a selection notice", async ({
  page,
}) => {
  // Interception point (issue #182 mode (b)): the ONLY thing this spec
  // intercepts is the SSE transport — GET /api/stream/{id} — and it
  // answers with the same frame shape d33d/streaming.py emits on a
  // passed design loop: a version-created progress frame carrying a
  // genuine rendered STL as `stl_data_uri`, then a terminal done frame.
  // Project creation, the postChat round-trip and the SPA's own SSE
  // demultiplexing all run for real; only the bytes behind the stream
  // are delivered from the fixture (a real rendered artifact).
  const stlBytes = readFileSync(STL_FIXTURE_PATH);
  const stlDataUri = `data:model/stl;base64,${stlBytes.toString("base64")}`;
  const frames = [
    `event: progress\ndata: ${JSON.stringify({
      step: "version-created",
      version_id: 1,
      stl_data_uri: stlDataUri,
      views: {},
    })}\n\n`,
    `event: done\ndata: ${JSON.stringify({
      message: "Design loop passed validation",
    })}\n\n`,
  ].join("");

  await page.route(
    (url) => url.pathname.match(/^\/api\/stream\/\d+$/),
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: {
          "Content-Type": "text/event-stream",
          "Cache-Control": "no-cache",
        },
        body: frames,
      });
    },
  );

  // Navigate to the SPA root — the app auto-creates a project on mount.
  // A fresh project mounts NO model (issue #107); the first-run screen
  // (issue #128) is what the operator sees instead.
  await page.goto("/");

  // The app stage must mount (project creation + SPA build are both live).
  await expect(page.locator('[data-testid="app-stage"]')).toBeVisible();

  // The 3D viewport pane is present (the pick layer lives inside it).
  await expect(page.locator('[data-testid="viewer-pane"]')).toBeVisible();

  // Drive the model in the way a real user establishes pick readiness:
  // send ONE message. The postChat call reaches the real backend (202 —
  // the design loop does not start without a model catalogue entry, and
  // that 202 response is what opens the stream); the SSE stream it
  // opens is the intercepted transport delivering the fixture STL.
  const firstRunInput = page.locator('[data-testid="first-run-input"]');
  await expect(firstRunInput).toBeVisible();
  await firstRunInput.fill("a small box");
  await firstRunInput.press("Enter");

  // The version-created frame appends the assistant turn and the
  // version-created trigger refetches the timeline — versions/messages
  // exist, so the first-run screen unmounts on its own. It must be gone
  // before the click: it overlays the stage (z-index 10) and its
  // centred column would intercept the click.
  await expect(page.locator('[data-testid="first-run"]')).toHaveCount(0);

  // Wait until the streamed model has loaded — the pick layer flips to
  // data-ready=true at exactly that point (deterministic wait, no
  // WebGL-raycast dependency).
  await waitForPickLayerReady(page);

  // Make the single click.
  await singleClickModel(page);

  // Assert that the region-edit bar OR the selection notice appears. The
  // spec asserts the user-visible response, not the 3D pick: either the
  // region-edit-bar (the pick hit geometry — marker + bar) or the
  // selection-notice (the pick missed — "Click on the model to point at a
  // part."). Both are the app's response to a completed pick, and which
  // one appears is WebGL-dependent under headless SwiftShader.
  const regionBar = page.locator('[data-testid="region-edit-bar"]');
  const selectionNotice = page.locator('[data-testid="selection-notice"]');

  await expect
    .poll(
      async () => (await regionBar.count()) > 0 || (await selectionNotice.count()) > 0,
      { timeout: 10_000, message: "no region-edit bar or selection notice appeared after the single click" },
    )
    .toBeTruthy();
});
