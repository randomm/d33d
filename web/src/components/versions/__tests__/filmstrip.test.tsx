/**
 * Filmstrip tests — the horizontal version strip (bottom-left, ~96px).
 *
 * The strip holds four fixed slots for the most recent versions, collapses
 * everything older into one honest count, renders a dashed pending slot while
 * a pass is in flight, outlines the current version in --color-live, and marks
 * a version that has siblings with a fork glyph. It is ABSENT (renders
 * nothing) when a project has no versions and no pass is in flight. It never
 * shows git.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Filmstrip } from "../Filmstrip";
import type { VersionTimelineEntry } from "../../../lib/api";

function entry(
  id: number,
  opts: Partial<VersionTimelineEntry> & { exported_at?: string | null } = {},
): VersionTimelineEntry {
  const { exported_at, ...rest } = opts;
  return {
    id,
    name: `Version ${id}`,
    params: {},
    created_by_message: "initial design",
    parent: id === 1 ? null : id - 1,
    restored_from: null,
    forked_from: null,
    pinned: false,
    archived: false,
    thumbnail: null,
    created_at: "2026-01-01T00:00:00Z",
    diff_count: 0,
    exported_at: exported_at ?? null,
    ...rest,
  };
}

function baseProps(overrides: Partial<React.ComponentProps<typeof Filmstrip>> = {}) {
  return {
    versions: [] as VersionTimelineEntry[],
    passInFlight: false,
    pendingName: null,
    inset: 24,
    onCompareSelect: vi.fn(),
    ...overrides,
  };
}

describe("Filmstrip", () => {
  it("is absent (renders nothing) at zero versions with no pass in flight", () => {
    const { container } = render(<Filmstrip {...baseProps()} />);
    expect(container.querySelector(".filmstrip")).toBeNull();
    expect(screen.queryByTestId("version-filmstrip")).toBeNull();
  });

  it("is present at one version", () => {
    render(<Filmstrip {...baseProps({ versions: [entry(1)] })} />);
    expect(screen.getByTestId("version-filmstrip")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-slot-1")).toBeTruthy();
  });

  it("renders a dashed pending entry during a pass, before any version exists", () => {
    // Zero versions + pass in flight → the strip appears with just the
    // dashed pending slot. This is the "never behind the conversation" rule:
    // it renders from in-flight state, not the versions list.
    render(
      <Filmstrip
        {...baseProps({ passInFlight: true, pendingName: "widen the box" })}
      />,
    );
    expect(screen.getByTestId("version-filmstrip")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-pending")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-pending-name").textContent).toBe("widen the box");
    // No version slots yet.
    expect(screen.queryByTestId(/filmstrip-slot-\d/)).toBeNull();
  });

  it("renders the pending slot's building label when no intended name is given", () => {
    render(<Filmstrip {...baseProps({ passInFlight: true })} />);
    expect(screen.getByTestId("filmstrip-pending").textContent).toContain("building");
  });

  it("the current version is the only one marked current", () => {
    const versions = [entry(1), entry(2)];
    render(<Filmstrip {...baseProps({ versions })} />);
    const current = screen.getByTestId("filmstrip-slot-2");
    const other = screen.getByTestId("filmstrip-slot-1");
    expect(current.getAttribute("aria-current")).toBe("true");
    expect(other.getAttribute("aria-current")).toBeNull();
    // Exactly one carries the current outline.
    const outlined = Array.from(
      document.querySelectorAll(".filmstrip-slot--current"),
    );
    expect(outlined).toHaveLength(1);
  });

  it("at 40 versions the single-current assertion still holds across the collapsed earlier-count", () => {
    const versions = Array.from({ length: 40 }, (_, i) => entry(i + 1));
    render(<Filmstrip {...baseProps({ versions })} />);
    // The latest (id 40) is the only current.
    expect(screen.getByTestId("filmstrip-slot-40").getAttribute("aria-current")).toBe("true");
    const outlined = Array.from(
      document.querySelectorAll(".filmstrip-slot--current"),
    );
    expect(outlined).toHaveLength(1);
  });

  it("slot count is four at 4 versions with no earlier-count", () => {
    const versions = [entry(1), entry(2), entry(3), entry(4)];
    render(<Filmstrip {...baseProps({ versions })} />);
    const slots = screen.getAllByTestId(/^filmstrip-slot-\d/);
    expect(slots).toHaveLength(4);
    expect(screen.queryByTestId("filmstrip-earlier")).toBeNull();
  });

  it("slot count is four at 40 versions, with the remainder as a single count", () => {
    const versions = Array.from({ length: 40 }, (_, i) => entry(i + 1));
    render(<Filmstrip {...baseProps({ versions })} />);
    // Four version slots (the earlier-count is not a 5th slot).
    expect(screen.getAllByTestId(/^filmstrip-slot-\d/)).toHaveLength(4);
    // The remainder collapses into one honest count: +36 earlier.
    const earlier = screen.getByTestId("filmstrip-earlier");
    expect(earlier.textContent).toContain("+36");
    expect(earlier.textContent).toContain("earlier");
  });

  it("at 5 versions: 4 slots + '+1 earlier'", () => {
    const versions = [entry(1), entry(2), entry(3), entry(4), entry(5)];
    render(<Filmstrip {...baseProps({ versions })} />);
    expect(screen.getAllByTestId(/^filmstrip-slot-\d/)).toHaveLength(4);
    // The four visible are the four MOST recent: 2,3,4,5.
    expect(screen.getByTestId("filmstrip-slot-2")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-slot-5")).toBeTruthy();
    expect(screen.queryByTestId("filmstrip-slot-1")).toBeNull();
    expect(screen.getByTestId("filmstrip-earlier").textContent).toContain("+1");
  });

  it("a version with siblings renders a fork mark and their count, and no riser", () => {
    // v2 and v3 share parent v1 → both are siblings (count 2). v4 has a unique
    // parent (v3) → no fork mark. The fork is a mark on the entry, never a
    // riser (risers are the W16 sheet's).
    const versions = [
      entry(1, { parent: null }),
      entry(2, { parent: 1 }),
      entry(3, { parent: 1 }),
      entry(4, { parent: 3 }),
    ];
    render(<Filmstrip {...baseProps({ versions })} />);
    expect(screen.getByTestId("filmstrip-fork-2")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-fork-3")).toBeTruthy();
    expect(screen.getByTestId("filmstrip-fork-2").textContent).toContain("2 variants");
    expect(screen.queryByTestId("filmstrip-fork-1")).toBeNull();
    expect(screen.queryByTestId("filmstrip-fork-4")).toBeNull();
  });

  it("renders no riser element anywhere in the strip", () => {
    const versions = [
      entry(1, { parent: null }),
      entry(2, { parent: 1 }),
      entry(3, { parent: 1 }),
    ];
    const { container } = render(<Filmstrip {...baseProps({ versions })} />);
    expect(container.querySelector(".filmstrip-riser")).toBeNull();
  });

  it("clicking a slot calls onCompareSelect with the version id", () => {
    const onCompareSelect = vi.fn();
    const versions = [entry(1), entry(2)];
    render(
      <Filmstrip {...baseProps({ versions, onCompareSelect })} />,
    );
    fireEvent.click(screen.getByTestId("filmstrip-slot-2"));
    expect(onCompareSelect).toHaveBeenCalledWith(2);
  });

  it("renders the version label and the diff fragment (v2 · D 30→45)", () => {
    const versions = [
      entry(1, { params: { D: 30 }, diff_count: 0, parent: null }),
      entry(2, { params: { D: 45 }, diff_count: 1, parent: 1 }),
    ];
    render(<Filmstrip {...baseProps({ versions })} />);
    const pos = screen.getByTestId("filmstrip-pos-2");
    expect(pos.textContent).toContain("v2");
    expect(pos.textContent).toContain("D 30→45");
    // The first version (no parent, diff_count 0) shows the label alone.
    expect(screen.getByTestId("filmstrip-pos-1").textContent).toBe(" · v1");
  });

  it("shows a thumbnail when the version has one", () => {
    const versions = [entry(1, { thumbnail: "/thumbs/v1.png" })];
    render(<Filmstrip {...baseProps({ versions })} />);
    const thumb = screen.getByTestId("filmstrip-thumb-1");
    expect(thumb).toBeTruthy();
  });

  it("renders at the bottom-left inset with z-index 10", () => {
    render(<Filmstrip {...baseProps({ versions: [entry(1)] })} />);
    const pane = screen.getByTestId("version-filmstrip");
    expect(pane.style.position).toBe("absolute");
    expect(pane.style.bottom).toBe("24px");
    expect(pane.style.left).toBe("24px");
    expect(pane.style.zIndex).toBe("10");
  });

  it("never renders a git commit hash or branch name", () => {
    const versions = [entry(1), entry(2, { name: "rod 45mm off wall" })];
    const { container } = render(<Filmstrip {...baseProps({ versions })} />);
    const text = container.textContent ?? "";
    // No 7-40 char lowercase hex run (a commit hash).
    expect(text).not.toMatch(/\b[0-9a-f]{7,40}\b/i);
    // No "branch:" git phrasing.
    expect(text).not.toMatch(/\bbranch\s*:/i);
  });

  it("renders the exported mark on the exported version and not on the others (issue #126)", () => {
    // The mark belongs to the version that was ACTUALLY exported — not
    // necessarily the latest. v2 is the exported one, v1 and v3 (the
    // latest) are not.
    const versions = [
      entry(1),
      entry(2, { exported_at: "2026-01-03T10:00:00.000Z" }),
      entry(3),
    ];
    render(<Filmstrip {...baseProps({ versions })} />);
    expect(screen.getByTestId("filmstrip-exported-2").textContent).toBe("exported");
    // The other versions carry no mark — the mark is per-version, not a
    // project-wide state.
    expect(screen.queryByTestId("filmstrip-exported-1")).toBeNull();
    expect(screen.queryByTestId("filmstrip-exported-3")).toBeNull();
  });

  it("the exported mark carries the export time in its title (copy.history.exportedAt)", () => {
    const versions = [entry(1, { exported_at: "2026-01-03T10:00:00.000Z" })];
    render(<Filmstrip {...baseProps({ versions })} />);
    const slot = screen.getByTestId("filmstrip-slot-1");
    expect(slot.getAttribute("title")).toBe("exported 2026-01-03T10:00:00.000Z");
  });
});

