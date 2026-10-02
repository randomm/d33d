// Issue #336: kill-confirm — pure extraction of the run-vitest wrapper's
// group-kill-and-confirm logic (web/scripts/run-vitest.mjs).
//
// The confirmation decides whether the child's process group is actually
// gone before the wrapper may exit. It can never throw: when the group is
// unprobeable (kill EPERM) and `ps` cannot settle the question, the result
// is a log line and a "not confirmed" outcome — the wrapper then exits
// with the child's own code (or 128 + signal on the signal path) and the
// parent watchdog remains the backstop.
//
// Everything (probe, ps, clock, sleep, log) is injected, so the unprobeable
// branch is unit-testable without a real process group.

/**
 * Confirm the child group is gone before the wrapper exits.
 *
 * The group is confirmed gone when the probe reports ESRCH, or (after the
 * grace budget) when `ps` reports no member. `ps === null` (unavailable)
 * or `ps === false` (still lists the group) means "not confirmed": log the
 * outcome and stop — never throw, never hide the failure.
 *
 * @param {object} deps
 * @param {() => boolean} deps.probe - true if `kill(-pgid, 0)` reports
 *   ESRCH (group gone).
 * @param {() => boolean | null} deps.ps - `ps` membership fallback:
 *   true = no member, false = still listed, null = unavailable.
 * @param {() => number} deps.now - injected clock (ms).
 * @param {(ms: number) => void} deps.sleep - injected wait (the wrapper
 *   binds it to an Atomics.wait-based tick).
 * @param {(line: string) => void} deps.log - where the confirmation log
 *   goes (the wrapper binds it to console.error).
 * @param {object} [opts]
 * @param {number} [opts.graceMs=2000] - how long to keep probing before
 *   deferring to `ps` / giving up.
 * @param {number} [opts.tickMs=50] - wait between probes.
 * @returns {boolean} true when the group was confirmed gone (the wrapper
 *   exits with the child's code on the normal path, or 128 + signo on the
 *   signal path), false when the confirmation could not be established
 *   (logged; the wrapper still exits with that same code and the parent
 *   watchdog remains the backstop).
 */
export function confirmGroupGone(
  { probe, ps, now, sleep, log },
  { graceMs = 2000, tickMs = 50 } = {},
) {
  const start = now();
  for (;;) {
    if (probe()) {
      return true;
    }
    if (now() - start > graceMs) {
      const empty = ps();
      if (empty === true) {
        return true;
      }
      log(
        empty === null
          ? "d33d: child group unprobeable; ps unavailable — continuing to exit, the parent watchdog remains the backstop."
          : "d33d: child group unprobeable; ps still lists the group — continuing to exit, the parent watchdog remains the backstop.",
      );
      return false;
    }
    sleep(tickMs);
  }
}
