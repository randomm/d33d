/**
 * Brief tests — the "what are we building" overlay (top-right full panel,
 * top-left chip — issue #209).
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { Brief } from "../Brief";
import copy from "../../../copy";
import type { DesignStateEntry } from "../../../lib/api";

describe("Brief", () => {
  it("renders the eyebrow line and the empty body when no entries are given", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel")).toBeTruthy();
    // The eyebrow is always present; with no entries the empty body follows.
    expect(screen.getByTestId("brief-panel").textContent).toContain("What we're building");
    expect(screen.getByTestId("brief-empty").textContent).toBe(copy.brief.emptyBody);
  });

  it("renders as a chip when isChip is true", () => {
    render(<Brief isChip inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("chip");
  });

  it("renders as a full panel when isChip is false", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("full");
  });

  it("the collapsed chip counts assumed separately from unknowns (issue #246)", () => {
    const entries: DesignStateEntry[] = [
      { name: "W", kind: "param" as const, label: "Width", value: 60, unit: "mm", provenance: "stated" },
      { name: "spacer_height", kind: "param" as const, label: "spacer_height", value: 12, unit: "mm", provenance: "assumed" },
      { name: "wall_thickness", kind: "param" as const, label: "wall_thickness", value: 3, unit: "mm", provenance: "assumed" },
      { name: "hole_clearance", kind: "param" as const, label: "hole_clearance", value: 0.3, unit: "mm", provenance: "assumed" },
      { name: "H", kind: "param" as const, label: "Height", value: null, unit: null, provenance: "unknown" },
    ];
    render(<Brief isChip inset={24} conversationCollapsed={false} entries={entries} />);
    const chip = screen.getByTestId("brief-chip");
    // Assumed count is separate from unknowns — "3 assumed" next to
    // "1 unknown", not folded into either.
    expect(screen.getByTestId("brief-chip-assumed").textContent).toBe(
      copy.brief.collapsedAssumed(3),
    );
    expect(screen.getByTestId("brief-chip-unknowns").textContent).toBe(
      copy.brief.collapsedUnknowns(1),
    );
    // The resolved rows (assumed values included) stay in the chip.
    expect(chip.textContent).toContain("Width · 60.0\u202Fmm");
    expect(chip.textContent).toContain("spacer_height · 12.0\u202Fmm");
  });

  it("the chip shows no assumed span when no entry is assumed (issue #246)", () => {
    render(
      <Brief
        isChip
        inset={24}
        conversationCollapsed={false}
        entries={[{ name: "W", kind: "param" as const, label: "Width", value: 60, unit: "mm", provenance: "stated" }]}
      />,
    );
    expect(screen.queryByTestId("brief-chip-assumed")).toBeNull();
  });

  it("positions with the given inset", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    const el = screen.getByTestId("brief-panel");
    expect(el.style.top).toBe("24px");
    // Full panel is right-anchored (issue #209): no left offset.
    expect(el.style.right).toBe("24px");
    expect(el.style.left).toBe("");
    expect(el.style.position).toBe("absolute");
    expect(el.style.zIndex).toBe("10");
  });

  it("keeps the chip left-anchored (issue #209)", () => {
    render(<Brief isChip inset={24} conversationCollapsed={false} />);
    const el = screen.getByTestId("brief-panel");
    expect(el.style.top).toBe("24px");
    expect(el.style.left).toBe("24px");
    expect(el.style.right).toBe("");
  });

  it("overrides margin-top only when the conversation is not collapsed (byte-identical to the pre-extraction inline style)", () => {
    const { unmount } = render(
      <Brief isChip={false} inset={24} conversationCollapsed={false} />
    );
    expect(screen.getByTestId("brief-panel").style.marginTop).toBe("0px");
    unmount();
    render(<Brief isChip={false} inset={24} conversationCollapsed />);
    expect(screen.getByTestId("brief-panel").style.marginTop).toBe("");
  });

  it("renders multiple entries without duplicate-key console warnings (issue #196)", () => {
    // The existing tests render ≤1 entry, so they never hit the multi-row
    // .map() path that emits the key warning. This one renders four entries
    // mixing provenances (including "assumed", issue #246) so both list
    // maps (unknowns + resolved) fire.
    const entries: DesignStateEntry[] = [
      { name: "W", kind: "param" as const, label: "Width", value: 60, unit: "mm", provenance: "stated" },
      {
        name: "H", kind: "param" as const,
        label: "Height",
        value: null,
        unit: null,
        provenance: "unknown",
      },
      {
        name: "D", kind: "param" as const,
        label: "Depth",
        value: 45,
        unit: "mm",
        provenance: "measured",
      },
      {
        name: "wall_thickness", kind: "param" as const,
        label: "wall_thickness",
        value: 3,
        unit: "mm",
        provenance: "assumed",
      },
    ];
    const consoleErrorSpy = vi.spyOn(console, "error");
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} entries={entries} />);
    const keyWarnings = consoleErrorSpy.mock.calls.filter((c) =>
      String(c[0]).includes('Each child in a list should have a unique "key" prop'),
    );
    expect(keyWarnings).toEqual([]);
    consoleErrorSpy.mockRestore();
  });

  it("the label renders in the UI face when label_is_identifier is false (issue #248)", () => {
    const entries: DesignStateEntry[] = [
      { name: "fillet_size_top", kind: "param", label: "Top fillet size", value: 2, unit: "mm", provenance: "assumed", label_is_identifier: false },
    ];
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} entries={entries} />);
    const labelEl = screen.getByText("Top fillet size");
    expect(labelEl.style.fontFamily).toBe("var(--font-ui)");
  });

  it("the identifier renders in the mono face when label_is_identifier is true (issue #248)", () => {
    const entries: DesignStateEntry[] = [
      { name: "fst", kind: "param", label: "fst", value: 2, unit: "mm", provenance: "assumed", label_is_identifier: true },
    ];
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} entries={entries} />);
    const labelEl = screen.getByText("fst");
    expect(labelEl.style.fontFamily).toBe("var(--font-mono)");
  });

  it("the assumed expanded row shows the reason sentence when one exists (issue #248)", () => {
    const entries: DesignStateEntry[] = [
      { name: "wall_t", kind: "param", label: "Wall thickness", value: 3, unit: "mm", provenance: "assumed", reason: "0.4 mm nozzle FDM tolerance" },
    ];
    const { container } = render(<Brief isChip={false} inset={24} conversationCollapsed={false} entries={entries} />);
    const row = container.querySelector("[data-testid='brief-row-wall_t']");
    const clickable = row?.querySelector("div[style*='cursor']");
    if (clickable) fireEvent.click(clickable);
    const expanded = container.querySelector("[data-testid='brief-row-expanded']");
    expect(expanded?.textContent).toContain(
      copy.brief.provenanceAssumedWithReason("3.0\u202Fmm", "0.4 mm nozzle FDM tolerance"),
    );
  });

  it("the assumed expanded row shows the reason-less sentence when no reason (issue #246 fallback)", () => {
    const entries: DesignStateEntry[] = [
      { name: "wall_t", kind: "param", label: "Wall thickness", value: 3, unit: "mm", provenance: "assumed" },
    ];
    const { container } = render(<Brief isChip={false} inset={24} conversationCollapsed={false} entries={entries} />);
    const row = container.querySelector("[data-testid='brief-row-wall_t']");
    const clickable = row?.querySelector("div[style*='cursor']");
    if (clickable) fireEvent.click(clickable);
    const expanded = container.querySelector("[data-testid='brief-row-expanded']");
    expect(expanded?.textContent).toContain(copy.brief.provenanceAssumed("3.0\u202Fmm"));
  });
});
