/**
 * Version-timeline E2E spec (issue #49, workstream `spec-versions`).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_version_timeline.spec.ts`
 *
 * Covers the version-timeline surface end-to-end against the locally
 * booted FastAPI app (web/playwright.config.ts webServer hook):
 *
 *   1. CREATE  — create two versions with DIFFERENT params via the API,
 *                open the history sheet (the filmstrip's expand mark),
 *                and assert the timeline inside the sheet renders both
 *                entries with the correct names and diff badges.
 *   2. RESTORE — restore version 1 from the UI (the
 *                `timeline-restore-{id}` button inside the sheet), then
 *                assert a NEW forward version appears in the timeline
 *                carrying v1's full snapshot (the non-destructive
 *                restore contract), and the latest's restore button is
 *                disabled.
 *   3. COMPARE — select versions 1 and 2 for compare via the UI (the
 *                `timeline-compare-{id}` buttons inside the sheet),
 *                then assert the compare pane shows the diff table with
 *                the expected changed/added rows and the shared-rotation
 *                contract.
 *
 * The spec talks to the live API directly for version creation (the
 * ticket's stated design: "create, restore, compare via the API + UI
 * assertions") and drives the SPA's history sheet for the UI half.
 *
 * The history sheet (issue #127, W16) is an OVERLAY over the canvas —
 * it is the home of the compare / restore / pin / timeline actions.
 * It is reached ONLY from the filmstrip's expand mark
 * (`filmstrip-expand-{id}`), which is a sibling of the filmstrip slot
 * button. The spec opens it that way — the user path.
 *
 * Project discovery: App.tsx auto-creates exactly one project on mount
 * (`createProject("untitled project")`). The spec discovers that project
 * id from the mount-time `POST /api/projects` response — so the
 * spec's direct-API work targets the SAME project the SPA is displaying,
 * and no orphan project is ever created.
 *
 * Timeline visibility: the SPA fetches its version timeline once per
 * page load (the `listVersions` effect keyed on projectId). The spec
 * gates the SPA's versions GET with a route handler that holds the
 * request until both versions exist, then releases all held requests
 * with the full timeline. The gate is scoped to THIS page's project id
 * only — it never holds requests for other projects. After the gate
 * releases, all subsequent versions GETs (e.g. the refetch after
 * restore) pass through un-gated.
 *
 * All version ids in this spec come from the API response (the ids
 * returned by createVersion / restoreVersion), never hardcoded.
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
  // default (SPA boot, gated fetch, sheet open, three network round-trips).
  test.setTimeout(60_000);

  const baseURL = process.env.E2E_BASE_URL ?? "http://localhost:8080";

  // -- Navigate and discover the SPA's auto-created project -----------------
  // The SPA POSTs /api/projects on mount; capture that response so the
  // spec's direct-API calls target the SAME project the SPA is displaying.
  const projectResp = page.waitForResponse(
    (r) => r.url().endsWith("/api/projects") && r.request().method() === "POST",
  );
  await page.goto("/");
  const created = (await (await projectResp).json()) as { id: number };
  const projectId = created.id;

  // -- Gate the SPA's versions GET for THIS project only -------------------
  // Hold all versions GETs for projectId until both versions exist, then
  // release all held requests with the full timeline in one burst. Requests
  // for other project ids are never intercepted. The gate releases exactly
  // once; subsequent calls to releaseGate are no-ops.
  let gateReleased = false;
  let releaseGate: (() => void) | null = null;
  const gate = new Promise<void>((resolve) => {
    releaseGate = resolve;
  });
  const release = () => {
    if (gateReleased) return;
    gateReleased = true;
    releaseGate?.();
  };

  // Scope the route to this page's project id only — the regex captures the
  // id at registration time, not a wildcard.
  const versionsPath = `/api/projects/${projectId}/versions`;
  await page.route(
    (url) => url.pathname === versionsPath,
    async (route) => {
      if (route.request().method() === "GET") {
        await gate;
      }
      await route.continue();
    },
  );

  await page.getByTestId("app-stage").waitFor();

  // Create both versions on the SPA's own project in the gated window.
  // The SPA's versions GET is held; after both exist, release lets it
  // through with the full two-entry timeline.
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
  release();
  await gate;

  // -- Open the history sheet the way a user does ---------------------------
  // The filmstrip renders once the versions list is non-empty (the SPA's
  // listVersions fetch just completed with both versions). The filmstrip's
  // expand mark (`filmstrip-expand-{id}`) is the sheet's only entry point.
  // This is a GENUINE pointer click — no force, no dispatchEvent, no panel
  // hidden first. It is the regression test for issue #184 (the defect where
  // the conversation pane's box covered the expand mark, so the browser hit
  // test delivered the click to the pane instead of the button).
  for (const viewport of [
    { width: 1024, height: 640 }, // the documented floor — tightest case
    { width: 1280, height: 720 },
    { width: 1440, height: 900 },
  ]) {
    page.setViewportSize(viewport);
    await page.getByTestId("version-filmstrip").waitFor();
    await page.getByTestId(`filmstrip-expand-${v1.id}`).click();
    await page.getByTestId("history-sheet").waitFor();
    // Close the sheet again — the next iteration re-clicks a fresh pointer
    // (the open slot's mark is aria-pressed while the sheet is open).
    await page.getByTestId("history-sheet-close").click();
    await page
      .getByTestId("history-sheet")
      .waitFor({ state: "detached" });
  }

  // Re-open the sheet (still via the genuine click) for the assertions below.
  page.setViewportSize({ width: 1280, height: 720 });
  await page.getByTestId(`filmstrip-expand-${v1.id}`).click();
  await page.getByTestId("history-sheet").waitFor();

  // -- 1. CREATE: assert the timeline renders both entries ------------------
  // The timeline lives INSIDE the sheet (VersionTimeline is a child of
  // HistorySheet). Both entries must be visible with correct names.
  await page.getByTestId(`timeline-entry-${v1.id}`).waitFor();
  await page.getByTestId(`timeline-entry-${v2.id}`).waitFor();
  await expect(page.getByTestId(`timeline-name-${v1.id}`)).toHaveText("base");
  await expect(page.getByTestId(`timeline-name-${v2.id}`)).toHaveText("taller");

  // Diff badges: v1 has no parent → diff_count 0 (badge renders empty);
  // v2's parent is v1 → diff_count 2 (H changed 40→50, D added).
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

  const restoreResponse = await restoreResp;
  const restored = JSON.parse(await restoreResponse.text()) as VersionEntry;

  // The restored version is a NEW forward version carrying v1's snapshot
  // (parent = current latest = v2; restored_from = v1).
  expect(restored.id).not.toBe(v1.id);
  expect(restored.restored_from).toBe(v1.id);
  expect(restored.parent).toBe(v2.id);
  expect(restored.params).toEqual({ W: 60, H: 40 });

  // The timeline re-fetches after the restore (handleVersionRestore calls
  // listVersions) → now 3 entries, including the new restored entry.
  // The gate is already released, so the re-fetch passes through un-gated.
  await page.getByTestId(`timeline-entry-${restored.id}`).waitFor();
  await expect(page.getByTestId("version-timeline-count")).toHaveText("3");

  // The restored entry's diff badge: its parent is v2 (W=60, H=50, D=30),
  // its own params are (W=60, H=40) → H changed, D removed → diff 2.
  await expect(
    page.getByTestId(`timeline-diff-${restored.id}`).textContent(),
  ).resolves.toEqual("2 params changed");

  // -- 3. COMPARE: select v1 and v2 for compare via the UI -------------------
  // Click compare on v1: compareIds = [v1, v1] (no network fetch yet —
  // the SPA skips the fetch when both ids are the same).
  await page.getByTestId(`timeline-compare-${v1.id}`).click();

  // Click compare on v2: compareIds = [v1, v2] → compareVersions fetch.
  const compareResp: Promise<Response> = page.waitForResponse(
    (r) =>
      r.url().includes(
        `/api/projects/${projectId}/versions/compare?a=${v1.id}&b=${v2.id}`,
      ) && r.status() === 200,
  );
  await page.getByTestId(`timeline-compare-${v2.id}`).click();
  await compareResp;

  // The compare pane appears inside the sheet (compareIds + compareResult
  // are both non-null → HistorySheet renders the compare-pane div).
  await page.getByTestId("compare-pane").waitFor();
  await page.getByTestId("compare-diff").waitFor();

  // v1 = {W:60, H:40} vs v2 = {W:60, H:50, D:30}:
  //   keys: W (unchanged), H (changed 40→50), D (added —→30)
  //   → 3 data rows + 1 header row = 4 tr elements total.
  await expect(page.getByTestId("diff-changed-H")).toBeVisible();
  await expect(page.getByTestId("diff-added-D")).toBeVisible();

  await expect(page.getByTestId("compare-diff").locator("tr")).toHaveCount(4);

  // The shared-rotation contract row.
  await expect(page.getByTestId("compare-shared-rotation")).toBeVisible();

  // -- PIN: reachable from this (session with versions) by pointer alone ----
  // The sheet's right-side box (right-anchored, 360px) is clear of the
  // left-anchored pane, so every timeline action inside the sheet is
  // pointer-reachable with the sheet open — pin included. Pin v1 from the
  // sheet's own timeline; the pointer path is the sheet open (real click on
  // the expand mark, above) plus this click.
  const pinBefore = await page.getByTestId(`timeline-pin-${v1.id}`).textContent();
  const pinResp: Promise<Response> = page.waitForResponse(
    (r) =>
      r.url() ===
        `${baseURL}/api/projects/${projectId}/versions/${v1.id}` &&
      r.status() === 200,
  );
  await page.getByTestId(`timeline-pin-${v1.id}`).click();
  await pinResp;
  const pinAfter = await page.getByTestId(`timeline-pin-${v1.id}`).textContent();
  expect(pinAfter).not.toEqual(pinBefore);
});
