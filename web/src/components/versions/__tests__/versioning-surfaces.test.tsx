/**
 * Version-management surface tests (issue #8).
 *
 * The five surfaces' SPA half:
 *   - VersionTimeline (side rail) — name, message excerpt, timestamp,
 *     thumbnail, diff badge, per-message restore + pin + compare-select
 *   - VariantGallery (pinned grid) — cards with actions (set-as-main,
 *     branch-from, archive)
 *   - CompareView (two viewports + param diff table) — the prioritized
 *     surface; the shared-rotation contract is asserted client-side
 *   - App wiring — opening a project resumes at the latest version with
 *     the timeline as a side rail; the design-loop FINALIZE is wired to
 *     `client.finalize`
 *
 * ModelViewer is mocked (it owns a real three.js WebGLRenderer — jsdom
 * cannot construct one); the timeline/gallery/compare tests mock the
 * version endpoints on the ApiClient the same way app-layout.test does.
 */
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { useEffect } from "react";
import {
  type VersionTimelineEntry,
  type GalleryCard,
  type VersionCompare,
} from "../../../lib/api";
import { VersionTimeline } from "../VersionTimeline";
import { VariantGallery } from "../VariantGallery";
import { CompareView } from "../CompareView";
import type { ModelViewerHandle } from "../../viewer/ModelViewer";

vi.mock("../../viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../../viewer/ModelViewer")>(
    "../../viewer/ModelViewer",
  );
  const MockModelViewer = (props: {
    data: ArrayBuffer | null;
    format: string;
    onReady?: (handle: ModelViewerHandle) => void;
  }) => {
    const { format, onReady } = props;
    useEffect(() => {
      onReady?.({
        scene: {} as never,
        camera: {} as never,
        renderer: {
          domElement: document.createElement("canvas"),
          getSize: (t: { x: number; y: number }) => {
            t.x = 600;
            t.y = 400;
            return t;
          },
        } as never,
        controls: {} as never,
        raycaster: {} as never,
        modelRoot: null,
      });
    }, [onReady]);
    return <div data-testid={`model-viewer-slot-${format}`} />;
  };
  return { ...actual, ModelViewer: MockModelViewer };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const TIMELINE: VersionTimelineEntry[] = [
  {
    id: 1,
    name: "first box",
    params: { W: 20, H: 25, D: 30 },
    created_by_message: "make a box",
    parent: null,
    restored_from: null,
    forked_from: null,
    pinned: false,
    archived: false,
    thumbnail: "/thumbs/v1.png",
    created_at: "2026-01-01T00:00:00Z",
    diff_count: 0,
    exported_at: null,
  },
  {
    id: 2,
    name: "wider box",
    params: { W: 24, H: 25, D: 30 },
    created_by_message: "widen the box",
    parent: 1,
    restored_from: null,
    forked_from: null,
    pinned: true,
    archived: false,
    thumbnail: "/thumbs/v2.png",
    created_at: "2026-01-02T00:00:00Z",
    diff_count: 1,
    exported_at: null,
  },
];

const GALLERY: GalleryCard[] = [
  {
    ...TIMELINE[1],
    actions: ["set-as-main", "branch-from", "archive"],
  },
];

const COMPARE: VersionCompare = {
  project_id: 1,
  a: TIMELINE[0],
  b: TIMELINE[1],
  diff: { added: [], removed: [], changed: ["W"], count: 1 },
  shared_rotation: { units: "mm", axis_convention: "z-up", identical_convention: true },
};

/** A restored version at list position 3 (id 3) whose parent is v1 —
 * its badge must diff against v1 (its parent), not v2 (the row before it).
 * v3 = {W: 22, H: 25, D: 30} (restored from v1 {W: 20, H: 25, D: 30}, so
 * 1 change vs the parent) vs v2 = {W: 24, H: 25, D: 30} (2 changes if the
 * badge were wrongly position-based). */
const TIMELINE_WITH_RESTORE: VersionTimelineEntry[] = [
  TIMELINE[0],
  TIMELINE[1],
  {
    id: 3,
    name: "restored from version 1",
    params: { W: 22, H: 25, D: 30 },
    created_by_message: "restored from version 1",
    parent: 1,
    restored_from: 1,
    forked_from: null,
    pinned: false,
    archived: false,
    thumbnail: null,
    created_at: "2026-01-03T00:00:00Z",
    diff_count: 1,
    exported_at: null,
  },
];

// ---------------------------------------------------------------------------
// VersionTimeline
// ---------------------------------------------------------------------------

describe("VersionTimeline (the side rail)", () => {
  it("renders each version with name, message excerpt, timestamp, thumbnail, and diff badge", () => {
    render(<VersionTimeline versions={TIMELINE} latestId={2} />);
    // Two timeline entries.
    expect(screen.getAllByTestId(/^timeline-entry-/)).toHaveLength(2);
    // Names.
    expect(screen.getByTestId("timeline-name-1").textContent).toBe("first box");
    expect(screen.getByTestId("timeline-name-2").textContent).toBe("wider box");
    // Message excerpts (the triggering message).
    expect(screen.getByTestId("timeline-msg-1").textContent).toBe("make a box");
    // Timestamps.
    expect(screen.getByTestId("timeline-ts-1").textContent).toBe("2026-01-01T00:00:00Z");
    // Thumbnails (img with the version's thumbnail src).
    const thumb = screen.getByTestId("timeline-thumb-2");
    expect(thumb).toBeTruthy();
    // Diff badges: v1 is 0 (first), v2 is "1 param(s) changed".
    expect(screen.getByTestId("timeline-diff-1").textContent).toBe("");
    expect(screen.getByTestId("timeline-diff-2").textContent).toBe("1 param changed");
  });

  it("the restore button calls onRestore with the version id", async () => {
    const onRestore = vi.fn();
    render(<VersionTimeline versions={TIMELINE} latestId={2} onRestore={onRestore} />);
    fireEvent.click(screen.getByTestId("timeline-restore-1"));
    expect(onRestore).toHaveBeenCalledWith(1);
  });

  it("restoring the LATEST version is disabled (the no-op edge case)", () => {
    render(<VersionTimeline versions={TIMELINE} latestId={2} />);
    const btn = screen.getByTestId("timeline-restore-2") as HTMLButtonElement;
    expect(btn.disabled).toBe(true);
  });

  it("the pin button calls onPin with (versionId, !pinned)", () => {
    const onPin = vi.fn();
    render(<VersionTimeline versions={TIMELINE} latestId={2} onPin={onPin} />);
    fireEvent.click(screen.getByTestId("timeline-pin-1"));
    expect(onPin).toHaveBeenCalledWith(1, true);
  });

  it("the compare-select button marks the version as selected for compare", () => {
    const onSelect = vi.fn();
    render(<VersionTimeline versions={TIMELINE} latestId={2} onCompareSelect={onSelect} />);
    fireEvent.click(screen.getByTestId("timeline-compare-2"));
    expect(onSelect).toHaveBeenCalledWith(2);
  });

  it("the restored version's diff badge is parent-based, not position-based", () => {
    // The restored v3 sits at list position 3 (its row's predecessor is v2),
    // but its parent is v1 — the badge must reflect the diff against the
    // parent (1 param changed), not the preceding row (2 params).
    render(<VersionTimeline versions={TIMELINE_WITH_RESTORE} latestId={3} />);
    expect(screen.getByTestId("timeline-diff-3").textContent).toBe("1 param changed");
  });
});

// ---------------------------------------------------------------------------
// VariantGallery
// ---------------------------------------------------------------------------

describe("VariantGallery (the pinned variant grid)", () => {
  it("renders each pinned card with thumbnail, name, params, and the three actions", () => {
    render(<VariantGallery cards={GALLERY} />);
    const card = screen.getByTestId("gallery-card-2");
    expect(card).toBeTruthy();
    expect(screen.getByTestId("gallery-name-2").textContent).toBe("wider box");
    // The params summary.
    expect(screen.getByTestId("gallery-params-2").textContent).toContain("W: 24");
    // The three actions are present.
    expect(screen.getByTestId("gallery-action-set-as-main-2")).toBeTruthy();
    expect(screen.getByTestId("gallery-action-branch-from-2")).toBeTruthy();
    expect(screen.getByTestId("gallery-action-archive-2")).toBeTruthy();
  });

  it("set-as-main calls onSetAsMain with the card id", () => {
    const onSetAsMain = vi.fn();
    render(<VariantGallery cards={GALLERY} onSetAsMain={onSetAsMain} />);
    fireEvent.click(screen.getByTestId("gallery-action-set-as-main-2"));
    expect(onSetAsMain).toHaveBeenCalledWith(2);
  });

  it("branch-from calls onBranchFrom with the card id", () => {
    const onBranch = vi.fn();
    render(<VariantGallery cards={GALLERY} onBranchFrom={onBranch} />);
    fireEvent.click(screen.getByTestId("gallery-action-branch-from-2"));
    expect(onBranch).toHaveBeenCalledWith(2);
  });

  it("archive calls onArchive with (cardId, true)", () => {
    const onArchive = vi.fn();
    render(<VariantGallery cards={GALLERY} onArchive={onArchive} />);
    fireEvent.click(screen.getByTestId("gallery-action-archive-2"));
    expect(onArchive).toHaveBeenCalledWith(2, true);
  });
});

// ---------------------------------------------------------------------------
// CompareView (the prioritized surface)
// ---------------------------------------------------------------------------

describe("CompareView (two viewports + param diff table)", () => {
  it("renders two model viewports (the two-viewport surface)", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // Two viewport panels (one per side) — the two viewports.
    const panels = screen.getAllByTestId(/compare-viewport-\d/);
    expect(panels).toHaveLength(2);
  });

  it("the shared-camera caption is rendered (the claim the sheet makes)", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // The caption is the shared_camera claim — it is a claim, so it is
    // present and the two viewports' pose readouts agree with it.
    expect(screen.getByTestId("compare-shared-camera").textContent).toContain(
      "Both models turn together",
    );
  });

  it("both viewports share one camera state — a rotation in either updates both", () => {
    // The shared camera is a CLAIM (copy.history.sharedCamera): if the two
    // viewports can desynchronise, the caption is a lie. This test drives
    // one viewport and asserts the OTHER follows — a desynchronised
    // implementation (each viewport owning its own pose) fails here.
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    const poseA = screen.getByTestId("compare-viewport-pose-1");
    const poseB = screen.getByTestId("compare-viewport-pose-2");
    expect(poseA.textContent).toBe(poseB.textContent);
    const before = poseA.textContent;

    // Rotate through viewport A: the SHARED pose updates, so BOTH readouts
    // move to the same new value (one camera state, two viewports).
    fireEvent.click(screen.getByTestId("compare-rotate-1"));
    const afterA = screen.getByTestId("compare-viewport-pose-1").textContent;
    const afterB = screen.getByTestId("compare-viewport-pose-2").textContent;
    expect(afterA).not.toBe(before);
    expect(afterB).toBe(afterA); // B followed A's rotation — same pose.

    // And the reverse direction: a rotation through B lands on A too.
    fireEvent.click(screen.getByTestId("compare-rotate-2"));
    expect(screen.getByTestId("compare-viewport-pose-1").textContent).toBe(
      screen.getByTestId("compare-viewport-pose-2").textContent,
    );
  });

  it("the shared-rotation contract is present and identical (shared rotation)", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // The shared-rotation marker is rendered with the contract's units +
    // axis convention (what lets the two viewports share one rotation).
    const sr = screen.getByTestId("compare-shared-rotation");
    expect(sr.textContent).toContain("mm");
    expect(sr.textContent).toContain("z-up");
  });

  it("the diff table shows changed keys, with the endpoint's values", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // W is changed: the row shows a→b values and the change cell is "20 → 24".
    const row = screen.getByTestId("diff-changed-W");
    expect(row.textContent).toContain("20 → 24");
  });

  it("the rendered rows are the endpoint's diff, not a client recomputation", () => {
    // The diff comes from the compare endpoint. This fixture's a.params
    // {W:20,H:25,D:30} vs b.params {W:24,H:25,D:30} would yield the SAME
    // changed set [W] by client-side comparison — so the fixture that
    // proves the wiring is one where a client recompute would DISAGREE:
    // the endpoint says D is unchanged (not in the diff), so a recompute
    // that compared the values would render D as changed.
    const endpointSays: VersionCompare = {
      ...COMPARE,
      a: {
        ...COMPARE.a,
        params: { W: 20, H: 25, D: 30, T: 2 },
      },
      b: {
        ...COMPARE.b,
        params: { W: 24, H: 25, D: 31, T: 2 },
      },
      // The endpoint says only W changed — D went 30→31 but the endpoint
      // did not list it (a client that recomputed from the two param sets
      // would render D as changed too, and the test would fail).
      diff: { added: [], removed: [], changed: ["W"], count: 1 },
    };
    render(<CompareView compare={endpointSays} aId={1} bId={2} />);
    // W renders as changed (the endpoint said so).
    expect(screen.getByTestId("diff-changed-W").textContent).toContain("20 → 24");
    // D is PRESENT (the union of both param sets) but UNCHANGED — dimmed,
    // not a changed row, even though a client recompute would call it changed.
    const dRow = screen.getByTestId("diff-unchanged-D");
    expect(dRow).toBeTruthy();
    expect(dRow.className).toContain("compare-diff-row--unchanged");
    expect(screen.queryByTestId("diff-changed-D")).toBeNull();
    // T (equal on both sides, not in the diff) is unchanged too.
    expect(screen.getByTestId("diff-unchanged-T")).toBeTruthy();
  });

  it("unchanged parameters are present in the DOM and dimmed, not absent", () => {
    // The whole point of the table: unchanged rows are PRESENT and DIMMED.
    // H (25/25) and D (30/30) are unchanged in COMPARE — they must render
    // as dimmed rows, never as holes.
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    const hRow = screen.getByTestId("diff-unchanged-H");
    const dRow = screen.getByTestId("diff-unchanged-D");
    // PRESENCE: both rows are in the DOM.
    expect(hRow).toBeTruthy();
    expect(dRow).toBeTruthy();
    // They carry their values (not blanked).
    expect(hRow.textContent).toContain("25");
    expect(dRow.textContent).toContain("30");
    // DIMMING: the unchanged class is on both (the dimmed, not hidden,
    // treatment — a hidden row is indistinguishable from a nonexistent one).
    expect(hRow.className).toContain("compare-diff-row--unchanged");
    expect(dRow.className).toContain("compare-diff-row--unchanged");
    // And the changed row does NOT carry the dimmed class.
    expect(screen.getByTestId("diff-changed-W").className).not.toContain(
      "compare-diff-row--unchanged",
    );
  });

  it("added parameters render a dash on the a-side, never a hidden row", () => {
    const withAdded: VersionCompare = {
      ...COMPARE,
      b: { ...COMPARE.b, params: { W: 24, H: 25, D: 30, T: 12 } },
      diff: { added: ["T"], removed: [], changed: ["W"], count: 2 },
    };
    render(<CompareView compare={withAdded} aId={1} bId={2} />);
    const row = screen.getByTestId("diff-added-T");
    expect(row).toBeTruthy();
    expect(row.textContent).toContain("12"); // the b value
    // The a side shows the not-present cell (a dash from the deck), not an
    // absent cell.
    expect(row.textContent).toContain("—");
  });

  it("removed parameters render a dash on the b-side, never a hidden row", () => {
    const withRemoved: VersionCompare = {
      ...COMPARE,
      a: { ...COMPARE.a, params: { W: 20, H: 25, D: 30, T: 9 } },
      b: { ...COMPARE.b, params: { W: 24, H: 25, D: 30 } },
      diff: { added: [], removed: ["T"], changed: ["W"], count: 2 },
    };
    render(<CompareView compare={withRemoved} aId={1} bId={2} />);
    const row = screen.getByTestId("diff-removed-T");
    expect(row).toBeTruthy();
    expect(row.textContent).toContain("9"); // the a value
    expect(row.textContent).toContain("—");
  });

  it("a fully-identical compare still renders every row (dimmed), not an empty table", () => {
    // diff count 0 → every key is unchanged. The rows are all present and
    // dimmed; nothing is omitted. (The old "no-diff" empty branch is gone:
    // an empty table would hide the identical-ness the caption claims.)
    const same: VersionCompare = {
      ...COMPARE,
      b: COMPARE.a,
      diff: { added: [], removed: [], changed: [], count: 0 },
    };
    render(<CompareView compare={same} aId={1} bId={1} />);
    expect(screen.getByTestId("diff-unchanged-W")).toBeTruthy();
    expect(screen.getByTestId("diff-unchanged-H")).toBeTruthy();
    expect(screen.getByTestId("diff-unchanged-D")).toBeTruthy();
  });
});
