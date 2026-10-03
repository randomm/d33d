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
