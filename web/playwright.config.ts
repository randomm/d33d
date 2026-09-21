/**
 * Playwright config for the MANUAL-ONLY e2e suite (NOT run by CI).
 *
 * Issue #186 — suite-level clean start + teardown.
 *
 * Root cause of the cross-run flake: the backend persists its DB (and
 * its AUTOINCREMENT project/version sequences) in `~/.d33d` for the
 * lifetime of the process. A second `npx playwright test` invocation
 * sees projects and version ids left over from the prior run — the SPA's
 * mount effect auto-creates a new project on every page load, so the
 * "first" project on a dirty server is the prior run's, not the current
 * spec's.
 *
 * Fix: launch the webServer through `python -m d33d.main` with a
 * throwaway `D33D_DATA_DIR` under the OS temp dir. `d33d/main.py` is
 * the ONLY entry point that reads `D33D_DATA_DIR` and builds the app
 * against a file-backed DB — the prior command
 * (`uvicorn d33d.app:create_app --factory`) never imported it, so the
 * env var was inert for that invocation. Each suite run now starts the
 * backend with an empty file DB and a fresh sequence; when the runner
 * process exits, the temp dir is removed. The in-run per-spec
 * isolation (each spec owning its own project via the POST
 * /api/projects response) is owned by the individual spec workstreams;
 * this config owns the suite-level clean start + teardown.
 *
 * Caveat: per-project git dirs default to `/tmp/d33d-projects/<uuid>/`
 * (outside `D33D_DATA_DIR`) and are NOT removed by this teardown —
 * they are uuid-named and do not collide across runs.
 */

import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { defineConfig } from "@playwright/test";
import { shouldGuardE2eSkipServer } from "./src/e2e-guard";

/**
 * Throwaway data dir for the webServer hook (suite lifetime).
 *
 * Only created when the hook will launch a server (E2E_SKIP_SERVER unset).
 * In E2E_SKIP_SERVER mode the operator manages the backend and its data
 * dir themselves — no throwaway dir is created or removed.
 */
let suiteDataDir: string | null = null;

if (!process.env.E2E_SKIP_SERVER) {
  const stamp = `${process.pid}-${Date.now()}`;
  suiteDataDir = fs.mkdtempSync(path.join(os.tmpdir(), `d33d-e2e-${stamp}`));
}

// Issue #207 — operator footgun guard.
//
// In E2E_SKIP_SERVER mode the webServer hook below is `undefined`, so the
// `D33D_DATA_DIR: <throwaway dir>` env override never applies: the specs
// talk to the operator's already-running server, which resolved its data
// dir from ITS OWN environment — defaulting to ~/.d33d when the variable
// was unset. Repeated runs in that mode have written test-fixture project
// and version rows into the operator's live database. Fail fast at
// config-module load (before any Playwright worker starts) unless the
// operator declared a non-blank D33D_DATA_DIR. We check intent, not the
// value — a declared path that happens to resolve to ~/.d33d is the
// operator's explicit choice.
if (
  process.env.E2E_SKIP_SERVER &&
  shouldGuardE2eSkipServer(process.env.D33D_DATA_DIR)
) {
  throw new Error(
    "D33D_E2E_GUARD: E2E_SKIP_SERVER is set but D33D_DATA_DIR is not set (or is blank). " +
      "Export D33D_DATA_DIR before STARTING your dev server — the guard cannot change " +
      "an already-running server's resolved data dir — then re-run the suite. " +
      "Note: `npm run build` has already run as part of test:e2e; the failure is the " +
      "guard, not the build.",
  );
}

const webServerEntry = process.env.E2E_SKIP_SERVER
  ? undefined
  : {
      // `python -m d33d.main` (NOT `uvicorn d33d.app:create_app --factory`):
      // main.main() is what reads D33D_DATA_DIR and passes the file-backed
      // db/master-key/catalogue paths to create_app; the factory path
      // defaults db_path to ":memory:" and would ignore the env var.
      // Port comes from D33D_HTTP_PORT (env below), default 8080.
      command: "uv run python -m d33d.main",
      env: {
        D33D_DATA_DIR: suiteDataDir ?? os.tmpdir(),
        D33D_HTTP_PORT: "8080",
      },
      url: "http://localhost:8080/api/projects",
      // `false` (not `true`): the throwaway data dir is only effective if
      // this run's server is the one the specs talk to. A stale server
      // from a prior run (or a dev server) on 8080 would be reused, and
      // its DB would carry the prior run's projects — defeating the
      // isolation. The E2E_SKIP_SERVER escape (webServer: undefined) is
      // the operator's path for a self-managed backend.
      reuseExistingServer: false,
      cwd: "..",
    };

export default defineConfig({
  testDir: "./tests/e2e",
  timeout: 30_000,
  use: {
    baseURL: process.env.E2E_BASE_URL ?? "http://localhost:8080",
  },
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
  webServer: webServerEntry,
});

// Teardown: remove the throwaway data dir when the runner process exits.
// The config module is loaded once by the Playwright runner; its
// top-level code registers the handler, and the handler fires when the
// runner finishes (normal exit) or is interrupted (SIGINT/SIGTERM).
// Playwright kills the webServer child before the runner exits, so the
// dir is not in use by the time `rmSync` runs.
if (suiteDataDir !== null) {
  const dir = suiteDataDir;
  const cleanup = () => {
    try {
      fs.rmSync(dir, { recursive: true, force: true, maxRetries: 5 });
    } catch {
      // non-fatal: the OS reaps $TMPDIR on reboot
    }
  };
  process.once("exit", cleanup);
  process.once("SIGINT", () => { cleanup(); process.exit(130); });
  process.once("SIGTERM", () => { cleanup(); process.exit(143); });
}
