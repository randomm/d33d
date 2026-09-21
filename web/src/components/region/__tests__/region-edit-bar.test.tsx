/**
 * RegionEditBar tests — the inline instruction bar that follows a point pick
 * (issue #129; extracted from App.tsx by issue #195).
 *
 * Exercises the extracted component directly (unit boundary): the quadrant
 * flip (bar placed opposite the pin), the viewport clamp (and the leader
 * line that flips when the clamp kicks in), the form (submit, cancel,
 * Escape), the module chip, the pose hint, and the cleared hint. All state
 * stays in App — here the bar is driven purely by props.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { RegionEditBar, type RegionEditBarSelection } from "../RegionEditBar";
import copy from "../../../copy";

function makeSelection(overrides: Partial<RegionEditBarSelection> = {}): RegionEditBarSelection {
  return {
    thumbnail: "data:image/png;base64,AAA",
    viewId: "front",
    moduleIds: ["wing_left"],
    point: { x: 100, y: 100 },
    ...overrides,
  };
}

const VIEWPORT = { width: 800, height: 600 };

describe("RegionEditBar", () => {
  it("renders the bar with its copy-driven input placeholder, module chip, and pose hint", () => {
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    expect(bar).toBeTruthy();
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
    const input = screen.getByTestId("region-edit-input");
    expect(input).toHaveAttribute("placeholder", copy.region.placeholder);
    const chip = screen.getByTestId("region-edit-module-chip");
    expect(chip).toHaveTextContent("wing_left");
    expect(chip).toHaveTextContent(copy.region.resolvedTo);
    expect(screen.getByTestId("region-edit-pose-hint")).toHaveTextContent(copy.region.poseHint);
    // No cleared hint while the pin is not cleared.
    expect(screen.queryByTestId("region-edit-cleared-hint")).toBeNull();
  });

  it("renders no module chip when the selection resolved to no module", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ moduleIds: [] })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    expect(screen.queryByTestId("region-edit-module-chip")).toBeNull();
    // The pose hint is always present (it is the bar's standing hint).
    expect(screen.getByTestId("region-edit-pose-hint")).toBeTruthy();
  });

  it("shows the cleared hint while the pin is cleared", () => {
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={true}
      />,
    );
    const hint = screen.getByTestId("region-edit-cleared-hint");
    expect(hint).toHaveTextContent(copy.region.clearedHint);
  });

  it("places the bar in the quadrant OPPOSITE the pin (pin upper-left → bar lower-right)", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ point: { x: 100, y: 100 } })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    // Centre is (400, 300); lower-right → left = 400 + 12, top = 300 + 12.
    expect(bar.style.left).toBe("412px");
    expect(bar.style.top).toBe("312px");
    // Not clamped, so the leader is not flipped.
    expect(bar.getAttribute("data-flipped")).toBe("false");
    expect(screen.getByTestId("region-edit-leader")).toBeTruthy();
  });

  it("places the bar lower-left for a pin in the upper-right quadrant", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ point: { x: 700, y: 100 } })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    // left = 400 - 12 - 320 = 68, top = 300 + 12 = 312.
    expect(bar.style.left).toBe("68px");
    expect(bar.style.top).toBe("312px");
    expect(bar.getAttribute("data-flipped")).toBe("false");
  });

  it("places the bar upper-right for a pin in the lower-left quadrant", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ point: { x: 100, y: 500 } })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    // left = 412, top = 300 - 12 - 120 = 168.
    expect(bar.style.left).toBe("412px");
    expect(bar.style.top).toBe("168px");
  });

  it("clamps the bar to the viewport on a small viewport and flips the leader", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ point: { x: 10, y: 10 } })}
        viewportSize={{ width: 300, height: 200 }}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    // Centre (150, 100): default left = 150 + 12 = 162, but the viewport is
    // only 300 wide → clamp to 300 - 320 - 4 = -24 → clamped at 4.
    // top = 100 + 12 = 112 → clamp: 200 - 120 - 4 = 76 → clamped at 76.
    expect(bar.style.left).toBe("4px");
    expect(bar.style.top).toBe("76px");
    expect(bar.getAttribute("data-flipped")).toBe("true");
  });

  it("submits the form (Enter) with the trimmed instruction", () => {
    const onSubmit = vi.fn();
    const onTextChange = vi.fn();
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={onTextChange}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const input = screen.getByTestId("region-edit-input");
    fireEvent.change(input, { target: { value: "open the top" } });
    expect(onTextChange).toHaveBeenCalledWith("open the top");
    fireEvent.submit(input.closest("form")!);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("enables the Apply button once the input is non-empty and submits on click", () => {
    const onSubmit = vi.fn();
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const apply = screen.getByTestId("region-edit-apply-btn");
    expect(apply).toBeDisabled();

    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="widen it"
        onTextChange={vi.fn()}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    fireEvent.click(screen.getAllByTestId("region-edit-apply-btn").at(-1)!);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });

  it("cancels via the cancel button AND via Escape in the input", () => {
    const onCancel = vi.fn();
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={onCancel}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    fireEvent.click(screen.getByTestId("pending-selection-cancel-btn"));
    expect(onCancel).toHaveBeenCalledTimes(1);

    fireEvent.keyDown(screen.getByTestId("region-edit-input"), { key: "Escape" });
    expect(onCancel).toHaveBeenCalledTimes(2);

    // Other keys do not cancel.
    fireEvent.keyDown(screen.getByTestId("region-edit-input"), { key: "a" });
    expect(onCancel).toHaveBeenCalledTimes(2);
  });

  it("dims the bar while the pin is orbiting", () => {
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={true}
        orbitClearedPin={false}
      />,
    );
    const bar = screen.getByTestId("region-edit-bar");
    expect(bar.style.opacity).toBe("0.5");
  });
});
