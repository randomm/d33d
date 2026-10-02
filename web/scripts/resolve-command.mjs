// Issue #336: resolve-command — side-effect-free extraction of the
// run-vitest wrapper's command-resolution logic.
//
// The wrapper must honour the D33D_TEST_COMMAND override ONLY when
// D33D_TEST_MODE === "1"; otherwise it resolves the real vitest binary.
// Extracted here so the gate can be unit-tested without spawning any
// real vitest (see the adversarial review: the "never spawn the real
// suite from a test" rule).
//
// The function is pure in its observable effect: it reads the resolved
// vitest package.json when `env` does not opt into the test command. It
// does not write files, spawn processes, or mutate global state.

import { readFileSync } from "node:fs";
import { createRequire } from "node:module";

/**
 * Resolve the vitest binary to an absolute path using the wrapper's own
 * location as the reference point.
 *
 * The wrapper's `import.meta.url` differs from the caller's, so we pass
 * the URL explicitly. Reading from the wrapper's URL guarantees we find
 * the vitest that the wrapper would resolve at runtime, regardless of the
 * caller's working directory.
 *
 * @param {string} url - `import.meta.url` of the calling module (the wrapper).
 * @returns {string} absolute path to the vitest binary, or "vitest" if no
 *   local vitest is resolvable.
 */
function resolveVitestBinFromUrl(url) {
  try {
    const requireHere = createRequire(url);
    const vitestPkg = requireHere.resolve("vitest/package.json");
    const pkg = JSON.parse(readFileSync(vitestPkg, "utf8"));
    const binEntry = typeof pkg.bin === "string" ? pkg.bin : pkg.bin?.vitest;
    if (typeof binEntry === "string") {
      const vitestDir = vitestPkg.slice(0, vitestPkg.lastIndexOf("/"));
      return `${vitestDir}/${binEntry}`;
    }
  } catch {
    // No local vitest resolvable — fall back to bare `vitest` so the
    // usual PATH lookup still applies.
  }
  return "vitest";
}

/**
 * Decide the command argv to spawn, given an environment and a reference
 * URL (the wrapper's `import.meta.url`).
 *
 * @param {Record<string, string | undefined>} env - environment variables.
 *   Relevant keys: `D33D_TEST_MODE`, `D33D_TEST_COMMAND`.
 * @param {string} url - `import.meta.url` of the calling module (the wrapper).
 * @returns {string[]} the argv for `spawn(argv[0], argv.slice(1), ...)`.
 */
export function resolveCommand(env, url) {
  const isTestMode = env.D33D_TEST_MODE === "1";
  if (isTestMode && env.D33D_TEST_COMMAND) {
    return env.D33D_TEST_COMMAND.split(" ");
  }
  return [resolveVitestBinFromUrl(url), "run"];
}
