// Issue #336: parent watchdog (vitest globalSetup).
//
// When the run-vitest wrapper (web/scripts/run-vitest.mjs) spawns vitest, it
// passes its own pid via D33D_TEST_WRAPPER_PID. A SIGKILL to the wrapper is
// uncatchable by the wrapper itself, so vitest covers that side: the watchdog
// polls every second and, once the wrapper is gone (ESRCH on two consecutive
// polls — about 2 s) it SIGKILLs its own process group. Because the wrapper
// spawns vitest with `detached: true`, vitest is the group leader, so this
// takes every fork worker with it.
//
// The setup function returns a teardown that clears the polling interval.
//
// When D33D_TEST_WRAPPER_PID is unset (CI, a direct `vitest` invocation),
// the watchdog does nothing.

import { parseWrapperPid, startWatchdog } from "./scripts/parent-watchdog.mjs";

export default async function watchdogSetup() {
  const pid = parseWrapperPid(process.env.D33D_TEST_WRAPPER_PID);
  if (pid === null) {
    return () => {};
  }
  return startWatchdog(
    (p, s) => process.kill(p, s),
    pid,
    (signal) => {
      process.kill(-process.pid, signal);
    },
  );
}
