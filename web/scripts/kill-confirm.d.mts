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
