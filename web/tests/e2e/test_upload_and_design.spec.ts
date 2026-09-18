/**
 * E2E happy path (issue #49, spec 1 of 5):
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_upload_and_design.spec.ts`
 *
 *   upload a photo via the reference-photo input
 *     → the photo-upload UI settles to the success state
 *     → the post-upload app state is stable (export button visible,
 *       no upload error, no app-level error).
 *
 * Determinism notes:
 *   - No LLM calls, no real Docker render worker. The SSE stream is NOT
 *     intercepted in this spec — the happy path exercises the upload path,
 *     and the app's SSE fetch only fires on chat send.
 *   - The SPA auto-creates a project on mount (App.tsx). Project creation
 *     and photo upload are both REAL (unintercepted) backend behaviour:
 *     we assert the settled API state (project exists, its
 *     source_photo_path is populated) rather than racing a network
 *     response, which makes the spec resilient to the mount-time race
 *     where the SPA's POST lands before page.waitForResponse is armed.
 *
 * The 3MF download half of this spec was removed in issue #109: the
 * download route does not exist server-side, so the old test fulfilled
 * page.route with a zip the test itself constructed — it asserted a
 * capability that was unimplemented.
 */

import { test, expect, type Page } from "@playwright/test";
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

/**
 * Wait until the SPA has auto-created its project on mount (App.tsx
 * creates one; POST /api/projects is real backend behaviour) and return
 * its id. Polls the API — no network-stream race.
 */
async function waitForAutoCreatedProject(page: Page): Promise<Project> {
  const poll = async () =>
    (await page.request.get(`${BASE()}/api/projects`)).json();
  await expect.poll(poll, { timeout: 15_000 }).not.toHaveLength(0);
  const projects = (await poll()) as Project[];
  return projects[0];
}

test.beforeEach(async ({ request }) => {
  // Wait for the app to be reachable (webServer hook / E2E_SKIP_SERVER).
  await expect
    .poll(async () => (await request.get(`${BASE()}/api/projects`)).status(), {
      timeout: 15_000,
    })
    .toBe(200);
});

test("happy path: upload photo → settle", async ({ page }) => {
  // --- SPA boot: the app auto-creates a project on mount -----------------
  await page.goto("/");
  await expect(page.getByTestId("app-stage")).toBeVisible();

  const project = await waitForAutoCreatedProject(page);
  expect(project.source_photo_path).toBeNull();

  // The upload input is rendered once the project id exists.
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
