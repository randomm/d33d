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
// D33D_TEST_COMMAND overrides what the wrapper spawns in the fixture cases.
//
// The final test ("REAL: SIGKILL…") IS the real path: it is self-contained
// (a tiny vitest project in a tempdir whose globalSetup points at the real
// vitest.watchdog.ts) and drives the actual production wrapper → vitest →
// tinypool path. See that test's doc comment.

import { expect, test } from "vitest";
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

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

/**
 * REAL-PATH integration test (issue #336 operator decision: SIGKILL of the
 * wrapper must be covered by the parent watchdog).
 *
 * The only test in this file that drives the actual production path. It is
 * self-contained: it builds a TINY vitest project in a tempdir (symlinking
 * the outer node_modules so no install is needed), whose vitest.config.ts
 * points `globalSetup` at the REAL `./vitest.watchdog.ts` (absolute path).
 * It then spawns the wrapper pointed at that project, SIGKILLs the wrapper
 * once the inner vitest has forked workers, and asserts the entire inner
 * vitest group (main + every fork worker) is gone within ~3 s — proving the
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

    // Spawn the wrapper against the tempdir project. No D33D_TEST_COMMAND and
    // no forwarded args: the wrapper spawns the REAL `vitest run` (the
    // hardcoded base command), which resolves via the symlinked
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
    const vitestMain = await new Promise<number | null>((resolve) => {
      const start = Date.now();
      const tick = () => {
        try {
          const rows = execFileSync("ps", ["-axo", "pid,ppid"], { encoding: "utf8" })
            .split("\n")
            .map((l) => l.trim().split(/\s+/))
            .filter((c) => c.length >= 2);
          const main = rows.find((c) => c[1] === String(wrapperPid));
          if (main) {
            resolve(Number(main[0]));
            return;
          }
        } catch {
          // ignore, retry
        }
        if (Date.now() - start > 30000) {
          resolve(null);
          return;
        }
        setTimeout(tick, 100);
      };
      tick();
    });
    if (!vitestMain) {
      cleanup();
      await done.catch(() => {});
      throw new Error(
        `real-path: vitest main never appeared (stdout: ${JSON.stringify(out.slice(0, 500))}, stderr: ${JSON.stringify(errOut.slice(0, 500))})`,
      );
    }

    // Wait for at least one fork worker to join the group (proof the run is
    // in progress and globalSetup has executed before workers are spawned).
    const hasWorker = await new Promise<boolean>((resolve) => {
      const start = Date.now();
      const tick = () => {
        try {
          const rows = execFileSync("ps", ["-axo", "pid,pgid"], { encoding: "utf8" })
            .split("\n")
            .map((l) => l.trim().split(/\s+/))
            .filter((c) => c.length >= 2);
          const inGroup = rows.filter((c) => c[1] === String(vitestMain));
          if (inGroup.length >= 2) {
            resolve(true);
            return;
          }
        } catch {
          // ignore
        }
        if (Date.now() - start > 30000) {
          resolve(false);
          return;
        }
        setTimeout(tick, 100);
      };
      tick();
    });
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
    // polling every 1 s) must SIGKILL vitest's own group within ~1 s + margin.
    process.kill(wrapperPid, "SIGKILL");
    await pidDead(wrapperPid, 5000);

    // Whole vitest group dead within 3 s. If the watchdog did not fire this
    // times out → the test fails, exactly the failure mode #336 must catch.
    await groupDead(vitestMain, 3000);

    await done.catch(() => {});
    await sleep(300);
    cleanup();
  },
  120000,
);
