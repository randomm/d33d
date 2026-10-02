// Issue #336: run-vitest — spawn vitest in its own process group so a
// timeout or a killed agent shell can never leave vitest fork workers
// running on the host.
//
// - `npm test` (the frontend gate) runs this wrapper instead of `vitest run`
//   directly.
// - The child is spawned with `detached: true`, so it leads its own process
//   group (pgid == child.pid). tinypool fork workers inherit that group, so
//   killing the group kills everything.
// - stdio is inherited, so the provenance "gate:" lines and the vitest
//   summary still print exactly as today.
// - On SIGTERM/SIGINT/SIGHUP (or its own exit) the wrapper kills the whole
//   child group: SIGTERM, then SIGKILL after a 3 s grace. Idempotent,
//   swallows ESRCH.
// - SIGKILL to the wrapper is uncatchable; the vitest side is covered by the
//   parent watchdog (web/vitest.watchdog.ts + scripts/parent-watchdog.mjs),
//   which kills the vitest group within ~2 s once it sees the wrapper gone.
//   The wrapper advertises its own pid via D33D_TEST_WRAPPER_PID.
// - The vitest binary is resolved to an ABSOLUTE path (via createRequire
//   from the wrapper's own location), so the spawn never depends on PATH —
//   a bare `vitest` is only findable when node_modules/.bin is on PATH
//   (e.g. under `npm run`); a direct `node scripts/run-vitest.mjs` with a
//   minimal PATH would otherwise ENOENT.
// - Exit code: vitest's own code on normal completion; 128 + signal number
//   when interrupted (SIGTERM → 143, SIGINT → 130, SIGHUP → 129).

import { spawn } from "node:child_process";
import { createRequire } from "node:module";

const GRACE_MS = 3000;

// The command to spawn. Overridable by the tests via D33D_TEST_COMMAND:
// space-separated argv, so the fixture is a tiny node script instead of the
// real vitest (the wrapper must never run the outer suite's own vitest).
const testCommand = process.env.D33D_TEST_COMMAND ? process.env.D33D_TEST_COMMAND.split(" ") : null;

// Resolve the real vitest binary to an ABSOLUTE path so the spawn never
// depends on PATH. Under `npm run`, node_modules/.bin is on PATH, but a
// direct `node scripts/run-vitest.mjs` can have a minimal PATH and would
// ENOENT on a bare `vitest`. We resolve from the wrapper's own location via
// createRequire: it finds the local vitest regardless of PATH. The bin is
// the vitest package's `vitest.mjs` (its declared bin entry).
function resolveVitestBin() {
  try {
    const requireHere = createRequire(import.meta.url);
    const vitestPkg = requireHere.resolve("vitest/package.json");
    const vitestDir = vitestPkg.slice(0, vitestPkg.lastIndexOf("/"));
    return `${vitestDir}/vitest.mjs`;
  } catch {
    // No local vitest resolvable (e.g. no node_modules) — fall back to a
    // bare `vitest` so the usual PATH lookup still applies.
    return "vitest";
  }
}
const vitestBin = resolveVitestBin();
const baseCommand = testCommand ?? [vitestBin, "run"];
const forwardedArgs = process.argv.slice(2);

const child = spawn(baseCommand[0], [...baseCommand.slice(1), ...forwardedArgs], {
  detached: true,
  stdio: "inherit",
  env: {
    ...process.env,
    D33D_TEST_WRAPPER_PID: String(process.pid),
  },
});

let interrupted = false;
let interruptCode = 0;

function killGroup(signal) {
  if (!child.pid) {
    return;
  }
  // Kill the child directly first: the child can also be a member of the
  // wrapper's own process group (inherited from npm's group), in which case
  // a bare group kill on the child's pid can EPERM; the direct kill always
  // reaches the leader. The group kill then takes any fork workers the
  // child spawned (they inherit the child's group, not the wrapper's).
  try {
    process.kill(child.pid, signal);
  } catch (err) {
    if (!err || err.code !== "ESRCH") {
      throw err;
    }
  }
  try {
    process.kill(-child.pid, signal);
  } catch (err) {
    // ESRCH: the group is already gone — exactly what we want.
    if (!err || err.code !== "ESRCH") {
      throw err;
    }
  }
}

function onSignal(exitCode) {
  if (interrupted) {
    return;
  }
  interrupted = true;
  interruptCode = exitCode;
  // SIGTERM now, SIGKILL after the grace period, in case the child traps it.
  // The grace timer is NOT unref'd: it keeps the wrapper alive through the
  // grace window so the escalation can fire if the child survives.
  killGroup("SIGTERM");
  const grace = setTimeout(() => {
    killGroup("SIGKILL");
    process.exit(exitCode);
  }, GRACE_MS);
  // The grace timer is intentionally ref'd (not unref'd).
  void grace;
}

process.on("SIGTERM", () => onSignal(143));
process.on("SIGINT", () => onSignal(130));
process.on("SIGHUP", () => onSignal(129));

// On the wrapper's own exit (any reason), make sure the group is dead too.
// Idempotent: the kill swallows ESRCH.
process.on("exit", () => {
  killGroup("SIGTERM");
});

child.on("exit", (code) => {
  if (interrupted) {
    // Signal path: the child died to the wrapper's SIGTERM (or the grace
    // timer's SIGKILL has already fired and exited). Exit with the signal's
    // code so the interrupted run never reads as a pass.
    process.exit(interruptCode);
  }
  // Normal completion: reap the group (idempotent) and exit with vitest's
  // own code.
  killGroup("SIGTERM");
  process.exit(code ?? 0);
});

child.on("error", (err) => {
  if (!err || err.code !== "ESRCH") {
    console.error(err);
  }
  process.exit(1);
});
