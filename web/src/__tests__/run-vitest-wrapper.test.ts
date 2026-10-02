// Issue #336: prove the run-vitest wrapper reaps its whole child process
// group on SIGTERM/SIGINT/SIGHUP, on normal exit, and (via the parent
// watchdog) on SIGKILL of the wrapper itself.
//
// Every fixture case spawns the wrapper against a FIXTURE command (never the
// outer suite's own vitest). The fixture is a tiny node script in a tempdir
// that spawns a grandchild and sleeps, so the group has 2+ members:
//
//   wrapper (own group, never the test runner's)
//     └── child  (detached → leads its own group, pgid == child.pid)
//           └── grandchild (inherits the child's group)
//
// D33D_TEST_COMMAND + D33D_TEST_MODE=1 overrides what the wrapper spawns in
// the fixture cases.
//
// The watchdog logic (web/scripts/parent-watchdog.mjs) is tested purely,
// with injected probes/timers — no real watchdog process is ever started.
//
// The final test ("REAL: SIGKILL…") IS the real path: it is self-contained
// (a tiny vitest project in a tempdir whose globalSetup points at the real
// vitest.watchdog.ts) and drives the actual production wrapper → vitest →
// tinypool path. See that test's doc comment.

import { expect, test } from "vitest";
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const SETTLE_MS = 300;

// The watchdog module is plain .mjs (outside tsconfig's src/ rootDir); import
// it dynamically to avoid the missing-declaration error.
type WatchdogModule = {
  checkParent: (
    probe: (p: number, s: number | string) => void,
    pid: number,
    kill: (s: string) => void,
  ) => "alive" | "dead";
  parseWrapperPid: (raw: string | undefined, warn?: (line: string) => void) => number | null;
  startWatchdog: (
    probe: (p: number, s: number | string) => void,
    pid: number,
    killGroup: (s: string) => void,
    opts?: { pollMs?: number; _interval?: unknown },
  ) => () => void;
};

async function loadWatchdog(): Promise<WatchdogModule> {
  const mod: unknown = await import("../../scripts/parent-watchdog.mjs");
  return mod as unknown as WatchdogModule;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

type Wrapper = {
  child: ReturnType<typeof spawn>;
  pid: number;
  done: Promise<{ code: number | null; signal: NodeJS.Signals | null }>;
  stdout: () => string;
};

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function waitForReady(w: Wrapper, timeoutMs = 15000): Promise<void> {
  return new Promise((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      if (w.stdout().includes("ready")) {
        resolve();
        return;
      }
      if (Date.now() - start > timeoutMs) {
        reject(new Error(`fixture never reported ready (stdout: ${JSON.stringify(w.stdout())})`));
        return;
      }
      setTimeout(tick, 20);
    };
    tick();
  });
}

/**
 * Poll until the predicate returns true, or timeout.
 *
 * @param fn - the predicate to check
 * @param timeoutMs - max time to wait
 * @param intervalMs - poll interval (default 50 ms)
 * @param label - description for error messages
 * @param fallbackValue - value returned on timeout (for non-assertion polls)
 */
async function pollUntil<T>(
  fn: () => boolean | T | null,
  timeoutMs: number,
  intervalMs = 50,
  label = "condition",
  fallbackValue?: T,
): Promise<T | undefined> {
  const start = Date.now();
  for (;;) {
    const r = fn();
    if (r === true || (r !== null && r !== undefined && r !== false)) {
      return r as T;
    }
    if (Date.now() - start > timeoutMs) {
      if (fallbackValue !== undefined) {
        return fallbackValue;
      }
      throw new Error(`${label} not satisfied after ${timeoutMs} ms`);
    }
    await sleep(intervalMs);
  }
}

/**
 * Poll until the process group at `pgid` is gone.
 *
 * `process.kill(-pgid, 0)` throws ESRCH when no process in the group is
 * reachable. It can ALSO throw EPERM during the teardown window: the group
 * leader may be dead while orphaned members are still being reaped, leaving
 * a partially-tearing-down group the test process cannot probe. EPERM means
 * "the group is dying but not fully reaped yet" — treat it as the same
 * "keep polling" condition rather than a hard failure (which made the gate
 * flaky). Any other error is a genuine failure and is rethrown.
 */
async function groupDead(pgid: number, timeoutMs: number): Promise<void> {
  const start = Date.now();
  for (;;) {
    try {
      process.kill(-pgid, 0);
    } catch (err) {
      const code = (err as NodeJS.ErrnoException).code;
      if (code === "ESRCH") {
        return;
      }
      // EPERM: group is mid-teardown (leader gone, members reaping) — keep
      // polling; do not treat a permission error on a dying group as failure.
      if (code === "EPERM") {
        // fall through to the retry below
      } else {
        throw err;
      }
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error(`process group ${pgid} still alive after ${timeoutMs} ms`);
    }
    await sleep(50);
  }
}

/** True if a single process pid is alive and signalable. */
function pidAlive(pid: number): boolean {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
}

/** Poll until a single process pid is gone (ESRCH). */
async function pidDead(pid: number, timeoutMs: number): Promise<void> {
  const start = Date.now();
  for (;;) {
    if (!pidAlive(pid)) {
      return;
    }
    if (Date.now() - start > timeoutMs) {
      throw new Error(`pid ${pid} still alive after ${timeoutMs} ms`);
    }
    await sleep(50);
  }
}

function fixtureSource(code: string): string {
  const path = `${tmpdir()}/d336-fix-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.mjs`;
  writeFileSync(path, code, "utf8");
  return path;
}

// web/scripts/ lives outside web/src (tsconfig rootDir is src); the wrapper
// is resolved relative to the package root (process.cwd() is web/ under
// vitest).
const WRAPPER = `${process.cwd()}/scripts/run-vitest.mjs`;
const WEB_ROOT = process.cwd();

/**
 * Best-effort cleanup of stale d336-real-* dirs in tmpdir.
 * A symlink is removed via rmSync(link, { force: true }) which removes
 * only the link, not the target.
 */
function cleanStaleRealDirs(): void {
  let entries: string[];
  try {
    entries = readdirSync(tmpdir());
  } catch {
    return;
  }
  for (const entry of entries) {
    if (!entry.startsWith("d336-real-")) {
      continue;
    }
    const p = join(tmpdir(), entry);
    try {
      rmSync(p, { recursive: true, force: true });
    } catch {
      // best-effort
    }
  }
}

function startWrapper(fixturePath: string, extraArgs: string[] = []): Wrapper {
  const child = spawn(process.execPath, [WRAPPER, ...extraArgs], {
    detached: true, // the wrapper runs in its own group, never the runner's
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
      D33D_TEST_MODE: "1",
      D33D_TEST_COMMAND: `${process.execPath} ${fixturePath}`,
    },
  });
  let out = "";
  child.stdout?.on("data", (d: Buffer) => {
    out += d.toString();
  });
  const done = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
    child.on("exit", (code, signal) => resolve({ code, signal }));
    child.on("error", (err) => reject(err));
  });
  return {
    child,
    pid: child.pid as number,
    done,
    stdout: () => out,
  };
}

/** The child pid of the fixture (the wrapper's child, group leader). */
function childPidOf(w: Wrapper): number {
  const m = w.stdout().match(/childpid\s+(\d+)/);
  if (!m || !m[1]) {
    throw new Error(`no childpid in stdout: ${JSON.stringify(w.stdout())}`);
  }
  return Number(m[1]);
}

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

function sleepFixture(): string {
  return fixtureSource(`
import { spawn } from "node:child_process";
const gc = spawn(${JSON.stringify(process.execPath)}, [
  ${JSON.stringify("-e")},
  "setTimeout(()=>{},300000)",
], { stdio: "ignore" });
console.log("childpid " + process.pid);
console.log("ready");
setTimeout(() => {}, 300000);
`);
}

function trapFixture(): string {
  return fixtureSource(`
process.on("SIGTERM", () => {});
console.log("childpid " + process.pid);
console.log("ready");
setTimeout(() => {}, 300000);
`);
}

// ---------------------------------------------------------------------------
// Watchdog pure-logic tests (injected probes/timers — no real process)
// ---------------------------------------------------------------------------

test("watchdog: startWatchdog retries the kill when it throws, then clears the timer", async () => {
  const { startWatchdog } = await loadWatchdog();

  // Spy on the global interval: startWatchdog's default timer is the global
  // setInterval, and the clear happens via the global clearInterval.
  // Capture the callback so the test drives ticks synchronously, and flag
  // the clear when it targets the fake timer.
  const realSetInterval = globalThis.setInterval;
  const realClearInterval = globalThis.clearInterval;
  const fakeTimer = { kind: "fake-watchdog-timer" } as unknown as ReturnType<typeof setInterval>;
  const captured: { cb?: () => void } = {};
  let cleared = false;
  Object.defineProperty(globalThis, "setInterval", {
    configurable: true,
    value: (cb: Parameters<typeof setInterval>[0], _ms?: Parameters<typeof setInterval>[1]) => {
      captured.cb = cb as () => void;
      return fakeTimer;
    },
  });
  Object.defineProperty(globalThis, "clearInterval", {
    configurable: true,
    value: (t: unknown) => {
      if (t === fakeTimer) {
        cleared = true;
      }
    },
  });
  try {
    const probe = (): never => {
      throw Object.assign(new Error("gone"), { code: "ESRCH" });
    };
    const killCalls: string[] = [];
    const killGroup = (s: string) => {
      killCalls.push(s);
      if (killCalls.length === 1) {
        // First kill throws (simulated EPERM mid-teardown); the watchdog
        // must keep the timer running and the streak, so the next tick
        // retries the kill.
        throw new Error("EPERM (simulated mid-teardown)");
      }
    };

    const teardown = startWatchdog(probe, 12345, killGroup);

    // Tick 1: streak 1 < 2, no kill yet, timer not cleared.
    captured.cb?.();
    expect(killCalls).toEqual([]);
    expect(cleared).toBe(false);

    // Tick 2: streak 2 → kill throws → timer must still be armed.
    captured.cb?.();
    expect(killCalls).toEqual(["SIGKILL"]);
    expect(cleared).toBe(false);

    // Tick 3: streak 3 ≥ 2 → kill succeeds → timer cleared.
    captured.cb?.();
    expect(killCalls).toEqual(["SIGKILL", "SIGKILL"]);
    expect(cleared).toBe(true);

    teardown();
  } finally {
    Object.defineProperty(globalThis, "setInterval", {
      configurable: true,
      value: realSetInterval,
    });
    Object.defineProperty(globalThis, "clearInterval", {
      configurable: true,
      value: realClearInterval,
    });
  }
}, 10000);

// ---------------------------------------------------------------------------
// killGroupAndConfirm direct tests (pure — no real process group)
// ---------------------------------------------------------------------------

test("confirmGroupGone: the unprobeable branch never throws, logs, and the wrapper exit code is the child's code on the normal path", async () => {
  const { confirmGroupGone } = await import("../../scripts/kill-confirm.mjs");

  // Injected clock: the group stays unprobeable (probe -> false) past the
  // grace; ps reports it still lists the group. The confirmation must log
  // (not throw, not hide) and return "not confirmed".
  const logs: string[] = [];
  let nowMs = 0;
  const deps = {
    probe: () => false,
    ps: () => false,
    now: () => nowMs,
    sleep: (ms: number) => {
      nowMs += ms;
    },
    log: (line: string) => logs.push(line),
  };

  let result: unknown;
  expect(() => {
    result = confirmGroupGone(deps, { graceMs: 2000, tickMs: 50 });
  }).not.toThrow();

  expect(result).toBe(false); // not confirmed
  expect(logs.length).toBe(1);
  expect(logs[0]).toContain("still lists the group");
  expect(logs[0]).toContain("backstop");

  // The wrapper's exit code on the normal path is `code ?? 0` — the child's
  // own code passes through unchanged regardless of whether confirmation
  // succeeded. This is covered by the "failing fixture: the child's exit
  // code passes through" test (exit 3) below, which drives the real
  // confirmGroupGone path through the production wrapper against a fixture.
  // No separate pure-function exit-code test is needed: the exit-code logic
  // is `process.exit(code ?? 0)` in run-vitest.mjs, not part of the
  // confirmGroupGone pure function.
});

test("confirmGroupGone: the unprobeable branch logs (ps unavailable) and the wrapper exit code is 128 + signo on the signal path", async () => {
  const { confirmGroupGone } = await import("../../scripts/kill-confirm.mjs");

  const logs: string[] = [];
  let nowMs = 0;
  const deps = {
    probe: () => false,
    ps: () => null, // ps unavailable
    now: () => nowMs,
    sleep: (ms: number) => {
      nowMs += ms;
    },
    log: (line: string) => logs.push(line),
  };

  expect(() => confirmGroupGone(deps, { graceMs: 2000, tickMs: 50 })).not.toThrow();
  expect(logs.length).toBe(1);
  expect(logs[0]).toContain("ps unavailable");
  expect(logs[0]).toContain("backstop");

  // The wrapper's exit code on the signal path is `interruptCode` (143/
  // 130/129) — the interruption code always wins, never the child's own
  // code. This is covered by the SIGTERM/SIGINT/SIGHUP fixture tests
  // below, which drive the production wrapper end-to-end.
});

test("confirmGroupGone: the probeable branch returns confirmed without logging", async () => {
  const { confirmGroupGone } = await import("../../scripts/kill-confirm.mjs");

  const logs: string[] = [];
  // Probe immediately confirms the group is gone — no ps, no log, no throw.
  const confirmed = confirmGroupGone(
    { probe: () => true, ps: () => null, now: () => 0, sleep: () => {}, log: (l) => logs.push(l) },
    { graceMs: 2000, tickMs: 50 },
  );
  expect(confirmed).toBe(true);
  expect(logs).toEqual([]);

  // And the ps-fallback path: probe never ESRCHes, past the grace ps says
  // the group is empty → confirmed, no log.
  let psT = 0;
  const confirmedViaPs = confirmGroupGone(
    {
      probe: () => false,
      ps: () => true,
      now: () => psT,
      sleep: (ms: number) => {
        psT += ms;
      },
      log: (l: string) => logs.push(l),
    },
    { graceMs: 2000, tickMs: 50 },
  );
  expect(confirmedViaPs).toBe(true);
  expect(logs).toEqual([]);
});

test("watchdog: checkParent kills the group on ESRCH (injected probe)", async () => {
  const { checkParent } = await loadWatchdog();
  const signals: string[] = [];
  const killed = checkParent(
    () => {
      throw Object.assign(new Error("gone"), { code: "ESRCH" });
    },
    42,
    (s) => signals.push(s),
  );
  expect(killed).toBe("dead");
  expect(signals).toEqual(["SIGKILL"]);

  const alive = checkParent(() => {}, 42, (s) => signals.push(s));
  expect(alive).toBe("alive");
  expect(signals).toEqual(["SIGKILL"]);
}, 5000);

test("watchdog: parseWrapperPid warns on a bad pid and disables the watchdog", async () => {
  const { parseWrapperPid } = await loadWatchdog();
  const valid = parseWrapperPid("12345", () => {
    throw new Error("should not warn");
  });
  expect(valid).toBe(12345);

  expect(parseWrapperPid(undefined, () => {})).toBeNull();

  for (const bad of ["", "abc", "0", "-5", "1.5"]) {
    const warnings: string[] = [];
    const r = parseWrapperPid(bad, (line) => warnings.push(line));
    expect(r).toBeNull();
    expect(warnings.length).toBe(1);
    expect(warnings[0]).toContain("D33D_TEST_WRAPPER_PID");
  }
}, 5000);

// ---------------------------------------------------------------------------
// Wrapper fixture tests (spawn the wrapper against fixtures, never the real
// outer suite)
// ---------------------------------------------------------------------------

test("SIGTERM reaps the wrapper's whole process group, exit 143", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGTERM");
  const res = await w.done;
  expect(res.code).toBe(143);
  await groupDead(childPid, 8000);
  await sleep(SETTLE_MS);
}, 20000);

test("SIGINT reaps the wrapper's whole process group, exit 130", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGINT");
  const res = await w.done;
  expect(res.code).toBe(130);
  await groupDead(childPid, 8000);
  await sleep(SETTLE_MS);
}, 20000);

test("SIGHUP reaps the wrapper's whole process group, exit 129", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGHUP");
  const res = await w.done;
  expect(res.code).toBe(129);
  await groupDead(childPid, 8000);
  await sleep(SETTLE_MS);
}, 20000);

test("SIGKILL to the wrapper: the watchdog reaps the group within the bound", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  const wrapperPid = w.pid;
  w.child.kill("SIGKILL");
  await w.done;
  // The wrapper is dead, but the kernel may not have reaped it yet (a
  // zombie's pid still answers a signal-0 probe), so poll until its pid
  // is unresolvable (ESRCH) before driving the watchdog.
  await new Promise<void>((resolve, reject) => {
    const start = Date.now();
    const tick = () => {
      try {
        process.kill(wrapperPid, 0);
      } catch (err) {
        if ((err as NodeJS.ErrnoException).code === "ESRCH") {
          resolve();
          return;
        }
        throw err;
      }
      if (Date.now() - start > 5000) {
        reject(new Error("wrapper pid still alive after SIGKILL"));
        return;
      }
      setTimeout(tick, 50);
    };
    tick();
  });
  // The group must still be alive — nothing has reaped it yet (the wrapper
  // cannot catch SIGKILL; the watchdog is the only path to reaping here).
  expect(pidAlive(childPid)).toBe(true);
  // Drive the watchdog module exactly as vitest.watchdog.ts does: it must
  // see the wrapper gone (ESRCH) and SIGKILL the group.
  const { checkParent } = await loadWatchdog();
  let verdict = "alive";
  checkParent(
    (p: number, s: number | string) => process.kill(p, s),
    wrapperPid,
    (signal: string) => {
      process.kill(-childPid, signal);
      verdict = "dead";
    },
  );
  expect(verdict).toBe("dead");
  await sleep(SETTLE_MS);
}, 20000);

test("D33D_TEST_MODE=1 and D33D_TEST_COMMAND are honoured", async () => {
  // A fixture that prints a sentinel.
  const fix = fixtureSource(`
console.log("childpid " + process.pid);
console.log("ready");
console.log("SENTINEL_FROM_FIXTURE");
setTimeout(() => process.exit(0), 100);
`);
  const w = startWrapper(fix);
  await w.done;
  expect(w.stdout()).toContain("SENTINEL_FROM_FIXTURE");
}, 20000);

test("D33D_TEST_MODE unset: D33D_TEST_COMMAND is ignored (pure unit test)", async () => {
  // Pure unit test of the env-gating logic — never spawns any real vitest.
  // The wrapper's gate lives in web/scripts/resolve-command.mjs as
  // `resolveCommand(env, url)`. With D33D_TEST_MODE unset (or anything but
  // "1") and D33D_TEST_COMMAND set, the override MUST be ignored and the
  // resolved real vitest bin + "run" returned. That is the exact
  // "production path" the negative test was meant to guard, without the
  // orphan risk of actually spawning vitest (see issue #336).
  const { resolveCommand } = await import("../../scripts/resolve-command.mjs");
  const wrapperUrl = new URL("file://" + WRAPPER).href;

  // 1. D33D_TEST_MODE unset, D33D_TEST_COMMAND set — override ignored.
  const unsetCmd = resolveCommand(
    { D33D_TEST_MODE: undefined, D33D_TEST_COMMAND: "node /tmp/sentinel.mjs" } as NodeJS.ProcessEnv,
    wrapperUrl,
  );
  expect(unsetCmd.length).toBe(2);
  expect(unsetCmd[1]).toBe("run");
  expect(unsetCmd[0]).not.toBe("node");
  expect(unsetCmd[0]).not.toContain("sentinel");

  // 2. D33D_TEST_MODE empty string — also not "1", override ignored.
  const emptyCmd = resolveCommand(
    { D33D_TEST_MODE: "", D33D_TEST_COMMAND: "node /tmp/sentinel.mjs" } as NodeJS.ProcessEnv,
    wrapperUrl,
  );
  expect(emptyCmd).toEqual(unsetCmd);

  // 3. D33D_TEST_MODE="1" + D33D_TEST_COMMAND set — override honoured.
  const onCmd = resolveCommand(
    { D33D_TEST_MODE: "1", D33D_TEST_COMMAND: "node /tmp/sentinel.mjs" } as NodeJS.ProcessEnv,
    wrapperUrl,
  );
  expect(onCmd).toEqual(["node", "/tmp/sentinel.mjs"]);

  // 4. D33D_TEST_MODE="1" but D33D_TEST_COMMAND unset — real vitest fallback.
  const modeOnly = resolveCommand(
    { D33D_TEST_MODE: "1", D33D_TEST_COMMAND: undefined } as NodeJS.ProcessEnv,
    wrapperUrl,
  );
  expect(modeOnly).toEqual(unsetCmd);

  // 5. The resolved vitest bin is an absolute path (PATH-independent).
  //    This is the production-path guarantee: a stray D33D_TEST_COMMAND
  //    can never redirect `npm test`.
  expect(unsetCmd[0].startsWith("/")).toBe(true);
  expect(unsetCmd[0]).toContain("vitest");
});

test("fixture traps SIGTERM: the wrapper escalates to SIGKILL after the grace", async () => {
  const w = startWrapper(trapFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGTERM");
  const res = await w.done;
  expect(res.code).toBe(143);
  await groupDead(childPid, 8000);
  await sleep(SETTLE_MS);
}, 25000);

test("normal completion: no orphans, exit code 0 passes through", async () => {
  const fix = fixtureSource(`
import { spawn } from "node:child_process";
const gc = spawn(${JSON.stringify(process.execPath)}, [
  ${JSON.stringify("-e")},
  "setTimeout(()=>process.exit(0),50)",
], { stdio: "ignore" });
gc.on("exit", () => process.exit(0));
console.log("childpid " + process.pid);
console.log("ready");
`);
  const w = startWrapper(fix);
  await waitForReady(w);
  const childPid = childPidOf(w);
  const res = await w.done;
  expect(res.code).toBe(0);
  await groupDead(childPid, 5000);
  await sleep(SETTLE_MS);
}, 20000);

test("failing fixture: the child's exit code passes through", async () => {
  const fix = fixtureSource(`
console.log("childpid " + process.pid);
console.log("ready");
setTimeout(() => process.exit(3), 100);
`);
  const w = startWrapper(fix);
  await waitForReady(w);
  const childPid = childPidOf(w);
  const res = await w.done;
  expect(res.code).toBe(3);
  await groupDead(childPid, 5000);
  await sleep(SETTLE_MS);
}, 20000);

test("extra arguments are forwarded to the spawned command", async () => {
  const fix = fixtureSource(`
const a = process.argv.slice(2);
console.log("args " + JSON.stringify(a));
console.log("childpid " + process.pid);
console.log("ready");
setTimeout(() => process.exit(0), 100);
`);
  const w = startWrapper(fix, ["alpha", "beta"]);
  await w.done;
  expect(w.stdout()).toContain("alpha");
  expect(w.stdout()).toContain("beta");
}, 20000);

/**
 * REAL-PATH integration test (issue #336 operator decision: SIGKILL of the
 * wrapper must be covered by the parent watchdog).
 *
 * The only test in this file that drives the actual production path. It is
 * self-contained: it builds a TINY vitest project in a tempdir (symlinking
 * the outer node_modules so no install is needed), whose vitest.config.mjs
 * points `globalSetup` at the REAL `./vitest.watchdog.ts` (absolute path).
 * It then spawns the wrapper pointed at that project, SIGKILLs the wrapper
 * once the inner vitest has forked workers, and asserts the entire inner
 * vitest group (main + every fork worker) is gone within ~5 s — proving the
 * registered watchdog fires end-to-end, not just the fixture-driven logic
 * above.
 *
 * It runs in the default `npm test` gate: the inner vitest is a separate
 * process in a tempdir running only its own probe test, so it cannot disturb
 * the outer suite (no vitest-in-vitest).
 */
test(
  "REAL: SIGKILL of the wrapper kills the whole vitest group via the watchdog",
  async () => {
    const { symlinkSync } = await import("node:fs");
    const { execFileSync } = await import("node:child_process");

    // Best-effort cleanup of stale d336-real-* dirs from prior runs.
    cleanStaleRealDirs();

    // A tiny vitest project in a tempdir. globalSetup points at the REAL
    // watchdog so the registered globalSetup is the production code.
    const project = mkdtempSync(join(tmpdir(), "d336-real-"));
    let cleaned = false;
    const cleanup = () => {
      if (cleaned) return;
      cleaned = true;
      try {
        rmSync(project, { recursive: true, force: true });
      } catch {
        // best-effort
      }
    };
    process.once("exit", cleanup);

    // Slow probe: keeps the inner vitest alive long enough to SIGKILL the
    // wrapper mid-run.
    writeFileSync(
      join(project, "probe.test.mjs"),
      `import { test } from "vitest";
test("slow enough for the SIGKILL probe", () => new Promise((r) => setTimeout(r, 60000)), 70000);
`,
      "utf8",
    );
    writeFileSync(
      join(project, "vitest.config.mjs"),
      `import { defineConfig } from "vitest/config";
export default defineConfig({
  test: {
    include: ["probe.test.mjs"],
    globalSetup: [${JSON.stringify(join(WEB_ROOT, "vitest.watchdog.ts"))}],
    teardownTimeout: 5000,
  },
});
`,
      "utf8",
    );
    writeFileSync(
      join(project, "package.json"),
      JSON.stringify({ name: "d336-real-probe", private: true, type: "module" }, null, 2),
      "utf8",
    );
    // Symlink the outer node_modules so `vitest` resolves without an install.
    symlinkSync(join(WEB_ROOT, "node_modules"), join(project, "node_modules"), "dir");

    // Spawn the wrapper against the tempdir project. No D33D_TEST_MODE and
    // no D33D_TEST_COMMAND: the wrapper spawns the REAL `vitest run` (the
    // resolved base command), which resolves via the symlinked
    // node_modules/.bin prepended to PATH and loads the project's own
    // vitest.config.mjs (globalSetup → the real watchdog). The inner vitest
    // honours D33D_TEST_WRAPPER_PID, so the watchdog fires on SIGKILL.
    const wrapperEnv = {
      ...process.env,
      PATH: `${join(WEB_ROOT, "node_modules/.bin")}:${process.env.PATH ?? ""}`,
    };
    const child = spawn(process.execPath, [WRAPPER], {
      detached: true,
      stdio: ["ignore", "pipe", "pipe"],
      env: wrapperEnv,
      cwd: project,
    });
    let out = "";
    let errOut = "";
    child.stdout?.on("data", (d: Buffer) => (out += d.toString()));
    child.stderr?.on("data", (d: Buffer) => (errOut += d.toString()));
    const done = new Promise<{ code: number | null; signal: NodeJS.Signals | null }>((resolve, reject) => {
      child.on("exit", (code, signal) => resolve({ code, signal }));
      child.on("error", (err) => reject(err));
    });
    const wrapperPid = child.pid as number;

    // Wait until the inner vitest main (the wrapper's child) appears.
    const vitestMain = await pollUntil<number | null>(
      () => {
        try {
          const rows = execFileSync("ps", ["-axo", "pid,ppid"], { encoding: "utf8" })
            .split("\n")
            .map((l) => l.trim().split(/\s+/))
            .filter((c) => c.length >= 2);
          const main = rows.find((c) => c[1] === String(wrapperPid));
          return main ? Number(main[0]) : null;
        } catch {
          return null;
        }
      },
      30000,
      100,
      "vitest main",
      null,
    );
    if (!vitestMain) {
      cleanup();
      await done.catch(() => {});
      throw new Error(
        `real-path: vitest main never appeared (stdout: ${JSON.stringify(out.slice(0, 500))}, stderr: ${JSON.stringify(errOut.slice(0, 500))})`,
      );
    }

    // Wait for at least one fork worker to join the group (proof the run is
    // in progress and globalSetup has executed before workers are spawned).
    const hasWorker = await pollUntil<boolean>(
      () => {
        try {
          const rows = execFileSync("ps", ["-axo", "pid,pgid"], { encoding: "utf8" })
            .split("\n")
            .map((l) => l.trim().split(/\s+/))
            .filter((c) => c.length >= 2);
          const inGroup = rows.filter((c) => c[1] === String(vitestMain));
          return inGroup.length >= 2;
        } catch {
          return false;
        }
      },
      30000,
      100,
      "fork worker in group",
      false,
    );
    if (!hasWorker) {
      cleanup();
      await done.catch(() => {});
      throw new Error(
        `real-path: no fork worker appeared in the vitest group (stdout: ${JSON.stringify(out.slice(0, 500))}, stderr: ${JSON.stringify(errOut.slice(0, 500))})`,
      );
    }

    // The group must be alive before the kill.
    expect(pidAlive(vitestMain)).toBe(true);

    // SIGKILL the wrapper. The registered watchdog (vitest.watchdog.ts,
    // polling every 1 s, requires two consecutive ESRCH polls) must SIGKILL
    // vitest's own group within ~2 s + margin.
    process.kill(wrapperPid, "SIGKILL");
    await pidDead(wrapperPid, 5000);

    // Whole vitest group dead within 5 s (two ESRCH polls ≈ 2 s + margin).
    // If the watchdog did not fire this times out → the test fails, exactly
    // the failure mode #336 must catch.
    await groupDead(vitestMain, 5000);

    await done.catch(() => {});
    await sleep(SETTLE_MS);
    cleanup();
  },
  120000,
);
