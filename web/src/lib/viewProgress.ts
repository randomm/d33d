/**
 * View progress model — drives the honest "view N of 6" counter
 * (issue #118).
 *
 * The state is reduced from the per-view SSE frames the backend emits
 * (``render-view-start`` / ``render-view-done``, each carrying ``view`` —
 * the view stem — and ``iteration`` — the 1-based design-loop index).
 * No timer, no interpolation: every transition is caused by a real frame.
 *
 * The three things that would make the counter lie:
 *
 * - **Iterations.** The loop runs up to three renders, each rendering six
 *   views. The counter resets to a fresh pass when the new iteration's
 *   first frame arrives (the start frame for the new pass's first view —
 *   the backend emits start before done, so the start frame is what
 *   signals the new pass has begun) — it never reads "view 9 of 6", and
 *   the attempt number (shown from the second pass on) is what makes the
 *   reset legible.
 * - **Silence.** Nothing but a frame advances the counter. A hung render
 *   freezes the counter at its last real value; a dead stream is detected
 *   by the terminal error frame, which unmounts the whole surface.
 * - **STL first.** The entrypoint renders ``stl`` (and ``csg``) before
 *   any view, and the SSE frames only carry view stems — so the counter
 *   stays at 0 of 6 through that window and never claims view progress
 *   that never started.
 */

import { passCard } from "../copy";

/** The closed set of view stems, in render order (the VIEWS contract,
 *  0-based position). The stem is the view filename minus ``.png``
 *  (see ``render_for_design_loop``'s ``_view_index_by_stem``). */
export const VIEW_STEMS = [
  "view_00_front",
  "view_01_back",
  "view_02_left",
  "view_03_right",
  "view_04_top",
  "view_05_iso",
] as const;

const STEM_TO_ORDER: ReadonlyMap<string, number> = new Map(
  VIEW_STEMS.map((stem, i) => [stem, i]),
);

/** Human label for a view stem (the closed six from the copy deck). */
export function viewLabelForStem(stem: string): string {
  const idx = STEM_TO_ORDER.get(stem);
  return idx !== undefined ? passCard.viewLabels[idx] : "the render";
}

export interface ViewProgressState {
  /** How many views completed (``render-view-done`` frames) in the
   *  current iteration. Never advanced by anything but a frame. */
  done: number;
  /** Total views the counter counts toward. 0 before the first frame
   *  (the render layout is not yet established — no digits at all); 6
   *  from the first view frame on. */
  total: number;
  /** The stem whose ``render-view-start`` frame was the last event
   *  (the "rendering…" slot). Null when nothing is mid-flight. */
  renderingStem: string | null;
  /** The 1-based design-loop iteration the current pass belongs to (0
   *  = no frame yet — no attempt is shown). */
  iteration: number;
  /** The stems already counted as done, in order. */
  doneStems: string[];
}

/** The idle state before any per-view frame arrives. */
export const INITIAL_VIEW_PROGRESS: ViewProgressState = {
  done: 0,
  total: 0,
  renderingStem: null,
  iteration: 0,
  doneStems: [],
};

/** The number of auto-attempts the design loop caps at (the same 3 the
 *  ``repairAttempt`` copy references). */
export const MAX_ITERATIONS = 3;

/** Reduce one per-view SSE frame into the progress state.
 *
 * ``step`` is ``"render-view-start"`` or ``"render-view-done"`` (the two
 * closed values the backend emits); any other value returns the state
 * unchanged — this reducer only reacts to the per-view frames, never to
 * ``design-loop-start`` / ``version-created`` and friends.
 *
 * ``iteration`` is the 1-based design-loop index the frame carries. A
 * frame from a NEW iteration (strictly greater than the state's) resets
 * the count to a fresh pass: the counter restarts at 0 of 6 with the
 * attempt number shown, instead of continuing to a nonsensical
 * "view 9 of 6".
 */
export function reduceViewProgress(
  state: ViewProgressState,
  step: string,
  view: string,
  iteration: number,
): ViewProgressState {
  if (step !== "render-view-start" && step !== "render-view-done") {
    return state;
  }
  // A 1-based index arrives with the frame; guard the degenerate 0/absent
  // case by keeping whatever iteration the state already knew.
  const iter = iteration >= 1 ? iteration : state.iteration;
  const order = STEM_TO_ORDER.get(view);
  // The total is established by the first frame carrying a known stem —
  // before that, no digits at all (the layout is not yet real).
  const total = order !== undefined ? VIEW_STEMS.length : state.total;
  // New design-loop iteration → fresh pass. The reset is triggered by the
  // first frame of the new iteration (in practice the ``render-view-start``
  // for the new pass's first view — the backend emits start before done,
  // so the start frame is what signals the new pass has begun). The
  // attempt number is shown from that frame on, so the reset is
  // announced, never silent. The counter never reads "view 9 of 6".
  if (state.iteration !== 0 && iter > state.iteration) {
    // The frame that triggered the reset is itself the new pass's first
    // frame: if it's a start frame, that view is the in-flight slot from
    // the moment of the reset (the reset and the "rendering…" state
    // happen together — there is no blank gap between one pass ending and
    // the next beginning, and the attempt line is what makes the
    // transition legible).
    return {
      ...INITIAL_VIEW_PROGRESS,
      iteration: iter,
      total,
      renderingStem: step === "render-view-start" ? view : null,
    };
  }
  if (step === "render-view-start") {
    return {
      ...state,
      total,
      renderingStem: view,
      iteration: iter,
    };
  }
  // render-view-done. Unknown stems are real events but uncountable —
  // the counter holds (honest: it only counts what it can name).
  if (order === undefined) {
    return { ...state, renderingStem: null, iteration: iter };
  }
  // Idempotency: a repeated done frame for the same stem does not
  // double-count.
  if (state.doneStems.includes(view)) {
    return { ...state, renderingStem: null };
  }
  return {
    ...state,
    done: state.done + 1,
    total,
    renderingStem: null,
    iteration: iter,
    doneStems: [...state.doneStems, view],
  };
}

/** The most recently completed stem (for the counter line's view name). */
export function lastDoneStem(state: ViewProgressState): string | null {
  return state.doneStems.length > 0 ? state.doneStems[state.doneStems.length - 1] : null;
}
