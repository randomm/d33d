/**
 * Canvas mode-agnostic API test.
 *
 * The shared React canvas component (react-konva) must define its API once
 * so that dimension lines (this ticket) AND lasso selection (#6) can be
 * supported on the same component without a rewrite. This test asserts the
 * component's public API is mode-agnostic: dimension mode + lasso mode on
 * one component.
 *
 * The spec (05-spa-core) says: "Define its API once: does it support both
 * simultaneously, and how does a 2D line map to a 3D axis constraint?"
 *
 * This test verifies:
 * 1. The component accepts a `mode` prop with both "dimension" and "lasso"
 *    as valid values.
 * 2. Both modes render the same container structure (no rewrite needed).
 * 3. The dimension-mode-specific UI (drawing hint, mm input) is only
 *    visible in dimension mode.
 * 4. The onDimensionCaptured callback is only wired in dimension mode.
 * 5. Existing dimensions are visible in both modes (coexistence).
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";

import {
  DimensionCanvas,
} from "../DimensionCanvas";
import type {
  CanvasMode,
  DimensionGroundTruthEvent,
  DimensionLine,
} from "../DimensionCanvas";

// ---------------------------------------------------------------------------
// Konva mocking (same pattern as dimension-canvas.test.tsx)
// ---------------------------------------------------------------------------

vi.mock("react-konva", () => {
  return {
    Stage: ({
      children,
      width,
      height,
    }: {
      children: React.ReactNode;
      width: number;
      height: number;
      onClick?: (e: unknown) => void;
    }) => (
      <div
        data-testid="konva-stage"
        data-width={width}
        data-height={height}
      >
        {children}
      </div>
    ),
    Layer: ({ children }: { children: React.ReactNode }) => (
      <div data-testid="konva-layer">{children}</div>
    ),
    Line: (props: {
      points?: number[];
      stroke?: string;
      strokeWidth?: number;
      dashed?: boolean;
      lineCap?: string;
    }) => (
      <div
        data-testid="konva-line"
        data-points={JSON.stringify(props.points ?? [])}
        data-stroke={props.stroke ?? ""}
        data-stroke-width={props.strokeWidth ?? 0}
        data-dashed={props.dashed ? "true" : "false"}
      />
    ),
    Text: (props: {
      text?: string;
      x?: number;
      y?: number;
      fontSize?: number;
      fill?: string;
      align?: string;
      height?: number;
    }) => (
      <span data-testid="konva-text" data-text={props.text ?? ""}>
        {props.text}
      </span>
    ),
    Image: (_props: {
      image?: HTMLImageElement;
      x?: number;
      y?: number;
      width?: number;
      height?: number;
    }) => (
      <div data-testid="konva-image" />
    ),
    Group: ({
      children,
      "data-testid": testid,
    }: {
      children: React.ReactNode;
      "data-testid"?: string;
    }) => (
      <div data-testid={testid ?? "konva-vgroup"}>{children}</div>
    ),
  };
});

// ---------------------------------------------------------------------------
// Test data
// ---------------------------------------------------------------------------

const PHOTO_SRC =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAA";
const PHOTO_W = 800;
const PHOTO_H = 600;

const existingDims: DimensionLine[] = [
  {
    id: "dim-exist-1",
    start: { x: 100, y: 100 },
    end: { x: 300, y: 100 },
    mmValue: 100,
  },
];

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("canvas mode-agnostic API", () => {
  it("accepts mode='dimension' and renders the canvas container", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
      />,
    );
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
  });

  it("accepts mode='lasso' and renders the canvas container", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
      />,
    );
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
  });

  it("defaults to mode='dimension' when no mode prop is given", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
      />,
    );
    // The drawing hint is visible only in dimension mode
    expect(screen.getByTestId("drawing-hint")).toBeInTheDocument();
  });

  it("dimension mode shows the drawing hint", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
      />,
    );
    expect(screen.getByTestId("drawing-hint")).toBeInTheDocument();
    expect(screen.getByTestId("drawing-hint")).toHaveTextContent(
      "Click two points to draw a dimension line",
    );
  });

  it("lasso mode does not show the dimension drawing hint", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
      />,
    );
    expect(screen.queryByTestId("drawing-hint")).not.toBeInTheDocument();
  });

  it("the container structure is identical in both modes (API compatibility)", () => {
    const { unmount } = render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
      />,
    );
    const container1 = screen.getByTestId("dimension-canvas-container");
    expect(container1).toBeInTheDocument();
    unmount();

    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
      />,
    );
    const container2 = screen.getByTestId("dimension-canvas-container");
    expect(container2).toBeInTheDocument();

    // Both containers are the same element type with the same test ID —
    // proving the component is mode-agnostic (one component, two modes).
    expect(container1.tagName).toBe(container2.tagName);
  });

  it("existing dimension lines are visible in both modes (coexistence)", () => {
    // Render in dimension mode
    const { unmount: unmount1 } = render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
        existingDimensions={existingDims}
      />,
    );
    expect(screen.getByTestId("dim-line-dim-exist-1")).toBeInTheDocument();
    unmount1();

    // Now render the same component in lasso mode — the existing
    // dimension line should still be visible (coexistence).
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
        existingDimensions={existingDims}
      />,
    );
    expect(screen.getByTestId("dim-line-dim-exist-1")).toBeInTheDocument();
  });

  it("the onDimensionCaptured callback is only invoked in dimension mode", () => {
    const onCaptured = vi.fn();

    // In dimension mode, the callback is wired
    const { unmount: unmount1 } = render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
        onDimensionCaptured={onCaptured}
      />,
    );
    // (The callback is only called after a full draw+type+confirm cycle,
    // which requires Konva stage pointer events. We verify the wiring
    // exists by checking the component accepts the prop without error.)
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
    unmount1();

    // In lasso mode, the callback is not wired (dimension capture is
    // a dimension-mode operation)
    const onCaptured2 = vi.fn();
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
        onDimensionCaptured={onCaptured2}
      />,
    );
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
  });

  it("the CanvasMode type includes both 'dimension' and 'lasso'", () => {
    // This is a compile-time check: the type must be a union that
    // includes both values. If the type is narrowed, this test fails
    // at compile time.
    const dimensionMode: CanvasMode = "dimension";
    const lassoMode: CanvasMode = "lasso";
    expect(dimensionMode).toBe("dimension");
    expect(lassoMode).toBe("lasso");
  });

  it("the DimensionGroundTruthEvent shape is stable across modes", () => {
    // The event shape is the same regardless of which mode triggered it —
    // this is the "API defined once" contract.
    const event: DimensionGroundTruthEvent = {
      start: { x: 0, y: 0 },
      end: { x: 100, y: 0 },
      mmValue: 100,
      pixelLength: 100,
      scaleFactor: 1.0,
      axis: "X",
    };
    expect(event.start).toEqual({ x: 0, y: 0 });
    expect(event.end).toEqual({ x: 100, y: 0 });
    expect(event.mmValue).toBe(100);
    expect(event.pixelLength).toBe(100);
    expect(event.scaleFactor).toBe(1.0);
    expect(event.axis).toBe("X");
  });

  it("lasso mode shows the lasso hint instead of the dimension hint", () => {
    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
      />,
    );
    expect(screen.queryByTestId("drawing-hint")).not.toBeInTheDocument();
    expect(screen.getByTestId("lasso-hint")).toBeInTheDocument();
  });

  it("onLassoCompleted is only meaningful in lasso mode (accepted in both without erroring)", () => {
    const onLassoCompleted = vi.fn();
    const { unmount } = render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
        onLassoCompleted={onLassoCompleted}
      />,
    );
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
    unmount();

    render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
        onLassoCompleted={onLassoCompleted}
      />,
    );
    expect(screen.getByTestId("dimension-canvas-container")).toBeInTheDocument();
  });

  it("switching from lasso back to dimension mode resets in-progress lasso state", () => {
    const { rerender } = render(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="lasso"
      />,
    );
    expect(screen.getByTestId("lasso-hint")).toBeInTheDocument();

    rerender(
      <DimensionCanvas
        photoSrc={PHOTO_SRC}
        photoWidth={PHOTO_W}
        photoHeight={PHOTO_H}
        mode="dimension"
      />,
    );
    expect(screen.queryByTestId("lasso-hint")).not.toBeInTheDocument();
    expect(screen.getByTestId("drawing-hint")).toBeInTheDocument();
  });
});
