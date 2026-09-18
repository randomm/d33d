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
 *     and photo upload are both REAL (unintercepted) backend behaviour.
 *   - OWN-PROJECT OWNERSHIP (issue #186): the spec discovers the project
 *     THIS page load created from the mount-time `POST /api/projects`
 *     response and asserts against that project — never from
 *     `GET /api/projects` index 0. The SPA creates a fresh project on
 *     every page load against a long-lived backend whose id sequence is
 *     global, so under a dirty server (a prior spec or run left projects
 *     behind) index 0 is some other spec's project and the spec would
 *     assert against the wrong one. The SPA's POST always yields a
 *     source_photo_path=null project, so the toBeNull assertion stays
 *     meaningful regardless of what else the server holds. The settled
 *     state (source_photo_path populated) is then verified via the API —
 *     we assert the settled API state rather than racing a network
 *     response, which keeps the spec resilient to the mount-time race
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

test("happy path: upload photo → settle", async ({ page }) => {
  // --- SPA boot: the app auto-creates a project on mount -----------------
  // The SPA's own project for THIS page load — discovered from the
  // mount-time POST response, never from the global projects list
  // (issue #186: on a dirty backend projects[0] belongs to another spec).
  const projectResp = page.waitForResponse(
    (r) =>
      r.url().endsWith("/api/projects") && r.request().method() === "POST",
  );
  await page.goto("/");
  const project = (await (await projectResp).json()) as Project;
  await expect(page.getByTestId("app-stage")).toBeVisible();
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
