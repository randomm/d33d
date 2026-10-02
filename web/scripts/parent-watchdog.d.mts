/** Is the parent process at `pid` still alive? */
export declare function parentAlive(probe: (pid: number, signal: number | string) => void, pid: number): boolean;

/**
 * Decide what to do after one poll of the parent.
 * Retained for unit-testability: the single-poll verdict is what tests
 * assert without driving the full timer loop.
 */
export declare function checkParent(probe: (pid: number, signal: number | string) => void, pid: number, killGroup: (signal: string) => void): "alive" | "dead";

/**
 * Parse and validate the D33D_TEST_WRAPPER_PID env value for the watchdog
 * setup. Returns a usable pid, or null when the watchdog must not run.
 * Warns (via the injected `warn`) when the value is set but invalid.
 */
export declare function parseWrapperPid(
  raw: string | undefined,
  warn?: (line: string) => void,
): number | null;

/**
 * Build the polling loop used by the vitest globalSetup.
 * Returns a teardown that clears the interval.
 */
export declare function startWatchdog(
  probe: (pid: number, signal: number | string) => void,
  pid: number,
  killGroup: (signal: string) => void,
  opts?: { pollMs?: number },
): () => void;
