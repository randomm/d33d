/**
 * Kill the child pid, then its process group (`-pid`), with one signal.
 * ESRCH is swallowed silently, every other errno is logged and swallowed.
 * Never throws — the caller's escalation / watchdog remains the backstop.
 */
export declare function killChildAndGroup(
  deps: { kill: (pid: number, signal: string) => void; log: (line: string) => void },
  pid: number,
  signal: string,
): void;

/**
 * Kill the child + group with SIGTERM, then confirm the group is gone.
 * Pure: kill/probe/ps/now/sleep/log all injected.
 * Returns the confirmation outcome (true = confirmed gone).
 */
export declare function killGroupGone(
  deps: {
    kill: (pid: number, signal: string) => void;
    probe: () => boolean;
    ps: () => boolean | null;
    now: () => number;
    sleep: (ms: number) => void;
    log: (line: string) => void;
  },
  pid: number,
  opts?: { graceMs?: number; tickMs?: number },
): boolean;

/**
 * Probe whether `ps` reports no process whose pgid is `pgid` (the EPERM
 * fallback probe of the run-vitest wrapper). Returns null when `ps` is
 * unavailable (including a ps timeout — `timeout: 1000` throws into the
 * bare catch). The `exec` option is injectable for the unit test of the
 * timeout path (no real `ps` process is spawned).
 */
export declare function psGroupEmpty(
  pgid: number,
  deps?: {
    exec?: (
      cmd: string,
      args: string[],
      o: { encoding: string; timeout?: number },
    ) => string;
  },
): boolean | null;

/**
 * Confirm the child group is gone before the wrapper exits.
 *
 * Pure: all I/O is injected. Returns true when the group was confirmed
 * gone, false when the confirmation could not be established (logged;
 * the parent watchdog remains the backstop).
 *
 * @param deps - injected probe/ps/now/sleep/log dependencies.
 * @param opts - optional grace/tick intervals.
 * @returns true when the group was confirmed gone, false otherwise.
 */
export declare function confirmGroupGone(
  deps: {
    probe: () => boolean;
    ps: () => boolean | null;
    now: () => number;
    sleep: (ms: number) => void;
    log: (line: string) => void;
  },
  opts?: { graceMs?: number; tickMs?: number },
): boolean;
