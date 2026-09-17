/**
 * HistorySheet tests (W16, issue #127).
 *
 * The sheet is the expanded history surface: an OVERLAY over the canvas
 * (not a route, not a page), reached from the filmstrip's expand mark.
 * It is where the compare / restore / pin actions belong — every one of
 * those three is reachable from the sheet, which is what retires the
 * transitional VersionTail rail.
 *
 * ModelViewer is mocked (jsdom cannot construct a WebGLRenderer) — the
 * sheet's compare viewports do not load geometry (the endpoint returns
 * no geometry payload), so the mock is only there for the import graph.
 */
import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import {
  type VersionTimelineEntry,
  type VersionCompare,
} from "../../../lib/api";
import { HistorySheet } from "../HistorySheet";

vi.mock("../../viewer/ModelViewer", async () => {
  const actual = await vi.importActual<typeof import("../../viewer/ModelViewer")>(
    "../../viewer/ModelViewer",
  );
  const MockModelViewer = (props: {
    data: ArrayBuffer | null;
    format: string;
    onReady?: (handle: unknown) => void;
  }) => {
    void props;
    return <div data-testid="model-viewer-mock" />;
  };
  return { ...actual, ModelViewer: MockModelViewer };
});

// ---------------------------------------------------------------------------
// Fixtures
// ---------------------------------------------------------------------------

const SHEET_VERSIONS: VersionTimelineEntry[] = [
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
    thumbnail: null,
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
    thumbnail: null,
    created_at: "2026-01-02T00:00:00Z",
    diff_count: 1,
    exported_at: null,
  },
  {
    id: 3,
    name: "restored back",
    params: { W: 22, H: 25, D: 30 },
    created_by_message: "restored from version 1",
    parent: 2,
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

const SHEET_COMPARE: VersionCompare = {
  project_id: 1,
  a: SHEET_VERSIONS[0],
  b: SHEET_VERSIONS[1],
  diff: { added: [], removed: [], changed: ["W"], count: 1 },
  shared_rotation: { units: "mm", axis_convention: "z-up", identical_convention: true },
};

function sheetProps(
  overrides: Partial<React.ComponentProps<typeof HistorySheet>> = {},
) {
  return {
    versions: SHEET_VERSIONS,
    compareIds: null,
    compareResult: null,
    compareError: null,
    inset: 24,
    onRestore: vi.fn(),
    onPin: vi.fn(),
    onCompareSelect: vi.fn(),
    onClose: vi.fn(),
    ...overrides,
  };
}

// ---------------------------------------------------------------------------
// The sheet as an overlay (not a route, not a page)
// ---------------------------------------------------------------------------

describe("HistorySheet (the expanded overlay)", () => {
  it("renders as an absolutely-positioned overlay, not a route or a page", () => {
    render(<HistorySheet {...sheetProps()} />);
    const sheet = screen.getByTestId("history-sheet");
    // The sheet is an OVERLAY: absolutely positioned within the stage (the
    // same contract the filmstrip and the brief have) — not a document.body
    // child, not a <main>/<header> page surface, not a router outlet.
    expect(sheet.style.position).toBe("absolute");
    expect(sheet.style.right).toBe("24px");
    expect(sheet.style.top).toBe("24px");
    // No page-level semantics: the sheet is a div, not a <main>/<section>
    // with a <h1> page title.
    expect(sheet.tagName).toBe("DIV");
    // It does not render a router: no <a href> navigation to a path, no
    // <nav> with route links.
    expect(sheet.querySelectorAll("a[href^='/']")).toHaveLength(0);
    // The close button returns to the filmstrip (the overlay's exit).
    expect(screen.getByTestId("history-sheet-close")).toBeTruthy();
  });

  it("closing the sheet calls onClose", () => {
    const onClose = vi.fn();
    render(<HistorySheet {...sheetProps({ onClose })} />);
    fireEvent.click(screen.getByTestId("history-sheet-close"));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it("is reachable only from the filmstrip: App does not mount it until an expand mark opens it", () => {
    // The "no route or page added" test's DOM half: the sheet has no URL of
    // its own. In App, the sheet is absent until sheetOpenFor is set by a
    // filmstrip expand-mark click (app-layout.test covers the wiring); this
    // component test pins the sheet's own exit (the close button) so the
    // overlay cannot render itself into anything that outlives the flag.
    const { unmount } = render(<HistorySheet {...sheetProps()} />);
    unmount();
    expect(screen.queryByTestId("history-sheet")).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Compare / restore / pin are all reachable from the sheet
// ---------------------------------------------------------------------------

describe("HistorySheet (the home of compare, restore and pin)", () => {
  it("compare is reachable: the timeline's compare-select button fires onCompareSelect", () => {
    const onCompareSelect = vi.fn();
    render(<HistorySheet {...sheetProps({ onCompareSelect })} />);
    fireEvent.click(screen.getByTestId("timeline-compare-1"));
    expect(onCompareSelect).toHaveBeenCalledWith(1);
  });

  it("the compare pane renders the fetched result when two versions are selected", () => {
    render(
      <HistorySheet
        {...sheetProps({ compareIds: [1, 2], compareResult: SHEET_COMPARE })}
      />,
    );
    expect(screen.getByTestId("compare-pane")).toBeTruthy();
    expect(screen.getByTestId("compare-view").textContent).toContain(
      "first box vs wider box",
    );
  });

  it("restore is reachable: the timeline's restore button fires onRestore with the id", () => {
    const onRestore = vi.fn();
    render(<HistorySheet {...sheetProps({ onRestore })} />);
    // v1 (non-latest) is enabled; v3 (the latest) is disabled.
    expect(screen.getByTestId("timeline-restore-1")).not.toBeDisabled();
    expect(screen.getByTestId("timeline-restore-3")).toBeDisabled();
    fireEvent.click(screen.getByTestId("timeline-restore-1"));
    expect(onRestore).toHaveBeenCalledWith(1);
  });

  it("pin is reachable: the timeline's pin button fires onPin with the toggle", () => {
    const onPin = vi.fn();
    render(<HistorySheet {...sheetProps({ onPin })} />);
    // v2 is pinned (the gallery shows it); clicking v1's pin toggles it on.
    fireEvent.click(screen.getByTestId("timeline-pin-1"));
    expect(onPin).toHaveBeenCalledWith(1, true);
    // Toggling the already-pinned v2 off.
    fireEvent.click(screen.getByTestId("timeline-pin-2"));
    expect(onPin).toHaveBeenCalledWith(2, false);
  });

  it("the pinned gallery is inside the sheet and its actions are wired", () => {
    const onPin = vi.fn();
    render(<HistorySheet {...sheetProps({ onPin })} />);
    // v2 is pinned → the gallery pane is present INSIDE the sheet.
    const gallery = screen.getByTestId("gallery-pane");
    expect(gallery).toBeTruthy();
    expect(gallery.closest("[data-testid='history-sheet']")).toBeTruthy();
    expect(screen.getByTestId("gallery-card-2")).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// The branch riser graph
// ---------------------------------------------------------------------------

describe("HistorySheet (the branch riser graph)", () => {
  it("draws the graph with one node per version and a parent riser to it", () => {
    render(<HistorySheet {...sheetProps()} />);
    const graph = screen.getByTestId("branch-graph");
    expect(graph).toBeTruthy();
    // One node per version.
    expect(screen.getByTestId("branch-node-1")).toBeTruthy();
    expect(screen.getByTestId("branch-node-2")).toBeTruthy();
    expect(screen.getByTestId("branch-node-3")).toBeTruthy();
    // The parent riser on v2 (parent v1) and v3 (parent v2).
    expect(screen.getByTestId("branch-parent-edge-2")).toBeTruthy();
    expect(screen.getByTestId("branch-parent-edge-3")).toBeTruthy();
    // v1 is the root: no parent edge.
    expect(screen.queryByTestId("branch-parent-edge-1")).toBeNull();
  });

  it("draws the restore riser (the rise-back) from the restored_from pointer", () => {
    render(<HistorySheet {...sheetProps()} />);
    // v3 was restored from v1: the riser is drawn back to v1.
    const riser = screen.getByTestId("branch-restore-riser-3");
    expect(riser).toBeTruthy();
    // No restore riser on v1/v2.
    expect(screen.queryByTestId("branch-restore-riser-1")).toBeNull();
    expect(screen.queryByTestId("branch-restore-riser-2")).toBeNull();
  });

  it("marks the pinned variant and carries its why (the version's own message)", () => {
    render(<HistorySheet {...sheetProps()} />);
    // v2 is pinned → the graph carries its mark + the why (its message).
    const mark = screen.getByTestId("branch-pinned-2");
    expect(mark.textContent).toContain("v2");
    expect(mark.textContent).toContain("pinned");
    expect(mark.textContent).toContain("widen the box");
    // v1 and v3 are not pinned.
    expect(screen.queryByTestId("branch-pinned-1")).toBeNull();
    expect(screen.queryByTestId("branch-pinned-3")).toBeNull();
  });

  it("the graph's legend names both edge kinds", () => {
    render(<HistorySheet {...sheetProps()} />);
    expect(screen.getByTestId("branch-legend-parent")).toBeTruthy();
    expect(screen.getByTestId("branch-legend-restored")).toBeTruthy();
  });
});
