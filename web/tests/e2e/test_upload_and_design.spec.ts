/**
 * E2E happy path (issue #49, spec 1 of 5):
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_upload_and_design.spec.ts`
 *
 *   the app loads with NO project created (issue #192 — creation moved
 *     off mount)
 *     → the first explicit chat send creates the project lazily
 *     → a reference photo is uploaded via the reference-photo input
 *     → the photo-upload UI settles to the success state
 *     → the post-upload app state is stable (export button visible,
 *       no upload error, no app-level error).
 *
 * Determinism notes:
 *   - No LLM calls, no real Docker render worker. The SSE stream is NOT
 *     intercepted in this spec — the happy path exercises the upload path,
 *     and the app's SSE fetch only fires on chat send.
 *   - Project creation and photo upload are both REAL (unintercepted)
 *     backend behaviour. Project creation is NOT on mount anymore
 *     (issue #192): it is triggered by the spec's own first explicit chat
 *     send through the real send path (chat input -> postChat to the real
 *     backend -> SSE stream). The spec discovers the project id from THAT
 *     POST /api/projects response and asserts against that project —
 *     never from `GET /api/projects` index 0. On a shared backend whose
 *     id sequence is global, index 0 is some other spec's project and the
 *     spec would assert against the wrong one. The freshly created
 *     project's source_photo_path is null, so the toBeNull assertion
 *     stays meaningful regardless of what else the server holds. The
 *     settled state (source_photo_path populated) is then verified via
 *     the API — we assert the settled API state rather than racing a
 *     network response, which keeps the spec resilient to the race
 *     where the SPA's POST lands before page.waitForResponse is armed.
 *
 * The 3MF download half of this spec was removed in issue #109: the
 * download route does not exist server-side, so the old test fulfilled
 * page.route with a zip the test itself constructed — it asserted a
 * capability that was unimplemented.
 */

import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const FIXTURE_PHOTO_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "test-photo.png",
);

const BASE = () => process.env.E2E_BASE_URL ?? "http://localhost:8080";

type Project = {
  id: number;
  name: string;
  source_photo_path: string | null;
};

test.beforeEach(async ({ request }) => {
  // Wait for the app to be reachable (webServer hook / E2E_SKIP_SERVER).
  await expect
    .poll(async () => (await request.get(`${BASE()}/api/projects`)).status(), {
      timeout: 15_000,
    })
    .toBe(200);
});

test("happy path: send a message → upload photo → settle", async ({ page }) => {
  // --- SPA boot: NO project is created on mount (issue #192) --------------
  // Arm the creation wait BEFORE navigating so the lazy POST (fired by the
  // first explicit send below) cannot land before the wait. Count the
  // project rows before and after boot to prove a fresh load created
  // nothing — the "no project on mount" acceptance criterion, asserted as
  // a count delta (a shared long-lived backend means absolute counts are
  // never zero).
  const before = (await page.request.get(`${BASE()}/api/projects`)).json() as Project[];
  const createdResp = page.waitForResponse(
    (r) =>
      r.url().endsWith("/api/projects") && r.request().method() === "POST",
  );
  await page.goto("/");
  await expect(page.getByTestId("app-stage")).toBeVisible();

  // The first-run screen is up (no versions, no messages) and NO mount-time
  // POST /api/projects fired: the row count is unchanged since navigation.
  await expect(page.getByTestId("first-run")).toBeVisible();
  const after = (await page.request.get(`${BASE()}/api/projects`)).json() as Project[];
  expect(after.length).toBe(before.length);

  // --- First explicit send: the lazy project creation ---------------------
  // The chat send is the project-creation trigger (issue #192): handleSendMessage
  // creates the project single-flight, then posts the message and opens the
  // SSE stream — all against the REAL backend.
  const chatInput = page.getByTestId("chat-input");
  await chatInput.fill("a small box");
  await chatInput.press("Enter");

  const project = (await (await createdResp).json()) as Project;
  expect(project.source_photo_path).toBeNull();

  // The upload input is rendered once the project id exists (PhotoUpload
  // receives the id as a prop and renders the hidden input + label).
  const photoInput = page.getByTestId("photo-file-input");
  await expect(photoInput).toBeAttached();

  // --- Upload: the reference photo via the input -------------------------
  // The photo is POSTed to the REAL (unintercepted) backend upload
  // endpoint for this project.
  await photoInput.setInputFiles([
    {
      name: "test-photo.png",
      mimeType: "image/png",
      buffer: readFileSync(FIXTURE_PHOTO_PATH),
    },
  ]);

  // Settle on the REAL backend state: the upload endpoint persists the
  // stored path into the project row (201 response + DB update), so the
  // project's source_photo_path flips from null to a path.
  await expect
    .poll(
      async () =>
        (
          (await page.request.get(`${BASE()}/api/projects/${project.id}`))
            .json() as Project
        ).source_photo_path !== null,
      { timeout: 15_000 },
    )
    .toBe(true);

  // The upload settles to the success state: the label now shows the
  // preview image instead of the "Attach reference photo" prompt.
  await expect(page.getByTestId("photo-preview")).toBeVisible({
    timeout: 10_000,
  });
  await expect(page.getByText("📎 Attach reference photo")).toHaveCount(0);

  // --- Settle -------------------------------------------------------------
  // The validation pane shows the real validation state or nothing — no
  // static placeholder (issue #114 removed the static status span): the
  // pane is absent before the project loads, and once it loads it carries
  // only the Export3MF button.
  await expect(page.getByTestId("export-3mf")).toBeVisible();
  await expect(page.getByTestId("upload-error")).toHaveCount(0);
  await expect(page.getByTestId("app-error")).toHaveCount(0);
});
