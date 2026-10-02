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
 * @param {(pid: number, signal: number | string) => void} probe - the kill
 *   signal probe; injected so tests can fake liveness without a real process.
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
 * Retained for unit-testability: a single-poll "alive/dead" verdict is
 * what the tests assert against without driving the full timer loop.
 *
 * @param {(pid: number, signal: number | string) => void} probe - the kill
 *   signal probe.
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

/**
 * Parse and validate the D33D_TEST_WRAPPER_PID env value for the watchdog
 * setup. Pure, so the "bad pid" warning path is unit-testable without
 * touching the real environment.
 *
 * @param {string | undefined} raw - the raw env value.
 * @param {() => void} warn - where the warning line goes (injected).
 * @returns {number | null} a usable pid, or null when the watchdog must
 *   not run (unset, non-integer, or non-positive).
 */
export function parseWrapperPid(raw, warn = (line) => console.error(line)) {
  const pid = raw ? Number(raw) : NaN;
  if (!Number.isInteger(pid) || pid <= 0) {
    warn(
      "d33d: D33D_TEST_WRAPPER_PID is set but not a valid pid; " +
        "the SIGKILL watchdog is disabled for this run.",
    );
    return null;
  }
  return pid;
}

/**
 * Build the polling loop used by the vitest globalSetup.
 *
 * The parent must be observed gone (ESRCH) on TWO consecutive polls — about
 * 2 s apart by default — before the group is killed, so a single-tick false
 * positive (e.g. a pid briefly unresolvable during a fork/reap window) can
 * never SIGKILL a live group.
 *
 * The kill is retried on the next tick if it throws (e.g. the group is
 * mid-teardown and the kill EPERMs): the timer stays armed and the streak
 * is kept until the kill succeeds or the teardown clears the timer.
 *
 * @param {(pid: number, signal: number | string) => void} probe - the
 *   `process.kill` probe.
 * @param {number} pid - the parent (wrapper) pid.
 * @param {(signal: string) => void} killGroup - the SIGKILL-of-the-group
 *   action.
 * @param {object} [opts]
 * @param {number} [opts.pollMs=1000] - interval between polls.
 * @param {ReturnType<typeof setInterval>} [opts._interval] - injectable
 *   timer (tests); defaults to `setInterval`.
 * @returns {() => void} a teardown that clears the interval. The caller
 *   must invoke it in the corresponding globalTeardown.
 */
export function startWatchdog(probe, pid, killGroup, { pollMs = 1000, _interval } = {}) {
  let eSrchStreak = 0;
  let timer =
    _interval ??
    setInterval(() => {
      if (parentAlive(probe, pid)) {
        eSrchStreak = 0;
        return;
      }
      eSrchStreak += 1;
      if (eSrchStreak >= 2) {
        try {
          killGroup("SIGKILL");
          clearTimer(timer);
        } catch (err) {
          // The group kill can throw (e.g. EPERM while the group is
          // mid-teardown). Keep the timer armed and the streak: the next
          // tick retries the kill. The teardown clears the timer if the
          // kill never succeeds.
          console.error(
            "d33d: watchdog group kill failed; will retry on the next tick: " +
              (err && err.message ? err.message : String(err)),
          );
        }
      }
    }, pollMs);
  return () => {
    clearTimer(timer);
  };
}

function clearTimer(timer) {
  if (typeof timer === "function") {
    timer();
    return;
  }
  clearInterval(timer);
}
