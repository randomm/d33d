/**
 * Shared Playwright fixtures for the d33d E2E suite (issue #49).
 *
 * Extends Playwright's `page` with:
 * - `e2eProject` — a project id created via the real backend API
 *   (`POST /api/projects`), deleted in `teardown` when not E2E_SKIP_SERVER.
 *   Sibling spec files own their own specs; this fixture is the single
 *   "temp project + cleanup" helper the test-surface section of the issue
 *   names. Specs that need a project reference it via dependency injection.
 *
 * - `e2eApi` — a minimal fetch-based API helper bound to the same
 *   `baseURL` as the Playwright `page`, so specs can exercise the backend
 *   directly (version-timeline create/restore/compare, etc.) without
 *   duplicating HTTP boilerplate.
 *
 * - `e2eFixturesDir` — absolute path to `web/tests/e2e/` (where the
 *   fixture files now live), resolved
 *   relative to this file so specs can locate `test-photo.png` and
 *   `sample-model.3mf` regardless of the process cwd (Playwright may be
 *   invoked from the repo root or from `web/`).
 *
 * - `e2eSseInterceptor` — a `page.route` helper that intercepts
 *   `GET /api/stream/{id}` (the SPA's SSE endpoint) and fulfils it with a
 *   controlled, finite SSE byte string. The SPA's `ApiClient.streamEvents`
 *   (web/src/lib/api.ts) only resolves after a terminal `done`|`error`
 *   frame AND a trailing blank line, so the fulfilled body is always a
 *   complete, well-formed SSE document — see `makeSseFrame` below.
 *
 * - `e2eModel3mfInterceptor` — a `page.route` helper that intercepts
 *   `GET /api/projects/*/model.3mf` (the SPA's 3MF download endpoint, not
 *   yet implemented server-side) and fulfils it with the pre-rendered
 *   `sample-model.3mf` fixture, so the happy-path spec can assert the
 *   download path without the backend route existing.
 *
 * All fixtures are deterministic: no LLM calls, no Docker, no network
 * beyond the local uvicorn that the `webServer` hook (or E2E_SKIP_SERVER)
 * provides.
 */

import { test as base } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

/** Absolute path to the `web/tests/e2e/` fixture directory. */
export const E2E_FIXTURES_DIR = path.resolve(__dirname);

/** Absolute path to the small reference-photo PNG fixture. */
export const TEST_PHOTO_PATH = path.join(E2E_FIXTURES_DIR, "test-photo.png");

/** Absolute path to the pre-rendered sample 3MF fixture. */
export const SAMPLE_3MF_PATH = path.join(E2E_FIXTURES_DIR, "sample-model.3mf");

/**
 * Build one well-formed SSE frame.
 *
 * The SPA's `ApiClient.streamEvents` (web/src/lib/api.ts) parses frames by
 * the `event:` line, rejoining one-or-more `data:` lines with `"\n"` and
 * `JSON.parse`-ing the result; a frame is terminated by a blank line. The
 * server (d33d/streaming.py `format_sse`) emits the same shape:
 * `event:` line, one-or-more `data:` lines, then a blank line to terminate.
 *
 * The `data` payload is JSON-stringified and emitted as a single `data:`
 * line (the JSON itself may contain no raw newlines since
 * `JSON.stringify` does not insert any — it escapes them as `\n`), so the
 * client's multi-line `data:` rejoin path still round-trips correctly.
 */
function makeSseFrame(event: string, data: Record<string, unknown>): string {
  return `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
}

/**
 * A complete, finite SSE document the SPA's stream parser will fully
 * consume and resolve on. The terminal frame (`done` or `error`) is always
 * present and always last, followed by its terminating blank line — the
 * parser only resolves on a terminal frame, so a mid-stream close without
 * one would silently dispatch nothing (see the issue's edge-case notes).
 */
export function makeSseDocument(
  frames: Array<{ event: string; data: Record<string, unknown> }>,
): string {
  return frames.map((f) => makeSseFrame(f.event, f.data)).join("");
}

/** A project created via the real backend (POST /api/projects). */
export interface E2EProject {
  id: number;
}

/** Minimal typed fetch helper for direct backend calls from specs. */
export class E2EApiClient {
  constructor(private readonly baseUrl: string) {}

  private async json<T>(
    method: string,
    path: string,
    body?: unknown,
    expectedStatus?: number,
  ): Promise<T> {
    const res = await fetch(`${this.baseUrl}${path}`, {
      method,
      headers: body !== undefined ? { "Content-Type": "application/json" } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
    if (expectedStatus !== undefined && res.status !== expectedStatus) {
      const text = await res.text();
      throw new Error(`API ${method} ${path}: expected ${expectedStatus}, got ${res.status}: ${text}`);
    }
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`API ${method} ${path} failed (${res.status}): ${text}`);
    }
    if (res.status === 204) return undefined as T;
    return (await res.json()) as T;
  }

  createProject(name: string): Promise<E2EProject> {
    return this.json<E2EProject>("POST", "/api/projects", { name }, 201);
  }

  deleteProject(id: number): Promise<undefined> {
    return this.json<undefined>("DELETE", `/api/projects/${id}`);
  }

  listVersions(projectId: number): Promise<unknown[]> {
    return this.json<unknown[]>("GET", `/api/projects/${projectId}/versions`);
  }

  createVersion(
    projectId: number,
    params: Record<string, unknown>,
    name?: string,
    message?: string,
  ): Promise<unknown> {
    return this.json<unknown>(
      "POST",
      `/api/projects/${projectId}/versions`,
      { params, name, message },
      201,
    );
  }

  restoreVersion(projectId: number, versionId: number): Promise<unknown> {
    return this.json<unknown>(
      "POST",
      `/api/projects/${projectId}/versions/${versionId}/restore`,
      {},
    );
  }

  compareVersions(projectId: number, a: number, b: number): Promise<unknown> {
    return this.json<unknown>(
      "GET",
      `/api/projects/${projectId}/versions/compare?a=${a}&b=${b}`,
    );
  }
}

type E2EFixtures = {
  e2eProject: E2EProject;
  e2eApi: E2EApiClient;
  e2eFixturesDir: string;
  e2eSseInterceptor: (projectId: number) => Promise<void>;
  e2eSseFrame: typeof makeSseFrame;
  e2eSseDocument: typeof makeSseDocument;
  e2eModel3mfInterceptor: (projectId: number) => Promise<void>;
  e2eSampleModel3mfBytes: Uint8Array;
  e2eTestPhotoBytes: Uint8Array;
};

/**
 * Extended Playwright test. Each spec file in `web/tests/e2e/` imports `test`
 * from here (rather than directly from `@playwright/test`) to get the
 * shared fixtures. Specs that don't need a particular fixture simply don't
 * reference it — Playwright's dependency injection only sets up fixtures a
 * test actually requests, so the `e2eProject` fixture (which creates and
 * deletes a real project via the API) is only paid for by tests that ask
 * for it.
 */
export const test = base.extend<E2EFixtures>({
  e2eApi: [
    async ({ baseURL }, use) => {
      use(new E2EApiClient(baseURL ?? "http://localhost:8080"));
    },
    { scope: "worker" },
  ],

  e2eProject: async ({ e2eApi }, use) => {
    const project = await e2eApi.createProject(`e2e-${Date.now()}`);
    use(project);
    // Best-effort teardown: the in-memory SQLite backend is per-webServer-
    // run, so a leaked project is low-cost, but keep the suite tidy when a
    // long-lived server (E2E_SKIP_SERVER) is reused across runs.
    await e2eApi.deleteProject(project.id).catch(() => {
      /* ignore — the project may already be gone, or the server is stopping */
    });
  },

  e2eFixturesDir: async (_: unknown, use) => {
    use(E2E_FIXTURES_DIR);
  },

  e2eSampleModel3mfBytes: async (_: unknown, use) => {
    use(new Uint8Array(readFileSync(SAMPLE_3MF_PATH)));
  },

  e2eTestPhotoBytes: async (_: unknown, use) => {
    use(new Uint8Array(readFileSync(TEST_PHOTO_PATH)));
  },

  e2eSseFrame: async (_: unknown, use) => {
    use(makeSseFrame);
  },

  e2eSseDocument: async (_: unknown, use) => {
    use(makeSseDocument);
  },

  /**
   * Intercepts `GET /api/stream/{projectId}` on the page and fulfils it
   * with a controlled SSE byte string. The SPA's `streamEvents` fetches
   * this exact path (not `/api/stream/`), so the glob is project-scoped.
   *
   * The default payload is a single terminal `done` frame (the same the
   * server emits when no active stream exists for a project — see
   * d33d/streaming.py), which is the "SSE settles" shape the happy-path
   * spec waits for. Specs 2/3 (gate errors) pass their own `frames` to feed
   * a terminal `error` frame instead.
   */
  e2eSseInterceptor: async
    (
      { page },
      use,
    ) => {
      let active = false;
      const interceptor = async (
        projectId: number,
        frames?: Array<{ event: string; data: Record<string, unknown> }>,
      ) => {
        const body = makeSseDocument(
          frames ?? [{ event: "done", data: { message: "no active stream" } }],
        );
        if (active) await page.unroute("**/api/stream/**");
        await page.route(`**/api/stream/${projectId}`, (route) =>
          route.fulfill({
            status: 200,
            contentType: "text/event-stream",
            body,
          }),
        );
        active = true;
      };
      use(interceptor);
      if (active) await page.unroute("**/api/stream/**");
    },

  /**
   * Intercepts `GET /api/projects/{projectId}/model.3mf` on the page and
   * fulfils it with the pre-rendered `sample-model.3mf` fixture bytes. The
   * real route does not exist yet (separate ticket) — the SPA's
   * `ApiClient.downloadModel3MF` would otherwise 404, so this is what lets
   * the happy-path spec assert the download path.
   */
  e2eModel3mfInterceptor: async
    (
      { page, e2eSampleModel3mfBytes },
      use,
    ) => {
      let active = false;
      const interceptor = async (projectId: number) => {
        if (active) await page.unroute("**/model.3mf");
        await page.route(`**/api/projects/${projectId}/model.3mf`, (route) =>
          route.fulfill({
            status: 200,
            contentType: "model/3mf",
            body: e2eSampleModel3mfBytes,
          }),
        );
        active = true;
      };
      use(interceptor);
      if (active) await page.unroute("**/model.3mf");
    },
});

export { expect } from "@playwright/test";
