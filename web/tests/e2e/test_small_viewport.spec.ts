/**
 * E2E: small-viewport adaptive layout (issue #194).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_small_viewport.spec.ts`
 *
 * The ticket's acceptance criterion: at 1024 × 640 (the documented floor)
 * a GENUINE pointer click at the centre of chat-send-btn's bounding box
 * must be delivered to chat-send-btn — not to the build plate / 3D canvas
 * behind it. The pre-fix layout floated the conversation pane at the
 * top-left at every size, so at 640px-tall viewports the pane's box
 * (z-index 20) covered the plate and the composer's Send button landed on
 * the cube.
 *
 * The fix (issue #194): below 900px of window height the conversation
 * Docks to the bottom of the window — a full-width bar, 48vh tall,
 * `data-testid="conversation-docked-notice"` in its header — and the
 * canvas keeps 100% − 48vh above it. The composer (and any failure card)
 * sits in the docked band; the plate is in the canvas band above it. The
 * filmstrip moves INSIDE the docked bar in this case (its expand marks
 * must stay pointer-reachable — the issue #184 hit-target class must not
 * be reintroduced by the dock).
 *
 * Determinism & what is real vs intercepted:
 *   - The only interception is the SSE transport (GET /api/stream/{id},
 *     issue #182 mode (b)): it is fulfilled with a version-created frame
 *     carrying the genuine rendered fixture
 *     web/tests/fixtures/viewer/mini-box.stl as `stl_data_uri` (the same
 *     real STL the render worker emits elsewhere in the suite), then a
 *     terminal done frame. Project creation (lazy, issue #192), the
 *     postChat round-trip and the SPA's own SSE demultiplexing all run
 *     for real; only the bytes behind the stream are the fixture.
 *   - A real version exists on the project's timeline (the SPA refetches
 *     the timeline on the version-created frame), so at 1024 × 640 the
 *     chat composer is present (the first-run screen is gone) and the
 *     Send button is enabled once the draft is non-empty and no loop is
 *     in flight (the stream completed: the done frame released
 *     designLoopInFlight).
 *   - At 1280 × 900 the same app runs the FLOATING layout (no dock): the
 *     docked notice is absent and the conversation pane is anchored
 *     top-left at (24, 24). This pairs the two layouts so the spec
 *     exercises the threshold itself, not only one side of it.
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

test("small viewport: the conversation docks and the Send button stays pointer-reachable", async ({
  page,
}) => {
  test.setTimeout(60_000);

  await page.setViewportSize({ width: 1024, height: 640 });
  await page.goto("/");

  // -- Drive the real send path (lazy project creation, issue #192) ----
  // On a fresh mount the first-run screen is the input surface (the chat
  // composer is hidden while versions.length === 0 && messages.length ===
  // 0), so the first Enter press targets first-run-input, not chat-input
  // (same pattern as test_point_selection.spec.ts; issue #217).
  const projectResp = page.waitForResponse(
    (r) => r.url().endsWith("/api/projects") && r.request().method() === "POST",
  );
  const firstRunInput = page.getByTestId("first-run-input");
  await expect(firstRunInput).toBeVisible();
  await firstRunInput.fill("a small box");
  await firstRunInput.press("Enter");
  const { id: projectId } = (await (await projectResp).json()) as { id: number };

  // -- Intercept the SSE transport (issue #182 mode (b)) -----------------
  // The stream delivers a genuine rendered STL (the version-created
  // frame's stl_data_uri) and then completes: the done frame releases
  // the design loop, so the Send button is not in-flight-disabled.
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
    (url) => url.pathname === `/api/stream/${projectId}`,
    async (route) => {
      await route.fulfill({
        status: 200,
        headers: { "Content-Type": "text/event-stream" },
        body: frames,
      });
    },
  );

  // The version-created frame refetches the timeline → a version exists,
  // the first-run screen unmounts, and the conversation pane (with its
  // composer) is the active input surface.
  await expect(page.getByTestId("version-filmstrip")).toBeVisible();
  await expect(page.getByTestId("first-run")).toHaveCount(0);

  // The design loop has finished (the done frame released the flag), so
  // a non-empty draft enables the Send button.
  const send = page.getByTestId("chat-send-btn");
  await page.getByTestId("chat-input").fill("make it taller");
  await expect(send).toBeEnabled();

  // -- The dock: the conversation bar is at the bottom, full width -------
  // The docked caption (copy.shell.conversationDocked) marks the bar;
  // the filmstrip has moved inside it (its expand mark is in the bar,
  // not floating at the stage's bottom-left).
  const dockedNotice = page.getByTestId("conversation-docked-notice");
  await expect(dockedNotice).toBeVisible();
  const barBox = await page.getByTestId("app-left-pane").boundingBox();
  const pageBox = await page.evaluate(() => ({
    w: window.innerWidth,
    h: window.innerHeight,
  }));
  expect(barBox).not.toBeNull();
  if (barBox) {
    // Full width, anchored to the bottom edge, ~48% of the viewport tall.
    expect(Math.abs(barBox.width - pageBox.w)).toBeLessThanOrEqual(2);
    expect(Math.abs(barBox.y + barBox.height - pageBox.h)).toBeLessThanOrEqual(2);
    expect(barBox.height).toBeGreaterThanOrEqual(0.4 * pageBox.h);
    expect(barBox.height).toBeLessThanOrEqual(0.55 * pageBox.h);
  }
  // The filmstrip lives INSIDE the docked bar (issue #184 class: its
  // expand marks stay reachable because the pane no longer covers them).
  const stripBox = await page.getByTestId("version-filmstrip").boundingBox();
  expect(stripBox).not.toBeNull();
  if (barBox && stripBox) {
    expect(stripBox.y).toBeGreaterThanOrEqual(barBox.y);
    expect(stripBox.y + stripBox.height).toBeLessThanOrEqual(
      barBox.y + barBox.height,
    );
  }

  // -- The docked history sheet occupies the canvas band (issue #194
  // adversarial fix) ----------------------------------------------
  // Regression guard for the layout defect that made the docked sheet
  // render with a NEGATIVE height: rendered as a child of the docked pane
  // (a positioned element, the sheet's containing block), the sheet's
  // top offset (band height + inset) resolved against the PANE, so at
  // 1024 × 640 its top landed at 664px with its bottom at 616px — the
  // sheet was off-screen, invisible, and non-interactable (a silent
  // failure: click the expand mark, state flips, nothing appears). This
  // is the check that catches the defect — jsdom cannot lay out and so
  // cannot see a negative-height box; a real browser can.
  await page.getByTestId("filmstrip-expand-1").click();
  const sheet = page.getByTestId("history-sheet");
  await expect(sheet).toBeVisible();
  const sheetBox = await sheet.boundingBox();
  expect(sheetBox, "the docked sheet must have a real bounding box").not.toBeNull();
  if (sheetBox) {
    // A positive height and an on-screen position: the sheet occupies the
    // canvas band above the bar, not a zero/negative-height box below it.
    expect(sheetBox.height, "the docked sheet must have height > 0").toBeGreaterThan(0);
    expect(sheetBox.y, "the docked sheet must not start below the viewport").toBeGreaterThanOrEqual(0);
    expect(sheetBox.y + sheetBox.height, "the docked sheet must fit in the canvas band")
      .toBeLessThanOrEqual(pageBox.h);
    expect(sheetBox.y + sheetBox.height, "the docked sheet must clear the bar")
      .toBeLessThanOrEqual(barBox!.y + 2);
  }

  // -- The acceptance criterion: a GENUINE pointer click at the centre of
  // chat-send-btn's bounding box is delivered to chat-send-btn (not the
  // plate/canvas behind it). Playwright's actionability check performs a
  // real hit-test at the click point; if the plate or the pane's old
  // (full-coverage) box intercepted the point, the click would time out
  // or land elsewhere. The click must also trigger the send path:
  // postChat is the observable side effect.
  const chatSendBox = await send.boundingBox();
  expect(chatSendBox).not.toBeNull();
  if (chatSendBox) {
    const point = {
      x: chatSendBox.x + chatSendBox.width / 2,
      y: chatSendBox.y + chatSendBox.height / 2,
    };
    const hit = await page.evaluate(
      ([px, py]) => {
        const el = document.elementFromPoint(px, py);
        return el ? el.closest('[data-testid="chat-send-btn"]') !== null : false;
      },
      [point.x, point.y] as [number, number],
    );
    expect(hit, "the pointer at the Send button's centre must hit the Send button").toBe(true);

    // And the click does what a send click does: the design loop starts
    // (postChat → the stream is re-opened), so the send button goes
    // in-flight-disabled.
    const postChatResp: Promise<unknown> = page.waitForResponse(
      (r) =>
        r.url() === `/api/projects/${projectId}/chat` &&
        r.request().method() === "POST",
    );
    await send.click();
    await postChatResp;
    await expect(send).toBeDisabled();
  }

  // -- Above the threshold the layout is the floating pane (no dock) ----
  // 1280 × 900 ≥ 900px height → the conversation floats at top-left
  // (24, 24); the docked bar and its caption are absent.
  await page.setViewportSize({ width: 1280, height: 900 });
  await expect(page.getByTestId("conversation-docked-notice")).toHaveCount(0);
  // The floating sheet keeps both edges at the inset (the docked bar
  // does not exist to clear, so no bottom offset is needed).
  const floatSheet = page.getByTestId("history-sheet");
  const floatSheetBox = await floatSheet.boundingBox();
  expect(floatSheetBox, "the floating sheet must have a real bounding box").not.toBeNull();
  if (floatSheetBox) {
    expect(floatSheetBox.height, "the floating sheet must have height > 0").toBeGreaterThan(0);
    expect(floatSheetBox.y, "the floating sheet's top must be at the inset").toBeGreaterThanOrEqual(22);
    expect(floatSheetBox.y + floatSheetBox.height, "the floating sheet's bottom must clear the inset")
      .toBeLessThanOrEqual(900 - 22);
  }
  const floatBox = await page.getByTestId("app-left-pane").boundingBox();
  expect(floatBox).not.toBeNull();
  if (floatBox) {
    expect(Math.abs(floatBox.x - 24)).toBeLessThanOrEqual(2);
    expect(Math.abs(floatBox.y - 24)).toBeLessThanOrEqual(2);
    expect(floatBox.width).toBeLessThanOrEqual(422);
  }
  // The filmstrip floats at the stage's bottom-left again.
  const floatStripBox = await page
    .getByTestId("version-filmstrip")
    .boundingBox();
  expect(floatStripBox).not.toBeNull();
  if (floatStripBox) {
    expect(Math.abs(floatStripBox.x - 24)).toBeLessThanOrEqual(2);
  }
});
