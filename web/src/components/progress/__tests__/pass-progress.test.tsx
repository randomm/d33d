/**
 * PassProgress tests — the design-loop stage indicator (issue #82) and
 * the honest per-view counter (issue #118).
 *
 * The counter tests drive the ACTUAL event shape the backend emits —
 * `render-view-start` / `render-view-done` with `view` (the stem) and
 * `iteration` (the 1-based loop index) — through the same reducer the
 * App's onProgress handler uses. No hand-invented frame shape: the
 * reducer's input is exactly what the SSE stream carries.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PassProgress } from "../PassProgress";
import copy from "../../../copy";
import {
  INITIAL_VIEW_PROGRESS,
  reduceViewProgress,
  type ViewProgressState,
} from "../../../lib/viewProgress";

/** Drive a sequence of REAL per-view frames (the exact shape the backend
 *  emits: step + view + iteration) through the same reducer App uses. */
function progressFromFrames(
  frames: Array<[step: string, view: string, iteration: number]>,
): ViewProgressState {
  return frames.reduce<ViewProgressState>(
    (state, [step, view, iteration]) =>
      reduceViewProgress(state, step, view, iteration),
    INITIAL_VIEW_PROGRESS,
  );
}

const FULL_RUN_1: Array<[string, string, number]> = [
  ["render-view-start", "view_00_front", 1],
  ["render-view-done", "view_00_front", 1],
  ["render-view-start", "view_01_back", 1],
  ["render-view-done", "view_01_back", 1],
  ["render-view-start", "view_02_left", 1],
];

describe("PassProgress — stage label", () => {
  it("renders 'Generating design…' on design-loop-start", () => {
    render(<PassProgress step="design-loop-start" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Generating design…");
  });

  it("renders 'Rendering and checking…' on design-loop-pass", () => {
    render(<PassProgress step="design-loop-pass" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Rendering and checking…");
  });

  it("renders 'Saving version…' on version-created", () => {
    render(<PassProgress step="version-created" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Saving version…");
  });

  it("renders the generic fallback for an unknown step", () => {
    render(<PassProgress step="some-unknown-step" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Working on your design…");
  });

  it("renders the generic fallback for null step", () => {
    render(<PassProgress step={null} elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Working on your design…");
  });

  it("never shows the raw step token", () => {
    render(<PassProgress step="some-unknown-token" elapsed={5} />);
    expect(screen.getByTestId("design-loop-progress").textContent).not.toContain("some-unknown-token");
  });

  it("shows the elapsed seconds", () => {
    render(<PassProgress step={null} elapsed={12} />);
    expect(screen.getByTestId("design-loop-elapsed").textContent).toBe("12s");
  });

  it("renders the progress bar", () => {
    render(<PassProgress step={null} elapsed={0} />);
    expect(screen.getByTestId("design-loop-progress-bar")).toBeTruthy();
  });
});

describe("PassProgress — the honest counter (issue #118)", () => {
  it("shows no counter before any per-view frame (no confident '0 of 6')", () => {
    render(<PassProgress step="design-loop-start" elapsed={3} />);
    expect(screen.queryByTestId("view-progress-counter")).toBeNull();
    // The bar holds at 0 — nothing is established yet.
    const fill = screen.getByTestId("design-loop-progress-fill");
    expect(fill.style.width).toBe("0%");
  });

  it("counts completed views from render-view-done frames", () => {
    const vp = progressFromFrames(FULL_RUN_1.slice(0, 5)); // front+back done, left rendering
    render(<PassProgress step="design-loop-pass" elapsed={10} viewProgress={vp} />);
    // While left is rendering, the counter names the view in the slot.
    expect(screen.getByTestId("view-progress-counter").textContent).toBe(
      `${copy.progress.viewsProgress(2, 6)} Left`,
    );
    // The in-flight view is named; the raw stem is not shown.
    expect(screen.getByTestId("view-progress-counter").textContent).not.toContain("view_02_left");
  });

  it("names the view that just finished after it lands", () => {
    const vp = progressFromFrames(FULL_RUN_1.slice(0, 4));
    // Replace the in-flight left with its done frame:
    const withLeft = reduceViewProgress(vp, "render-view-done", "view_02_left", 1);
    render(<PassProgress step="design-loop-pass" elapsed={11} viewProgress={withLeft} />);
    const text = screen.getByTestId("view-progress-counter").textContent;
    expect(text).toContain("3 of 6");
    expect(text).toContain("Left");
    // Never the raw stem — the stem is a filename, not a word.
    expect(screen.getByTestId("design-loop-progress").textContent).not.toContain("view_02_left");
  });

  it("fills the bar deterministically from the done count", () => {
    const vp = progressFromFrames(FULL_RUN_1.slice(0, 4)); // 2 done
    render(<PassProgress step="design-loop-pass" elapsed={10} viewProgress={vp} />);
    expect(screen.getByTestId("design-loop-progress-fill").style.width).toBe(`${(2 / 6) * 100}%`);
  });

  it("shows the full count of 6 of 6 when all views land", () => {
    const all = progressFromFrames([
      ["render-view-start", "view_00_front", 1],
      ["render-view-done", "view_00_front", 1],
      ["render-view-start", "view_01_back", 1],
      ["render-view-done", "view_01_back", 1],
      ["render-view-start", "view_02_left", 1],
      ["render-view-done", "view_02_left", 1],
      ["render-view-start", "view_03_right", 1],
      ["render-view-done", "view_03_right", 1],
      ["render-view-start", "view_04_top", 1],
      ["render-view-done", "view_04_top", 1],
      ["render-view-start", "view_05_iso", 1],
      ["render-view-done", "view_05_iso", 1],
    ]);
    render(<PassProgress step="design-loop-pass" elapsed={20} viewProgress={all} />);
    const text = screen.getByTestId("view-progress-counter").textContent;
    expect(text).toContain("6 of 6");
    expect(text).toContain("Iso");
    expect(screen.getByTestId("design-loop-progress-fill").style.width).toBe("100%");
  });

  it("resets on a new iteration and announces the attempt (never 'view 9 of 6')", () => {
    // Iteration 1: three views done.
    let vp = progressFromFrames(FULL_RUN_1.slice(0, 6));
    // Iteration 2 begins: the count resets to 0 of 6 (fresh pass) — the
    // attempt is announced as soon as the new pass's first frame arrives,
    // never a silent "view 9 of 6".
    vp = reduceViewProgress(vp, "render-view-start", "view_00_front", 2);
    expect(vp.iteration).toBe(2);
    expect(vp.done).toBe(0);
    expect(vp.total).toBe(6);
    expect(vp.renderingStem).toBe("view_00_front");
  });

  it("announces the attempt in the stage line from the second pass on", () => {
    // The first pass: no attempt number (the baseline).
    let vp = reduceViewProgress(
      INITIAL_VIEW_PROGRESS,
      "render-view-done",
      "view_00_front",
      1,
    );
    const first = render(
      <PassProgress step="design-loop-pass" elapsed={10} viewProgress={vp} />,
    );
    expect(first.getByTestId("design-loop-stage").textContent).not.toContain("Attempt");
    first.unmount();
    // The second pass: the attempt number is what makes the reset legible.
    vp = reduceViewProgress(vp, "render-view-start", "view_00_front", 2);
    const second = render(
      <PassProgress step="design-loop-pass" elapsed={25} viewProgress={vp} />,
    );
    expect(second.getByTestId("design-loop-stage").textContent).toContain("Attempt 2 of up to 3");
    expect(second.getByTestId("view-progress-counter").textContent).toContain("0 of 6");
  });

  it("does not double-count a repeated done frame for the same stem", () => {
    let vp = reduceViewProgress(INITIAL_VIEW_PROGRESS, "render-view-start", "view_00_front", 1);
    vp = reduceViewProgress(vp, "render-view-done", "view_00_front", 1);
    vp = reduceViewProgress(vp, "render-view-done", "view_00_front", 1);
    render(<PassProgress step="design-loop-pass" elapsed={10} viewProgress={vp} />);
    expect(screen.getByTestId("view-progress-counter").textContent).toContain("1 of 6");
  });

  it("freezes at the last real value when no further frame arrives", () => {
    // A hung render at view 3: the state is whatever the frames set, and
    // re-rendering with the same state cannot advance it (no timer).
    const vp = reduceViewProgress(
      progressFromFrames(FULL_RUN_1.slice(0, 6)),
      "render-view-done",
      "view_02_left",
      1,
    ); // 3 done, nothing in flight
    const { rerender } = render(
      <PassProgress step="design-loop-pass" elapsed={15} viewProgress={vp} />,
    );
    expect(screen.getByTestId("view-progress-counter").textContent).toContain("3 of 6");
    rerender(
      <PassProgress step="design-loop-pass" elapsed={99} viewProgress={vp} />,
    );
    expect(screen.getByTestId("view-progress-counter").textContent).toContain("3 of 6");
  });

  it("reduces no non-per-view step (the counter only moves on its own frames)", () => {
    const vp = progressFromFrames(FULL_RUN_1.slice(0, 4)); // 2 done
    const next = reduceViewProgress(vp, "design-loop-pass", "view_01_back", 1);
    expect(next.done).toBe(2);
    const idle = reduceViewProgress(INITIAL_VIEW_PROGRESS, "version-created", "view_00_front", 1);
    expect(idle.done).toBe(0);
    expect(idle.total).toBe(0);
  });
});
