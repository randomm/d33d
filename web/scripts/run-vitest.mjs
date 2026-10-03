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
// - The wrapper's process effects (the `spawn`, the signal/exit handlers)
//   live inside `runMain`, invoked only when the module is executed as the
//   entrypoint (`node scripts/run-vitest.mjs`). Importing the module — e.g.
//   the `psGroupEmpty` unit test in the vitest suite — pulls in the pure
//   exports only; nothing is spawned and no handler is registered in the
//   importing process.

import { execFileSync, spawn } from "node:child_process";
import { fileURLToPath } from "node:url";
import { killChildAndGroup, killGroupGone } from "./kill-confirm.mjs";
import { resolveCommand } from "./resolve-command.mjs";

const GRACE_MS = 3000;

// ---------------------------------------------------------------------------
// Group-death probing (used on the child's exit path to confirm the group is
// gone before the wrapper exits). The decision logic itself is the pure
// `confirmGroupGone` in ./kill-confirm.mjs; the wrappers below are the
// real-process bindings.
// ---------------------------------------------------------------------------

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
 * `timeout: 1000` bounds the probe: a wedged `ps` throws into the same
 * catch, so the function still returns null — the "ps unavailable" branch
 * of confirmGroupGone (the kill-confirm.mjs fallback contract is
 * unchanged). Exported (with an injectable exec) for the unit test that
 * covers the timeout path with an injected failing `ps` — no real `ps`
 * process is spawned.
 */
export function psGroupEmpty(pgid, { exec = execFileSync } = {}) {
  try {
    const out = exec("ps", ["-axo", "pgid="], { encoding: "utf8", timeout: 1000 });
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

// ---------------------------------------------------------------------------
// The wrapper's process effects: resolve the command, spawn the child in its
// own process group, and install the signal/exit handlers. Everything below
// runs ONLY when this module is the entrypoint — an `import` of the module
// (the unit test for `psGroupEmpty`) never reaches it.
// ---------------------------------------------------------------------------

async function runMain() {
  // The command to spawn. Delegates to the pure `resolveCommand` in
  // ./resolve-command.mjs (unit-tested without spawning any real vitest —
  // see the adversarial review for the "never spawn the real suite from a
  // test" rule).
  //
  // Semantics: D33D_TEST_COMMAND is honoured ONLY when D33D_TEST_MODE ===
  // "1". Otherwise the real vitest bin is resolved from the wrapper's own
  // location (PATH-independent), then `"run"` is appended.
  const baseCommand = resolveCommand(process.env, import.meta.url);
  const forwardedArgs = process.argv.slice(2);

  // Shared by every confirmation sleep — Atomics.wait needs an Int32Array
  // over a SharedArrayBuffer; allocate once per run, not per iteration.
  const waitBuf = new Int32Array(new SharedArrayBuffer(4));

  // The wrapper's single kill path: kill the child pid, then `-pid`, via the
  // pure `killChildAndGroup`. Never throws — the escalation, the signal
  // handler and the exit handler all rely on the second kill always running.
  // ESRCH is silent by construction; every other errno is written to stderr
  // via `log` so a failed kill (EPERM mid-reap, a vanished group, anything
  // else) is visible to the operator instead of swallowed.
  const KILL_DEPS = {
    kill: (target, signal) => process.kill(target, signal),
    log: (line) => console.error(line),
  };

  /**
   * Kill the child group on the normal-completion path, then confirm it is
   * actually gone before the wrapper exits. The decision logic is the pure
   * `killGroupGone` in ./kill-confirm.mjs (SIGTERM to child + group, then
   * confirm with an EPERM → `ps` fallback); the bindings below are the
   * real-process ones. The "child group <pid>" prefix makes the stderr lines
   * unambiguous about which kill path emitted them. If the group is
   * unprobeable (EPERM and no `ps`) past the grace, `confirmGroupGone` logs
   * and returns false — the wrapper then exits with the child's code and the
   * parent watchdog remains the backstop. The confirmation never throws.
   */
  function killGroupAndConfirm(pid) {
    killGroupGone(
      {
        ...KILL_DEPS,
        probe: () => groupProbeESRCH(pid),
        ps: () => psGroupEmpty(pid),
        now: Date.now,
        sleep: (ms) => Atomics.wait(waitBuf, 0, 0, ms),
        log: (line) => console.error(`d33d: child group ${pid} — ${line}`),
      },
      pid,
      { graceMs: 2000, tickMs: 50 },
    );
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
    killChildAndGroup(KILL_DEPS, child.pid, signal);
  }

  function onSignal(exitCode) {
    if (interrupted) {
      return;
    }
    interrupted = true;
    interruptCode = exitCode;
    // SIGTERM now; SIGKILL fires once after the grace, whatever the SIGTERM's
    // outcome (killChildAndGroup never throws, so a failed SIGTERM cannot
    // drop the escalation). Retries on kill failure exist only in the parent
    // watchdog. The grace timer is intentionally ref'd (not unref'd): it
    // keeps the wrapper alive through the grace window so the escalation can
    // fire if the child traps SIGTERM.
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
  // killChildAndGroup never throws, so this handler can never throw.
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
}

// Entry-point guard: the process effects above run only when this module is
// executed directly (`node scripts/run-vitest.mjs`), never when it is
// imported. `process.argv[1]` is undefined when the entrypoint is stdin or a
// REPL — never this file — so the guard is strict.
if (process.argv[1] === fileURLToPath(import.meta.url)) {
  void runMain();
}
