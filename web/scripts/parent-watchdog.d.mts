/** Is the parent process at `pid` still alive? */
export declare function parentAlive(probe: (pid: number, signal: number | string) => void, pid: number): boolean;

/** Decide what to do after one poll of the parent. */
export declare function checkParent(probe: (pid: number, signal: number | string) => void, pid: number, killGroup: (signal: string) => void): "alive" | "dead";

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
