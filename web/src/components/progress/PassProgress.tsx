/**
 * PassProgress — the design-loop stage indicator (issue #82, reworked in
 * issue #118 from an indeterminate animation to an honest, event-driven
 * counter).
 *
 * Presentational: the parent (App) owns the in-flight flag, the current
 * step, the elapsed-seconds counter and the per-view progress state.
 * The per-view state is reduced from the real ``render-view-start`` /
 * ``render-view-done`` SSE frames (see ``lib/viewProgress.ts``) — no
 * timer and no interpolation advance it.
 *
 * Display rules, all of them about not lying:
 * - Before any per-view frame the counter shows no digits at all
 *   (the render layout is not yet established).
 * - "N of 6" is driven by the done frames; the view in the slot is named
 *   from the closed six-label set, never the raw stem (the stem is a
 *   filename, not a word).
 * - A new ``iteration`` resets the count and the attempt number is shown
 *   from the second pass on — the counter never reads "view 9 of 6" and
 *   never restarts unannounced.
 * - The indeterminate sliding bar is gone: a moving gradient implied
 *   motion the system cannot substantiate. The remaining bar is a
 *   deterministic fill driven by the frame count (width is a layout
 *   property, not an animation — no CSS transition, no keyframes).
 * - If events stop (a hung render), the counter simply freezes at its
 *   last real value — a frozen counter is honest; a ticking one is the
 *   lie this item deletes. Stream death unmounts the whole surface via
 *   the terminal error frame.
 */

import { copy } from "../../copy";
import {
  INITIAL_VIEW_PROGRESS,
  lastDoneStem,
  MAX_ITERATIONS,
  viewLabelForStem,
  type ViewProgressState,
} from "../../lib/viewProgress";

interface PassProgressProps {
  /** The raw step token from the design-loop progress frame (may be null
   *  or an unknown value — both render the generic label). */
  step: string | null;
  /** Elapsed seconds since the pass started (the caller owns the 1s
   *  timer — the elapsed display, not the counter, is time-derived). */
  elapsed: number;
  /** The per-view progress state reduced from the SSE stream (the
   *  caller owns the reducer; defaults to idle). */
  viewProgress?: ViewProgressState;
}

export function PassProgress({
  step,
  elapsed,
  viewProgress = INITIAL_VIEW_PROGRESS,
}: PassProgressProps) {
  const stageLabel =
    step === "design-loop-start"
      ? "Generating design…"
      : step === "design-loop-pass"
        ? "Rendering and checking…"
        : step === "version-created"
          ? "Saving version…"
          : "Working on your design…";

  // The per-view counter line. No digits before a frame establishes the
  // render layout (total === 0) — a confident "0 of 6" before the first
  // real event would claim a total the system has not yet shown.
  let counterLine: string | null = null;
  if (viewProgress.total > 0) {
    const done = viewProgress.done;
    const total = viewProgress.total;
    const doneLine = copy.progress.viewsProgress(done, total);
    const rendering = viewProgress.renderingStem;
    const last = lastDoneStem(viewProgress);
    if (rendering !== null) {
      // A view is in the slot: the count, then the in-flight view —
      // "2 of 6 … Left".
      counterLine = `${doneLine} ${viewLabelForStem(rendering)}`;
    } else if (last !== null) {
      // Nothing in flight, at least one landed: the count, then the view
      // that just finished.
      counterLine = `${doneLine} ${viewLabelForStem(last)}`;
    } else {
      // Neither landed nor in flight with a total established (e.g. the
      // first done of a reset pass) — just the count.
      counterLine = doneLine;
    }
  }

  // The deterministic fill: frames only. 0 before any done frame (the
  // layout is not established yet → width 0, no confident percentage).
  const fillPct =
    viewProgress.total > 0 && viewProgress.done > 0
      ? (viewProgress.done / viewProgress.total) * 100
      : 0;

  // The attempt line: announced, not hidden. First pass is the baseline
  // (no attempt number to show); from the second pass on the number is
  // what makes the counter reset legible.
  const attemptLine =
    viewProgress.iteration > 1
      ? copy.progress.attemptLine(viewProgress.iteration, MAX_ITERATIONS)
      : null;

  return (
    <div className="design-loop-progress" data-testid="design-loop-progress" role="status">
      <span data-testid="design-loop-stage">
        {stageLabel}
        {attemptLine ? ` — ${attemptLine}` : ""}
      </span>
      {counterLine !== null && (
        <span data-testid="view-progress-counter">{counterLine}</span>
      )}
      <div
        className="design-loop-progress-bar"
        data-testid="design-loop-progress-bar"
        aria-hidden="true"
      >
        <span
          className="design-loop-progress-fill"
          data-testid="design-loop-progress-fill"
          style={{ width: `${fillPct}%` }}
        />
      </div>
      <span data-testid="design-loop-elapsed">{elapsed}s</span>
    </div>
  );
}

export type { ViewProgressState };
