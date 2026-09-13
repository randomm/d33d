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
 * Timeline visibility (issue #52 fix): the SPA fetches its version
 * timeline exactly once per page load (the `listVersions` effect keyed
 * on projectId, which only runs after the mount-time `POST /api/projects`
 * resolves and `setProjectId` re-renders the pane). The spec gates the
 * SPA's first versions GET with a `page.route` handler that holds the
 * request until both versions exist, then releases it — so the SPA's
 * single timeline fetch of this page load returns both versions and the
 * pane renders them without any reload (a reload would POST a FRESH
 * project via App.tsx's unconditional mount-time `createProject` and
 * orphan the versions).
 *
 * The old gate (pre-#52) held the versions GET only when `projectId`
 * was already populated in the spec:
 *
 *   if (route.request().method() === "GET" && projectId !== null) {
 *     await gate;
 *   }
 *   await route.continue();
 *
 * That raced with the SPA's fetch ordering under parallel load (5
 * workers against one uvicorn server): the SPA's `setProjectId` and the
 * spec's `projectId = created.id` are both async continuations on the
 * same POST response, and the SPA's `listVersions` effect (and thus its
 * versions GET) could reach the route handler before the spec's
 * `projectId` assignment ran. With `projectId` still null the gate was
 * skipped, the fetch went through un-gated, and the SPA rendered "No
 * versions yet" for the rest of the test (60s timeout waiting for
 * `timeline-entry-{id}`).
 *
 * The fix drops the `projectId !== null` condition: the versions GET is
 * gated unconditionally (any matching project id). On a fresh page load
 * the SPA's only versions GET is its own timeline fetch — no compare,
 * restore, pin, or single-version GET is in flight at that point — so
 * gating every matching GET is safe and eliminates the race entirely.
 * The gate is released exactly once, after both versions are created via
 * the direct API.
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
  // The SPA's timeline fetch (GET /api/projects/{pid}/versions) is the ONLY
  // versions GET in flight on a fresh page load (compare/restore/pin/single
  // are longer paths and don't match the bare-versions regex below). Hold it
  // until both versions exist, then let it through.
  //
  // The gate is UNCONDITIONAL on the GET — it does NOT check whether the
  // spec's `projectId` is populated yet. That check (pre-#52) was the race:
  // the SPA's listVersions effect runs as soon as setProjectId fires, which
  // is an async continuation on the same POST response the spec reads for
  // projectId. Under parallel load the SPA's GET could reach this handler
  // before the spec's `projectId = created.id` ran; with the check, the
  // fetch went through un-gated and the SPA rendered an empty timeline for
  // the rest of the test. Gating every matching GET removes the race because
  // there is no other bare versions GET to block on a fresh page load.
  let releaseGate: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    releaseGate = resolve;
  });

  await page.route(
    (url) => url.pathname.match(/^\/api\/projects\/\d+\/versions$/),
    async (route) => {
      if (route.request().method() === "GET") {
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
  const projectId = created.id;

  await page.getByTestId("app-shell").waitFor();

  // The SPA's listVersions is now hanging on the gate (its GET matched the
  // route and is awaiting `gate`). Create both versions on the SPA's own
  // project in this window — the gate is unconditional, so this works
  // regardless of whether the SPA's GET arrived before or after the spec's
  // projectId assignment above.
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
  //
  // NOTE: this re-fetch is a SECOND bare versions GET. The gate is already
  // released (it fires once), so the re-fetch passes through un-gated — the
  // gate is a one-shot latch, not a permanent hold.
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

  // v1 = {W:60, H:40} vs v2 = {W:60, H=50, D:30}:
  //   changed: H (40 → 50)   added: D (— → 30)   removed: (none)
  await expect(page.getByTestId("diff-changed-H")).toBeVisible();
  await expect(page.getByTestId("diff-added-D")).toBeVisible();

  // The diff table has one header row + the two data rows (H, D) — no
  // removed rows, so exactly 3 rows total.
  await expect(page.getByTestId("compare-diff").locator("tr")).toHaveCount(3);

  // The shared-rotation contract row.
  await expect(page.getByTestId("compare-shared-rotation")).toBeVisible();
});
