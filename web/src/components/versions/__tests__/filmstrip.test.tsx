/**
 * Filmstrip tests — the version-tail overlay (right edge): the timeline
 * rail plus the compare pane and the pinned-variants gallery.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Filmstrip } from "../Filmstrip";
import type { VersionTimelineEntry, VersionCompare } from "../../../lib/api";

function entry(id: number, opts: Partial<VersionTimelineEntry> = {}): VersionTimelineEntry {
  return {
    id,
    name: `Version ${id}`,
    params: {},
    created_by_message: "initial design",
    parent: null,
    restored_from: null,
    forked_from: null,
    pinned: false,
    archived: false,
    thumbnail: null,
    created_at: "2026-01-01T00:00:00Z",
    diff_count: 0,
    ...opts,
  };
}

function makeCompare(a: VersionTimelineEntry, b: VersionTimelineEntry): VersionCompare {
  return {
    project_id: 1,
    a,
    b,
    diff: { added: [], removed: [], changed: [], count: 0 },
    shared_rotation: { units: "mm", axis_convention: "z-up", identical_convention: true },
  };
}

describe("Filmstrip", () => {
  it("renders the version timeline pane with the correct positioning", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2)]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    const pane = screen.getByTestId("version-timeline-pane");
    expect(pane).toBeTruthy();
    expect(pane.style.position).toBe("absolute");
    expect(pane.style.right).toBe("24px");
    expect(pane.style.zIndex).toBe("10");
    expect(pane.style.width).toBe("300px");
  });

  it("renders a timeline entry per version", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2)]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("timeline-entry-1")).toBeTruthy();
    expect(screen.getByTestId("timeline-entry-2")).toBeTruthy();
  });

  it("does NOT render the compare pane until compareIds and compareResult are both set", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2)]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("compare-pane")).toBeNull();
  });

  it("renders the compare pane when both compareIds and compareResult are set", () => {
    const a = entry(1);
    const b = entry(2);
    render(
      <Filmstrip
        versions={[a, b]}
        compareIds={[1, 2]}
        compareResult={makeCompare(a, b)}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("compare-pane")).toBeTruthy();
    expect(screen.getByTestId("compare-view")).toBeTruthy();
  });

  it("shows the compare error inside the compare pane", () => {
    const a = entry(1);
    const b = entry(2);
    render(
      <Filmstrip
        versions={[a, b]}
        compareIds={[1, 2]}
        compareResult={makeCompare(a, b)}
        compareError="Compare failed: 500"
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("compare-error").textContent).toContain("Compare failed");
  });

  it("does NOT render the gallery pane when no version is pinned", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2)]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("gallery-pane")).toBeNull();
  });

  it("renders the gallery pane when a version is pinned (and not archived)", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2, { pinned: true })]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.getByTestId("gallery-pane")).toBeTruthy();
    expect(screen.getByTestId("gallery-card-2")).toBeTruthy();
  });

  it("does NOT include archived pinned versions in the gallery", () => {
    render(
      <Filmstrip
        versions={[entry(1), entry(2, { pinned: true, archived: true })]}
        compareIds={null}
        compareResult={null}
        compareError={null}
        inset={24}
        onRestore={vi.fn()}
        onPin={vi.fn()}
        onCompareSelect={vi.fn()}
      />,
    );
    expect(screen.queryByTestId("gallery-pane")).toBeNull();
  });
});
