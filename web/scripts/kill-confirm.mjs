// Issue #336: kill-confirm — pure extraction of the run-vitest wrapper's
// group-kill-and-confirm logic (web/scripts/run-vitest.mjs).
//
// Three pure helpers live here so the wrapper's kill/confirm paths are
// unit-testable without a real process group (injected kill/probe/ps/clock/
// sleep/log):
//
// - `killChildAndGroup` is the ONE kill path: it kills the child pid, then
//   `-pid`, each in its own try, so a failure on the first never skips the
//   second. ESRCH is swallowed silently, every other errno is logged via
//   the injected `log` and never thrown — the wrapper's escalation timer,
//   signal handler and exit handler therefore can never lose a kill to an
//   unhandled throw.
//
// - `confirmGroupGone` decides whether the child's process group is actually
//   gone before the wrapper may exit. It can never throw: when the group is
//   unprobeable (kill EPERM) and `ps` cannot settle the question, the
//   result is a log line and a "not confirmed" outcome — the wrapper then
//   exits with the child's own code (or 128 + signal on the signal path)
//   and the parent watchdog remains the backstop.
//
// - `killGroupGone` is the exit-path composition of the two: it delivers
//   SIGTERM to the child + group via `killChildAndGroup`, then confirms the
//   group is gone via `confirmGroupGone`, and reports the confirmation
//   outcome. The wrapper binds the real probes (`process.kill(-pgid, 0)`,
//   `ps`) here.
//
// - `psGroupEmpty` is the real `ps` probe itself, kept here (not in the
//   wrapper) so the ps-timeout path is unit-testable WITHOUT importing
//   `run-vitest.mjs` (whose top-level `spawn` is the #336 orphaning hazard —
//   the wrapper is the process, and importing it inside a vitest worker
//   would spawn there). Its `exec` dependency is injectable for that test.

import { execFileSync } from "node:child_process";

/**
 * True if `ps` reports no process whose pgid is `pgid`. This is the EPERM
 * fallback probe: `process.kill(-pgid, 0)` can EPERM when the group leader
 * is gone but members are mid-reap; asking `ps` for membership is reliable
 * because every process of a dying group is inspectable (or already gone).
 * Returns null when `ps` is unavailable (the caller must not assume).
 *
 * `timeout: 1000` bounds the probe: a wedged `ps` throws into the same
 * catch, so the function still returns null — the "ps unavailable" branch
 * of `confirmGroupGone` (the fallback contract is unchanged). `exec` is
 * injectable so the timeout path can be tested with an injected failing
 * `ps` — no real `ps` process is spawned.
 *
 * @param {number} pgid - the group pid to probe.
 * @param {object} [deps]
 * @param {(cmd: string, args: string[], o: { encoding: string; timeout?: number }) => string} [deps.exec] -
 *   the exec binding (the wrapper binds it to `execFileSync`).
 * @returns {boolean | null} true if `ps` lists no member of `pgid`, false
 *   if it does, null if `ps` was unavailable (including a ps timeout).
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

/**
 * Kill the child pid, then its process group (`-pid`), with one signal.
 *
 * Each kill is in its own try: an error on the direct child kill must never
 * skip the group kill (which takes the fork workers). ESRCH means "already
 * gone" — exactly what the kill path wants, so it is swallowed silently.
 * Any other errno is logged (via the injected `log`) and swallowed: the
 * caller's escalation / watchdog / confirmation remains the backstop, and
 * a throw here would drop timers in the signal/exit paths. Never throws.
 *
 * @param {object} deps
 * @param {(pid: number, signal: string) => void} deps.kill - the process
 *   kill (the wrapper binds it to `process.kill`).
 * @param {(line: string) => void} deps.log - where errno warnings go.
 * @param {number} pid - the child pid (the group leader).
 * @param {string} signal - the signal to deliver ("SIGTERM"/"SIGKILL").
 */
export function killChildAndGroup({ kill, log }, pid, signal) {
  for (const target of [pid, -pid]) {
    try {
      kill(target, signal);
    } catch (err) {
      if (!err || err.code === "ESRCH") {
        continue; // gone already — the desired outcome
      }
      log(
        `d33d: kill(${target}, ${signal}) failed — ` +
          (err && err.message ? err.message : String(err)),
      );
    }
  }
}

/**
 * Kill the child + group with SIGTERM, then confirm the group is gone.
 *
 * Exit-path composition of `killChildAndGroup` + `confirmGroupGone`, kept
 * pure (kill/probe/ps/now/sleep/log all injected) so the wrapper's child-
 * exit path is unit-testable without a real process group.
 *
 * @param {object} deps
 * @param {(pid: number, signal: string) => void} deps.kill - the process
 *   kill (the wrapper binds it to `process.kill`).
 * @param {() => boolean} deps.probe - true if the group probe reports
 *   ESRCH (group gone).
 * @param {() => boolean | null} deps.ps - `ps` membership fallback.
 * @param {() => number} deps.now - injected clock (ms).
 * @param {(ms: number) => void} deps.sleep - injected wait.
 * @param {(line: string) => void} deps.log - where kill/confirm log lines
 *   go.
 * @param {number} pid - the child pid (the group leader).
 * @param {object} [opts]
 * @param {number} [opts.graceMs=2000] - confirmation grace budget.
 * @param {number} [opts.tickMs=50] - wait between confirmation probes.
 * @returns {boolean} the confirmation outcome (true = confirmed gone,
 *   false = not confirmed; the caller logs via `deps.log` either way).
 */
export function killGroupGone(
  { kill, probe, ps, now, sleep, log },
  pid,
  { graceMs = 2000, tickMs = 50 } = {},
) {
  killChildAndGroup({ kill, log }, pid, "SIGTERM");
  return confirmGroupGone({ probe, ps, now, sleep, log }, { graceMs, tickMs });
}

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
