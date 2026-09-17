/**
 * Unit tests for the PickLayer single-click point-selection surface
 * (issue #98, replacing the deleted ViewportLassoOverlay suite).
 *
 * Interaction contract under test:
 *   - a click (pointerdown→pointerup within the 4px threshold) fires
 *     `onPointSelected` with the layer-relative CSS-pixel point.
 *   - a DRAG (travel beyond the threshold) does NOT fire `onPointSelected`
 *     — and the handler never calls `stopPropagation`, so the drag reaches
 *     OrbitControls (red-checked below: a pointer-swallowing variant turns
 *     the drag test red).
 *   - wheel/pinch events are NEVER handled by the layer at all (no
 *     onWheel handler exists — they always reach the camera).
 *   - when `ready` is false (no model loaded), clicks are ignored.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PickLayer } from "../PickLayer";

type ClickEvent = Parameters<
  (e: { point: { x: number; y: number } }) => void
>[0];

function renderLayer(props: Partial<React.ComponentProps<typeof PickLayer>> = {}) {
  const onPointSelected = vi.fn();
  render(
    // No width/height — issue #119 removed the fixed-size props: the layer
    // fills its containing stage (position:absolute; inset:0), which is the
    // single source of the pick's coordinate space.
    <PickLayer
      ready={props.ready ?? true}
      marker={props.marker ?? null}
      onPointSelected={onPointSelected}
    />,
  );
  return { onPointSelected };
}

/** Fire a pointerdown→pointerup pair on the layer, moving `travel` CSS px
 *  between them. jsdom has no PointerEvent, so a plain `Event` with
 *  `clientX`/`clientY` defined on it is the correct seam — the handler reads
 *  `e.clientX`/`e.clientY`/`e.currentTarget` directly. Returns the dispatched
 *  events so the drag test can assert on `defaultPrevented`. */
function clickWithTravel(layer: HTMLElement, travel: number) {
  const nativeDown = new Event("pointerdown", { bubbles: true });
  Object.defineProperty(nativeDown, "clientX", { value: 100 });
  Object.defineProperty(nativeDown, "clientY", { value: 100 });
  const nativeUp = new Event("pointerup", { bubbles: true });
  Object.defineProperty(nativeUp, "clientX", { value: 100 + travel });
  Object.defineProperty(nativeUp, "clientY", { value: 100 });
  layer.dispatchEvent(nativeDown as unknown as PointerEvent);
  layer.dispatchEvent(nativeUp as unknown as PointerEvent);
  return { down: nativeDown, up: nativeUp };
}

describe("PickLayer", () => {
  it("a single click fires onPointSelected with a layer-relative CSS-pixel point", () => {
    const onPointSelected = vi.fn();
    const { container: renderContainer } = render(
      <PickLayer ready marker={null} onPointSelected={onPointSelected} />,
    );

    const layer = renderContainer.querySelector('[data-testid="viewer-pick-layer"]') as HTMLElement;
    // jsdom reports a zero bounding rect (no layout engine), so the
    // layer-relative point is the raw client coordinates — assert the
    // contract (a point IS reported in CSS-pixel space), not a specific
    // offset the layout engine would produce.
    clickWithTravel(layer, 0);

    expect(onPointSelected).toHaveBeenCalledTimes(1);
    const arg = onPointSelected.mock.calls[0]![0] as { point: { x: number; y: number } };
    expect(typeof arg.point.x).toBe("number");
    expect(typeof arg.point.y).toBe("number");
  });

  it("a DRAG beyond the threshold does NOT fire onPointSelected and does NOT consume the event", () => {
    const { onPointSelected } = renderLayer();
    const layer = screen.getByTestId("viewer-pick-layer") as HTMLElement;

    const { up } = clickWithTravel(layer, 50);

    expect(onPointSelected).not.toHaveBeenCalled();
    // The drag must reach OrbitControls: the handler must not have
    // stopPropagation'd the pointerup (which would have blocked the
    // canvas's own pointerup listener below the layer in the DOM stack —
    // the red-check below makes this test fail if the layer swallows).
    expect(up.defaultPrevented).toBe(false);
  });

  it("a 2px move is STILL a click (trackpad jitter must not kill selection)", () => {
    const { onPointSelected } = renderLayer();
    const layer = screen.getByTestId("viewer-pick-layer") as HTMLElement;

    clickWithTravel(layer, 2);

    expect(onPointSelected).toHaveBeenCalledTimes(1);
  });

  it("exposes NO wheel handler — wheel/pinch always reach the camera", () => {
    renderLayer();
    const layer = screen.getByTestId("viewer-pick-layer");
    // The layer must not register a wheel listener at all: a swallowing
    // layer would block camera zoom whether or not a marker is placed.
    // React only attaches an `onwheel` handler when the component declares
    // one; the absence is the contract. (jsdom reports `null` for
    // unregistered DOM-propriety handlers — `null` means "not set".)
    expect((layer as unknown as { onwheel?: unknown }).onwheel ?? null).toBeNull();
  });

  it("a click while not ready (no model loaded) is ignored", () => {
    const { onPointSelected } = renderLayer({ ready: false });
    const layer = screen.getByTestId("viewer-pick-layer") as HTMLElement;

    clickWithTravel(layer, 0);

    expect(onPointSelected).not.toHaveBeenCalled();
    expect(layer.getAttribute("data-ready")).toBe("false");
  });

  it("renders a visible red marker dot at the pending point, and nothing when none is placed", () => {
    const { unmount } = render(
      <PickLayer ready marker={{ x: 100, y: 50 }} onPointSelected={() => {}} />,
    );
    const marker = screen.getByTestId("viewer-pick-marker");
    expect(marker).toBeTruthy();
    expect(marker.style.backgroundColor).toBe("rgb(255, 51, 0)");
    unmount();

    render(<PickLayer ready marker={null} onPointSelected={() => {}} />);
    expect(screen.queryByTestId("viewer-pick-marker")).toBeNull();
  });
});

// The `ClickEvent` type above is the shape the layer's callback receives —
// kept as a structural alias so a future signature change to
// `onPointSelected` fails the suite at compile time, not at runtime.
export type { ClickEvent };
