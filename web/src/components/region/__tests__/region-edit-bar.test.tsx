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

  it("shows the 'on the part you brought' chip and the mm hit point in mono for an imported-geometry pick (issue #338)", () => {
    render(
      <RegionEditBar
        selection={makeSelection({
          moduleIds: [],
          onImportedPart: true,
          hitPointMm: { x: 12.0, y: 0.0, z: 20.0 },
        })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const chip = screen.getByTestId("region-edit-imported-chip");
    expect(chip).toHaveTextContent(copy.region.onImportedPart);
    // The hit point renders in the mono face, formatted "x 12.0 · y 0.0 · z 20.0 mm".
    const hit = screen.getByTestId("region-edit-hitpoint");
    expect(hit).toHaveTextContent("x 12.0 · y 0.0 · z 20.0 mm");
  });

  it("shows no imported chip or hit point for a non-imported pick (issue #338)", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ moduleIds: ["wing_left"] })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    expect(screen.queryByTestId("region-edit-imported-chip")).toBeNull();
    expect(screen.queryByTestId("region-edit-hitpoint")).toBeNull();
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
    const { unmount } = render(
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
    unmount();
  });
});

// ---------------------------------------------------------------------------
// Issue #388 (operator decision 3) — the region-edit bar is DISABLED, not
// queued, while a design run is in flight
// ---------------------------------------------------------------------------

describe("RegionEditBar — in-flight gating (issue #388)", () => {
  it("disables the Apply button while a run is in flight, and a click on it does NOT fire onSubmit", () => {
    // Decision 3: a region-edit submit is disabled (not queued) while a
    // run is in flight. The Apply button is the closed path: a click on a
    // disabled button is a no-op (the browser swallows the click), so
    // onSubmit is never reached.
    const onSubmit = vi.fn();
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
        inFlight={true}
      />,
    );
    const apply = screen.getByTestId("region-edit-apply-btn");
    expect(apply).toBeDisabled();
    // A click on the disabled button is a no-op — the browser swallows it,
    // and jsdom's fireEvent.click on a disabled button also does not fire
    // the handler.
    fireEvent.click(apply);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("the disabled Apply button exposes the copy.ts reason as tooltip and accessible description (issue #388)", () => {
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="widen it"
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
        inFlight={true}
      />,
    );
    const btn = screen.getByTestId("region-edit-apply-btn");
    expect(btn).toBeDisabled();
    expect(btn).toHaveAttribute("title", copy.shell.disabledReason);
    expect(btn).toHaveAttribute("aria-describedby", "region-edit-apply-disabled-reason");
    // The visually-associated hint is rendered and carries the same text.
    const hint = screen.getByTestId("region-edit-apply-disabled-reason");
    expect(hint).toHaveTextContent(copy.shell.disabledReason);
  });

  it("no disabled-reason hint when not in flight", () => {
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="widen it"
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    expect(screen.queryByTestId("region-edit-apply-disabled-reason")).toBeNull();
    const btn = screen.getByTestId("region-edit-apply-btn");
    expect(btn).not.toHaveAttribute("title");
    expect(btn).not.toHaveAttribute("aria-describedby");
  });

  it("enables the Apply button once the run ends (inFlight is false) and the text is non-empty", () => {
    // The in-flight gate is the only thing disabling Apply when the text is
    // non-empty — once the run ends (inFlight is false) the button is
    // enabled again and the user can re-apply the same instruction.
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="widen it"
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
        inFlight={false}
      />,
    );
    expect(screen.getByTestId("region-edit-apply-btn")).not.toBeDisabled();
  });

  it("a form submit (Enter) is a no-op while a run is in flight — the selection is kept, nothing is queued (issue #388, operator decision 3)", () => {
    const onSubmit = vi.fn();
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="open the top"
        onTextChange={vi.fn()}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
        inFlight={true}
      />,
    );
    const input = screen.getByTestId("region-edit-input");
    fireEvent.submit(input.closest("form")!);
    expect(onSubmit).not.toHaveBeenCalled();
  });

  it("keeps the input enabled and the selection visible while inFlight (the drawn selection is kept)", () => {
    // Decision 3 explicitly says the drawn selection is kept — the bar
    // does not unmount or hide its content. The thumbnail, the module
    // chip, and the input all stay rendered while inFlight is true.
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
        inFlight={true}
      />,
    );
    expect(screen.getByTestId("region-edit-bar")).toBeTruthy();
    expect(screen.getByTestId("pending-selection-thumbnail")).toBeTruthy();
    expect(screen.getByTestId("region-edit-module-chip")).toBeTruthy();
    expect(screen.getByTestId("region-edit-input")).not.toBeDisabled();
  });

  it("submit still works when not in flight (no inFlight)", () => {
    const onSubmit = vi.fn();
    render(
      <RegionEditBar
        selection={makeSelection()}
        viewportSize={VIEWPORT}
        text="open the top"
        onTextChange={vi.fn()}
        onSubmit={onSubmit}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    expect(screen.getByTestId("region-edit-apply-btn")).not.toBeDisabled();
    fireEvent.submit(screen.getByTestId("region-edit-input").closest("form")!);
    expect(onSubmit).toHaveBeenCalledTimes(1);
  });
});


// ---------------------------------------------------------------------------
// Issue #338 (decision 3) — the "on the part you brought" chip + hit point
// ---------------------------------------------------------------------------

describe("RegionEditBar — imported-geometry chip + hit point (issue #338)", () => {
  it("shows the 'on the part you brought' chip for imported-geometry picks", () => {
    render(
      <RegionEditBar
        selection={makeSelection({
          onImportedPart: true,
          hitPointMm: { x: 12.0, y: 0.0, z: 20.0 },
        })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    const chip = screen.getByTestId("region-edit-imported-chip");
    expect(chip.textContent).toBe("on the part you brought");
  });

  it("does NOT show the imported chip for a module pick (non-imported geometry)", () => {
    render(
      <RegionEditBar
        selection={makeSelection({ onImportedPart: false })}
        viewportSize={VIEWPORT}
        text=""
        onTextChange={vi.fn()}
        onSubmit={vi.fn()}
        onCancel={vi.fn()}
        orbitingPin={false}
        orbitClearedPin={false}
      />,
    );
    expect(screen.queryByTestId("region-edit-imported-chip")).toBeNull();
  });
});


