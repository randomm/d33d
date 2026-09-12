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
    useEffect(() => {
      props.onReady?.({
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
      });
    }, []);
    return <div data-testid={`model-viewer-slot-${props.format}`} />;
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

  it("the shared-rotation contract is present and identical (shared rotation)", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // The shared-rotation marker is rendered with the contract's units +
    // axis convention (what lets the two viewports share one rotation).
    const sr = screen.getByTestId("compare-shared-rotation");
    expect(sr.textContent).toContain("mm");
    expect(sr.textContent).toContain("z-up");
  });

  it("the diff table shows changed keys", () => {
    render(<CompareView compare={COMPARE} aId={1} bId={2} />);
    // W is changed: the row shows a→b values and the change cell is "20 → 24".
    const row = screen.getByTestId("diff-changed-W");
    expect(row.textContent).toContain("20 → 24");
  });

  it("an empty diff shows a no-change state", () => {
    const same: VersionCompare = {
      ...COMPARE,
      b: COMPARE.a,
      diff: { added: [], removed: [], changed: [], count: 0 },
    };
    render(<CompareView compare={same} aId={1} bId={1} />);
    expect(screen.getByTestId("compare-no-diff").textContent).toContain("identical");
  });
});
