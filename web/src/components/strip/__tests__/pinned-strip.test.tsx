/**
 * PinnedParamStrip tests — starts empty, max 3 entries, add/remove.
 *
 * Spec acceptance 5: "No slider panel is auto-generated from the parameter
 * block" — the pinned strip must start EMPTY and the user explicitly pins
 * at most 3.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PinnedParamStrip, type PinnedParam } from "../PinnedParamStrip";

describe("PinnedParamStrip", () => {
  it("starts empty with a 'No pinned parameters' message", () => {
    const onToggle = vi.fn();
    render(<PinnedParamStrip params={[]} onToggle={onToggle} />);
    expect(screen.getByTestId("pinned-empty")).toBeTruthy();
    expect(screen.queryByTestId(/^pinned-item-/)).toBeNull();
  });

  it("displays pinned parameters with their values", () => {
    const params: PinnedParam[] = [
      { name: "width", value: 25.0 },
      { name: "height", value: 12.5 },
    ];
    const onToggle = vi.fn();
    render(<PinnedParamStrip params={params} onToggle={onToggle} />);
    expect(screen.getByTestId("pinned-item-width")).toBeTruthy();
    expect(screen.getByTestId("pinned-item-height")).toBeTruthy();
    expect(screen.getByTestId("pinned-item-width")).toHaveTextContent("width: 25");
    expect(screen.getByTestId("pinned-item-height")).toHaveTextContent("height: 12.5");
  });

  it("allows adding a new pinned parameter (up to max)", () => {
    const onToggle = vi.fn();
    const params: PinnedParam[] = [];
    render(<PinnedParamStrip params={params} onToggle={onToggle} />);
    // Click add button
    fireEvent.click(screen.getByTestId("pinned-add-btn"));
    // Fill in name and value
    fireEvent.change(screen.getByTestId("pinned-new-name"), {
      target: { value: "depth" },
    });
    fireEvent.change(screen.getByTestId("pinned-new-value"), {
      target: { value: "8" },
    });
    fireEvent.click(screen.getByTestId("pinned-add-confirm"));
    expect(onToggle).toHaveBeenCalledWith("depth", 8);
  });

  it("caps pinned parameters at 3 (does not allow adding a 4th)", () => {
    const onToggle = vi.fn();
    const params: PinnedParam[] = [
      { name: "a", value: 1 },
      { name: "b", value: 2 },
      { name: "c", value: 3 },
    ];
    render(<PinnedParamStrip params={params} onToggle={onToggle} />);
    // When at max, the add button should not be visible
    expect(screen.queryByTestId("pinned-add-btn")).toBeNull();
    expect(screen.queryByTestId("pinned-add-form")).toBeNull();
  });

  it("calls onToggle to remove a pinned parameter when × is clicked", () => {
    const onToggle = vi.fn();
    const params: PinnedParam[] = [{ name: "width", value: 25 }];
    render(<PinnedParamStrip params={params} onToggle={onToggle} />);
    fireEvent.click(screen.getByTestId("pinned-remove-width"));
    expect(onToggle).toHaveBeenCalledWith("width", 25);
  });

  it("does not auto-populate from any external source", () => {
    // The strip renders empty when no params are passed — it does NOT
    // pull from any prop like `paramBlock` or auto-generate sliders.
    const onToggle = vi.fn();
    render(<PinnedParamStrip params={[]} onToggle={onToggle} />);
    // No range inputs (sliders) rendered
    expect(document.querySelectorAll("input[type='range']")).toHaveLength(0);
  });
});
