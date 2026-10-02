// Issue #336: parent-watchdog — pure check-and-kill logic, side-effect-free.
//
// The run-vitest wrapper spawns vitest with `detached: true`, so vitest
// leads its own process group. When the harness SIGKILLs the wrapper
// (uncatchable), vitest would be orphaned and keep running. This module
// gives vitest a way to detect that: the wrapper passes its own pid via
// D33D_TEST_WRAPPER_PID, and a vitest globalSetup polls the watchdog below
// once per second. On ESRCH the wrapper is gone, so the vitest process
// kills its own group (`-process.pid`), taking all fork workers with it.
//
// Exported as pure functions so the tests can drive the logic without
// starting a real vitest.

/**
 * Is the parent process at `pid` still alive?
 *
 * @param {(pid: number) => void} probe - the kill signal probe; injected so
 *   tests can fake liveness without a real process.
 * @param {number} pid - the parent pid to check.
 * @returns {boolean} true if the parent is alive, false if it threw ESRCH
 *   (dead). Any other error is treated as alive (be conservative: never
 *   kill the group on an ambiguous probe).
 */
export function parentAlive(probe, pid) {
  try {
    probe(pid, 0);
    return true;
  } catch (err) {
    if (err && err.code === "ESRCH") {
      return false;
    }
    return true;
  }
}

/**
 * Decide what to do after one poll of the parent.
 *
 * @param {(pid: number) => void} probe - the kill signal probe.
 * @param {number} pid - the parent (wrapper) pid.
 * @param {(signal: string) => void} killGroup - called with "SIGKILL" when
 *   the parent is gone; the caller binds it to process.kill(-process.pid).
 * @returns {"alive"|"dead"} - the watchdog verdict, for test assertions.
 */
export function checkParent(probe, pid, killGroup) {
  if (parentAlive(probe, pid)) {
    return "alive";
  }
  killGroup("SIGKILL");
  return "dead";
}
