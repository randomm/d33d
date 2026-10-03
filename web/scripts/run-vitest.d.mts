/**
 * Typed contract for `run-vitest.mjs` (a plain .mjs wrapper with no source
 * file to import). Mirrors the module's `psGroupEmpty` export and MUST be
 * updated with it — it is the boundary the wrapper test types against.
 *
 * The module's process effects (the child `spawn`, the signal/exit
 * handlers) are entry-point-guarded inside `runMain`; importing the module
 * yields the pure exports only, with no side effects.
 */

/**
 * Probe whether `ps` reports no process whose pgid is `pgid`.
 * Returns null when `ps` is unavailable (including a ps timeout).
 * The `exec` option is injectable for unit tests of the timeout path.
 */
export declare function psGroupEmpty(
  pgid: number,
  opts?: {
    exec?: (
      cmd: string,
      args: string[],
      o: { encoding: string; timeout?: number },
    ) => string;
  },
): boolean | null;
