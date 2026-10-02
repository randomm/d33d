// Issue #336: parent watchdog (vitest globalSetup).
//
// When the run-vitest wrapper (web/scripts/run-vitest.mjs) spawns vitest, it
// passes its own pid via D33D_TEST_WRAPPER_PID. A SIGKILL to the wrapper is
// uncatchable by the wrapper itself, so vitest covers that side: once per
// second it checks whether the wrapper is still alive, and on ESRCH (wrapper
// gone) it SIGKILLs its own process group. Because the wrapper spawns vitest
// with `detached: true`, vitest is the group leader, so this takes every
// fork worker with it.
//
// When D33D_TEST_WRAPPER_PID is unset (CI, a direct `vitest` invocation),
// the watchdog does nothing.

import { checkParent } from "./scripts/parent-watchdog.mjs";

const wrapperPid = process.env.D33D_TEST_WRAPPER_PID;

export default async function watchdogSetup() {
  const pid = wrapperPid ? Number(wrapperPid) : NaN;
  if (!Number.isInteger(pid) || pid <= 0) {
    return;
  }
  const interval = setInterval(() => {
    checkParent((p, s) => process.kill(p, s), pid, (signal) => {
      process.kill(-process.pid, signal);
    });
  }, 1000);
  interval.unref();
}
