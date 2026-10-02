// Issue #336: prove the run-vitest wrapper reaps its whole child process
// group on SIGTERM/SIGINT/SIGHUP, on normal exit, and (via the parent
// watchdog) on SIGKILL of the wrapper itself.
//
// Every case spawns the wrapper against a FIXTURE command (never the outer
// suite's own vitest). The fixture is a tiny node script in a tempdir that
// spawns a grandchild and sleeps, so the group has 2+ members:
//
//   wrapper (own group, never the test runner's)
//     └── child  (detached → leads its own group, pgid == child.pid)
//           └── grandchild (inherits the child's group)
//
// D33D_TEST_COMMAND overrides what the wrapper spawns.
// The SIGKILL case drives the watchdog module directly against the real
// fixture group instead of starting a real vitest.

import { expect, test } from "vitest";
import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { tmpdir } from "node:os";

// The watchdog module is plain .mjs (outside tsconfig's src/ rootDir); import
// it dynamically to avoid the missing-declaration error.
async function loadWatchdog() {
  const mod: unknown = await import("../../scripts/parent-watchdog.mjs");
  return mod as { checkParent: (probe: (p: number, s: number | string) => void, pid: number, kill: (s: string) => void) => "alive" | "dead" };
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

/** Poll until the process group at `pgid` is gone (kill throws ESRCH). */
async function groupDead(pgid: number, timeoutMs: number): Promise<void> {
  const start = Date.now();
  for (;;) {
    try {
      process.kill(-pgid, 0);
    } catch (err) {
      if ((err as NodeJS.ErrnoException).code === "ESRCH") {
        return;
      }
      throw err;
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

function fixtureSource(code: string): string {
  const path = `${tmpdir()}/d336-fix-${process.pid}-${Date.now()}-${Math.random().toString(36).slice(2)}.mjs`;
  writeFileSync(path, code, "utf8");
  return path;
}

// web/scripts/ lives outside web/src (tsconfig rootDir is src); the wrapper
// is resolved relative to the package root (process.cwd() is web/ under
// vitest).
const WRAPPER = `${process.cwd()}/scripts/run-vitest.mjs`;

function startWrapper(fixturePath: string, extraArgs: string[] = []): Wrapper {
  const child = spawn(process.execPath, [WRAPPER, ...extraArgs], {
    detached: true, // the wrapper runs in its own group, never the runner's
    stdio: ["ignore", "pipe", "pipe"],
    env: {
      ...process.env,
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

function watchdogFixture(): string {
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

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

test("SIGTERM reaps the wrapper's whole process group, exit 143", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGTERM");
  const res = await w.done;
  expect(res.code).toBe(143);
  await groupDead(childPid, 8000);
  await sleep(300);
}, 20000);

test("SIGINT reaps the wrapper's whole process group, exit 130", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGINT");
  const res = await w.done;
  expect(res.code).toBe(130);
  await groupDead(childPid, 8000);
  await sleep(300);
}, 20000);

test("SIGHUP reaps the wrapper's whole process group, exit 129", async () => {
  const w = startWrapper(sleepFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGHUP");
  const res = await w.done;
  expect(res.code).toBe(129);
  await groupDead(childPid, 8000);
  await sleep(300);
}, 20000);

test("SIGKILL to the wrapper: the watchdog reaps the group within the bound", async () => {
  const w = startWrapper(watchdogFixture());
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
  await sleep(300);
}, 20000);

test("fixture traps SIGTERM: the wrapper escalates to SIGKILL after the grace", async () => {
  const w = startWrapper(trapFixture());
  await waitForReady(w);
  const childPid = childPidOf(w);
  w.child.kill("SIGTERM");
  const res = await w.done;
  expect(res.code).toBe(143);
  await groupDead(childPid, 8000);
  await sleep(300);
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
  await sleep(300);
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
  await sleep(300);
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
