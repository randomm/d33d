/**
 * Unit tests for ViewportLassoOverlay (issue #29).
 */

import { render, fireEvent, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { ViewportLassoOverlay } from "../ViewportLassoOverlay";

function clickAt(el: Element, x: number, y: number) {
  vi.spyOn(el, "getBoundingClientRect").mockReturnValue({
    top: 0,
    left: 0,
    right: 100,
    bottom: 100,
    width: 100,
    height: 100,
    x: 0,
    y: 0,
    toJSON: () => ({}),
  });
  fireEvent.click(el, { clientX: x, clientY: y });
}

describe("ViewportLassoOverlay", () => {
  it("does not fire onLassoCompleted before a valid (>=3 point) polygon is closed", () => {
    const onLassoCompleted = vi.fn();
    render(<ViewportLassoOverlay viewId="front" onLassoCompleted={onLassoCompleted} />);
    const overlay = screen.getByTestId("viewport-lasso-overlay");

    clickAt(overlay, 10, 10);
    clickAt(overlay, 20, 10);

    expect(onLassoCompleted).not.toHaveBeenCalled();
  });

  it("closes the polygon and fires onLassoCompleted on a click near the first vertex", () => {
    const onLassoCompleted = vi.fn();
    render(<ViewportLassoOverlay viewId="front" onLassoCompleted={onLassoCompleted} />);
    const overlay = screen.getByTestId("viewport-lasso-overlay");

    clickAt(overlay, 10, 10);
    clickAt(overlay, 20, 10);
    clickAt(overlay, 20, 20);
    // Closing click within CLOSE_THRESHOLD_PX of the first vertex (10,10).
    clickAt(overlay, 11, 11);

    expect(onLassoCompleted).toHaveBeenCalledTimes(1);
    const event = onLassoCompleted.mock.calls[0][0];
    expect(event.viewId).toBe("front");
    expect(event.points).toEqual([
      { x: 10, y: 10 },
      { x: 20, y: 10 },
      { x: 20, y: 20 },
    ]);
  });

  it("closes on double-click when >=3 points are placed", () => {
    const onLassoCompleted = vi.fn();
    render(<ViewportLassoOverlay viewId="iso" onLassoCompleted={onLassoCompleted} />);
    const overlay = screen.getByTestId("viewport-lasso-overlay");

    clickAt(overlay, 10, 10);
    clickAt(overlay, 50, 10);
    clickAt(overlay, 50, 50);
    fireEvent.doubleClick(overlay);

    expect(onLassoCompleted).toHaveBeenCalledTimes(1);
    expect(onLassoCompleted.mock.calls[0][0].viewId).toBe("iso");
  });

  it("cancels the in-progress polygon on Escape", () => {
    const onLassoCompleted = vi.fn();
    render(<ViewportLassoOverlay viewId="front" onLassoCompleted={onLassoCompleted} />);
    const overlay = screen.getByTestId("viewport-lasso-overlay");

    clickAt(overlay, 10, 10);
    clickAt(overlay, 20, 10);
    clickAt(overlay, 20, 20);
    fireEvent.keyDown(overlay, { key: "Escape" });
    fireEvent.doubleClick(overlay);

    // Escape cleared the in-progress points, so the subsequent
    // double-click has nothing to close.
    expect(onLassoCompleted).not.toHaveBeenCalled();
  });

  it("ignores clicks when disabled", () => {
    const onLassoCompleted = vi.fn();
    render(
      <ViewportLassoOverlay viewId="front" onLassoCompleted={onLassoCompleted} disabled />,
    );
    const overlay = screen.getByTestId("viewport-lasso-overlay");

    clickAt(overlay, 10, 10);
    clickAt(overlay, 20, 10);
    clickAt(overlay, 20, 20);
    fireEvent.doubleClick(overlay);

    expect(onLassoCompleted).not.toHaveBeenCalled();
  });
});
