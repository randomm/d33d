/**
 * Brief tests — the "what are we building" overlay (top-left).
 */

import { render, screen } from "@testing-library/react";
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

  it("positions with the given inset", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    const el = screen.getByTestId("brief-panel");
    expect(el.style.top).toBe("24px");
    expect(el.style.left).toBe("24px");
    expect(el.style.position).toBe("absolute");
    expect(el.style.zIndex).toBe("10");
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
    // .map() path that emits the key warning. This one renders three entries
    // mixing provenances so both list maps (unknowns + resolved) fire.
    const entries: DesignStateEntry[] = [
      { name: "W", label: "Width", value: 60, unit: "mm", provenance: "stated" },
      {
        name: "H",
        label: "Height",
        value: null,
        unit: null,
        provenance: "unknown",
      },
      {
        name: "D",
        label: "Depth",
        value: 45,
        unit: "mm",
        provenance: "measured",
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
});
