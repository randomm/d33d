/**
 * Version-timeline E2E spec (issue #49, workstream `spec-versions`).
 *
 * Covers the version-timeline surface end-to-end against the locally
 * booted FastAPI app (web/playwright.config.ts webServer hook):
 *
 *   1. CREATE  — create two versions with DIFFERENT params via the API,
 *                then assert the SPA's version-timeline pane renders both
 *                entries with the correct names, the correct diff badges
 *                (0 for the parent-less first version, 2 for the child
 *                whose snapshot changes exactly two params), and the
 *                correct timeline count.
 *   2. RESTORE — restore version 1 from the UI (the
 *                `timeline-restore-{id}` button), then assert a NEW
 *                forward version appears in the timeline carrying v1's
 *                full snapshot (the non-destructive restore contract),
 *                and the original latest's restore button is disabled.
 *   3. COMPARE — select versions 1 and 2 for compare via the UI (the
 *                `timeline-compare-{id}` buttons), then assert the
 *                compare pane shows the diff table with the expected
 *                changed/added rows and the shared-rotation contract.
 *
 * The spec talks to the live API directly for version creation (the
 * ticket's stated design: "create, restore, compare via the API + UI
 * assertions") and drives the SPA's version-timeline pane for the UI
 * half.
 *
 * Project discovery: App.tsx auto-creates exactly one project on mount
 * (`createProject("untitled project")`). The spec discovers that project
 * id from the mount-time `POST /api/projects` response — so the
 * spec's direct-API work targets the SAME project the SPA is displaying,
 * and no orphan project is ever created.
 *
 * Timeline visibility: the SPA fetches its version timeline exactly once
 * on mount (the `listVersions` effect keyed on projectId). The spec gates
 * the SPA's first `GET /api/projects/{pid}/versions` with a
 * `page.route` handler that waits on a gate promise, creates both
 * versions on the SPA's project in that window, then releases the gate —
 * so the SPA's only timeline fetch returns both versions and the pane
 * renders them without any reload (a reload would create a fresh project
 * and lose the versions).
 *
 * No LLM calls, no render worker, no SSE interception: the version
 * endpoints are plain REST and fully deterministic.
 */

import { expect, test, type Response } from "@playwright/test";

/** One version-timeline entry as returned by the versions API. */
interface VersionEntry {
  id: number;
  name: string;
  params: Record<string, number | string | boolean>;
  created_by_message: string;
  parent: number | null;
  restored_from: number | null;
  pinned: boolean;
  archived: boolean;
  created_at: string;
  diff_count: number;
}

/**
 * Create a version directly via the API (the ticket's stated design).
 */
async function createVersion(
  baseUrl: string,
  projectId: number,
  params: Record<string, number>,
  name: string,
  message: string,
): Promise<VersionEntry> {
  const res = await fetch(`${baseUrl}/api/projects/${projectId}/versions`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ params, name, message }),
  });
  if (!res.ok) {
    throw new Error(`createVersion failed: ${res.status} ${await res.text()}`);
  }
  return (await res.json()) as VersionEntry;
}

test("version timeline: create, restore, compare", async ({ page }) => {
  // The restore + compare assertions need margin beyond the config's 30 s
  // default (SPA boot, gated fetch, three network round-trips).
  test.setTimeout(60_000);

  // The app base URL (config's use.baseURL — http://localhost:8080 or
  // E2E_BASE_URL). page.goto("/") below uses it too; we need the absolute
  // form for the direct-API fetches.
  const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:8080";

  // -- Gate the SPA's mount-time listVersions --------------------------------
  // The SPA's timeline fetch (GET /api/projects/{pid}/versions) is the
  // ONLY place it reads the timeline on this page load. We intercept it
  // with page.route, hold it until both versions exist, then let it
  // through. `projectId` is filled in as soon as the mount-time POST
  // /api/projects resolves; the route handler reads it at request time
  // (after the fill), so the gate works even though the route is
  // registered before the project exists.
  let projectId: number | null = null;
  let releaseGate: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    releaseGate = resolve;
  });

  await page.route(
    (url) => url.pathname.match(/^\/api\/projects\/\d+\/versions$/),
    async (route) => {
      // Only gate the bare timeline GET for the SPA's project (not
      // compare, create, restore, or single-version — those have longer
      // paths and don't match the regex above). The SPA's listVersions is
      // the only request that hits this route on a fresh page load.
      if (route.request().method() === "GET" && projectId !== null) {
        await gate;
      }
      await route.continue();
    },
  );

  // -- Navigate and discover the SPA's auto-created project -----------------
  const projectResp = page.waitForResponse(
    (r) => r.url().endsWith("/api/projects") && r.request().method() === "POST",
  );
  await page.goto("/");
  const created = (await (await projectResp).json()) as { id: number };
  projectId = created.id;

  await page.getByTestId("app-shell").waitFor();

  // The SPA's listVersions is now hanging on the gate. Create both
  // versions on the SPA's own project in this window.
  const v1 = await createVersion(
    baseURL,
    projectId,
    { W: 60, H: 40 },
    "base",
    "initial base params",
  );
  const v2 = await createVersion(
    baseURL,
    projectId,
    { W: 60, H: 50, D: 30 },
    "taller",
    "taller and deeper",
  );

  // Release the gate — the SPA's timeline fetch proceeds and sees both.
  releaseGate?.();
  await gate;

  // -- 1. CREATE: assert the timeline renders both entries ------------------
  await page.getByTestId(`timeline-entry-${v1.id}`).waitFor();
  await page.getByTestId(`timeline-entry-${v2.id}`).waitFor();
  await expect(page.getByTestId(`timeline-name-${v1.id}`)).toHaveText("base");
  await expect(page.getByTestId(`timeline-name-${v2.id}`)).toHaveText("taller");

  // Diff badges: v1 has no parent → 0 (badge renders empty); v2's parent
  // is v1 → 2 (H changed 40→50, D added — W unchanged).
  await expect(
    page.getByTestId(`timeline-diff-${v1.id}`).textContent(),
  ).resolves.toEqual("");
  await expect(
    page.getByTestId(`timeline-diff-${v2.id}`).textContent(),
  ).resolves.toEqual("2 params changed");

  // The timeline count.
  await expect(page.getByTestId("version-timeline-count")).toHaveText("2");

  // -- 2. RESTORE: restore v1 from the UI -----------------------------------
  // v1 (non-latest) restore is enabled; v2 (latest) restore is disabled.
  await expect(page.getByTestId(`timeline-restore-${v1.id}`)).toBeEnabled();
  await expect(page.getByTestId(`timeline-restore-${v2.id}`)).toBeDisabled();

  const restoreResp: Promise<Response> = page.waitForResponse(
    (r) =>
      r.url() ===
        `${baseURL}/api/projects/${projectId}/versions/${v1.id}/restore` &&
      r.status() === 201,
  );

  await page.getByTestId(`timeline-restore-${v1.id}`).click();

  const restored = (await restoreResp).json() as Promise<VersionEntry>;
  const restoredBody = await restored;

  // The restored version is a NEW forward version carrying v1's snapshot
  // (parent = current latest = v2; restored_from = v1).
  expect(restoredBody.id).not.toBe(v1.id);
  expect(restoredBody.restored_from).toBe(v1.id);
  expect(restoredBody.parent).toBe(v2.id);
  expect(restoredBody.params).toEqual({ W: 60, H: 40 });

  // The timeline re-fetches after the restore (handleVersionRestore calls
  // listVersions) → now 3 entries, including the new restored entry.
  await page.getByTestId(`timeline-entry-${restoredBody.id}`).waitFor();
  await expect(page.getByTestId("version-timeline-count")).toHaveText("3");

  // The restored entry's diff badge: its parent is v2 (W=60, H=50, D=30),
  // its own params are (W=60, H=40) → H changed, D removed → diff 2.
  await expect(
    page.getByTestId(`timeline-diff-${restoredBody.id}`).textContent(),
  ).resolves.toEqual("2 params changed");

  // -- 3. COMPARE: select v1 and v2 for compare via the UI -------------------
  // Click compare on v1: compareIds = [v1, v1] (no network fetch yet —
  // the SPA skips the fetch when both ids are the same).
  await page.getByTestId(`timeline-compare-${v1.id}`).click();

  // Click compare on v2: compareIds = [v1, v2] → compareVersions fetch.
  const compareResp: Promise<Response> = page.waitForResponse(
    (r) =>
      r.url().includes(`/api/projects/${projectId}/versions/compare?a=${v1.id}&b=${v2.id}`) &&
      r.status() === 200,
  );
  await page.getByTestId(`timeline-compare-${v2.id}`).click();
  await compareResp;

  // The compare pane appears with the diff table.
  await page.getByTestId("compare-pane").waitFor();
  await page.getByTestId("compare-diff").waitFor();

  // v1 = {W:60, H:40} vs v2 = {W:60, H:50, D:30}:
  //   changed: H (40 → 50)   added: D (— → 30)   removed: (none)
  await expect(page.getByTestId("diff-changed-H")).toBeVisible();
  await expect(page.getByTestId("diff-added-D")).toBeVisible();

  // The diff table has one header row + the two data rows (H, D) — no
  // removed rows, so exactly 3 rows total.
  await expect(page.getByTestId("compare-diff").locator("tr")).toHaveCount(3);

  // The shared-rotation contract row.
  await expect(page.getByTestId("compare-shared-rotation")).toBeVisible();
});
