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
//   minimal PATH would otherwise ENOENT. The bin file name is read from
//   vitest's package.json (`pkg.bin.vitest`, or `pkg.bin` if it is a
//   string), not hard-coded.
// - Exit code: vitest's own code on normal completion; 128 + signal number
//   when interrupted (SIGTERM → 143, SIGINT → 130, SIGHUP → 129).

import { execFileSync, spawn } from "node:child_process";
import { resolveCommand } from "./resolve-command.mjs";

const GRACE_MS = 3000;

// The command to spawn. Delegates to the pure `resolveCommand` in
// ./resolve-command.mjs (unit-tested without spawning any real vitest —
// see the adversarial review for the "never spawn the real suite from a
// test" rule).
//
// Semantics: D33D_TEST_COMMAND is honoured ONLY when D33D_TEST_MODE === "1".
// Otherwise the real vitest bin is resolved from the wrapper's own location
// (PATH-independent), then `"run"` is appended.
const baseCommand = resolveCommand(process.env, import.meta.url);
const forwardedArgs = process.argv.slice(2);

// ---------------------------------------------------------------------------
// Group-death probing (used on the child's exit path to confirm the group is
// gone before the wrapper exits).
// ---------------------------------------------------------------------------

function killIgnoreESRCH(target, signal) {
  try {
    process.kill(target, signal);
  } catch (err) {
    // ESRCH: the process (or group) is already gone — exactly what we want.
    if (!err || err.code !== "ESRCH") {
      throw err;
    }
  }
}

/** True if `process.kill(-pgid, 0)` reports the group gone (ESRCH). */
function groupProbeESRCH(pgid) {
  try {
    process.kill(-pgid, 0);
    return false;
  } catch (err) {
    return Boolean(err) && err.code === "ESRCH";
  }
}

/**
 * True if `ps` reports no process whose pgid is `pgid`. This is the EPERM
 * fallback: `process.kill(-pgid, 0)` can EPERM when the group leader is
 * gone but members are mid-reap; asking `ps` for membership is reliable
 * because every process of a dying group is inspectable (or already gone).
 * Returns null when `ps` is unavailable (the caller must not assume).
 */
function psGroupEmpty(pgid) {
  try {
    const out = execFileSync("ps", ["-axo", "pgid="], { encoding: "utf8" });
    const seen = new Set(
      out
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean),
    );
    return !seen.has(String(pgid));
  } catch {
    return null;
  }
}

/**
 * Kill the child group, then confirm it is actually gone before returning.
 * EPERM on the kill is treated as "the group is mid-teardown"; the probe
 * below decides. If the group has been unprobeable (EPERM and no `ps`)
 * for more than 2 s we fail fast with a clear message — no blind retry to
 * the full timeout.
 */
function killGroupAndConfirm(pid) {
  // Kill the child directly first: the child can also be a member of the
  // wrapper's own process group (inherited from npm's group), in which case
  // a bare group kill on the child's pid can EPERM; the direct kill always
  // reaches the leader. The group kill then takes any fork workers the
  // child spawned (they inherit the child's group, not the wrapper's).
  killIgnoreESRCH(pid, "SIGTERM");
  killIgnoreESRCH(-pid, "SIGTERM");

  const start = Date.now();
  for (;;) {
    if (groupProbeESRCH(pid)) {
      return;
    }
    if (Date.now() - start > 2000) {
      const empty = psGroupEmpty(pid);
      if (empty === true) {
        return;
      }
      throw new Error(
        `child group ${pid} unprobeable after 2 s (kill -pgid EPERM and ` +
          (empty === null ? "ps unavailable" : "ps still lists the group"),
      );
    }
    Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 50);
  }
}

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
  // The grace timer is intentionally ref'd (not unref'd): it keeps the
  // wrapper alive through the grace window so the escalation can fire if
  // the child survives.
  killGroup("SIGTERM");
  setTimeout(() => {
    killGroup("SIGKILL");
    process.exit(exitCode);
  }, GRACE_MS);
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
  // Normal completion: reap the group, confirm it is gone (EPERM → ps
  // fallback, fail fast at 2 s unprobeable), and exit with vitest's code.
  killGroupAndConfirm(child.pid);
  process.exit(code ?? 0);
});

child.on("error", (err) => {
  if (!err || err.code !== "ESRCH") {
    console.error(err);
  }
  process.exit(1);
});
