/**
 * PickLayer — the single-click region-pick surface over the live three.js
 * viewport (issue #98, replacing the polygon-lasso ViewportLassoOverlay).
 *
 * Interaction model (settled by PM decisions in issue #98):
 *   - A SINGLE CLICK on the model places a marker and fires
 *     `onPointSelected` with the click location in the layer's CSS-pixel
 *     space (the same space `resolvePointPick` raycasts through).
 *   - Click vs drag: pointerdown→pointerup with pointer travel under
 *     `CLICK_MAX_TRAVEL_PX` (4 CSS px, trackpad jitter) is a click; beyond
 *     that the pointer events were NOT stopped, so OrbitControls received
 *     them and the model orbited.
 *   - The layer NEVER consumes wheel/pinch events — they always reach the
 *     camera below, marker or no marker.
 *   - The visible marker is a small red dot rendered here in the DOM at
 *     the click point (the SAME red the marked PNG composites with — the
 *     dot is what the user pointed at). Exactly one marker exists; a new
 *     click replaces it. The parent clears it on cancel/dismiss/submit and
 *     when the camera pose changes (the marker is view-dependent).
 */

import { useCallback, useRef, type PointerEvent } from "react";

import { MARKER_COLOR } from "../../lib/marker";

/** Maximum pointer travel (CSS pixels) between pointerdown and pointerup
 *  for the interaction to count as a SELECTING CLICK rather than a drag.
 *  4px exists because trackpad/trackpad-trackpads report a few pixels of
 *  jitter on a nominally stationary press; a 2px move must still be a
 *  click. Beyond it, the press is a drag and must reach OrbitControls.
 *  Measured in CSS pixels (pointer event coordinates), not drawing-buffer
 *  pixels. */
const CLICK_MAX_TRAVEL_PX = 4;

/** Marker dot diameter in CSS pixels — small enough to read as a point,
 *  large enough to see against the model. */
const MARKER_DOT_PX = 12;

export interface PointSelectedEvent {
  /** The click location in the layer's CSS-pixel space (0,0 = top-left). */
  point: { x: number; y: number };
}

export interface PickLayerProps {
  /** Whether picking is live (a model is loaded). When false the layer
   *  renders (with `data-ready="false"`) but ignores clicks. */
  ready: boolean;
  /** The currently placed marker, or null when none is selected. */
  marker: { x: number; y: number } | null;
  onPointSelected: (event: PointSelectedEvent) => void;
  width?: number;
  height?: number;
}

export function PickLayer({
  ready,
  marker,
  onPointSelected,
  width = 600,
  height = 400,
}: PickLayerProps) {
  // pointerdown position (CSS px, client coords). A ref, not state — it
  // exists only for the pointerup decision and must not re-render.
  const downRef = useRef<{ x: number; y: number } | null>(null);

  const handlePointerDown = useCallback((e: PointerEvent<HTMLDivElement>) => {
    downRef.current = { x: e.clientX, y: e.clientY };
  }, []);

  const handlePointerUp = useCallback(
    (e: PointerEvent<HTMLDivElement>) => {
      const down = downRef.current;
      downRef.current = null;
      if (!ready || !down) return;

      // TRAVEL CHECK (issue #98, red-verified): only a pointerdown→pointerup
      // within CLICK_MAX_TRAVEL_PX is a click; beyond it is a drag that must
      // reach OrbitControls (the layer never stopPropagation). Removing this
      // check makes the "a DRAG does NOT fire" test go RED — see the
      // red-check in the issue #98 test report.
      const dx = e.clientX - down.x;
      const dy = e.clientY - down.y;
      if (Math.sqrt(dx * dx + dy * dy) > CLICK_MAX_TRAVEL_PX) {
        return; // drag — let OrbitControls have it
      }

      const rect = e.currentTarget.getBoundingClientRect();
      onPointSelected({ point: { x: e.clientX - rect.left, y: e.clientY - rect.top } });
    },
    [ready, onPointSelected],
  );

  return (
    <div
      data-testid="viewer-pick-layer"
      data-ready={ready}
      role="presentation"
      aria-label="Click the 3D model to point at a part"
      onPointerDown={handlePointerDown}
      onPointerUp={handlePointerUp}
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        width,
        height,
        cursor: ready ? "crosshair" : "default",
        // The layer must NOT swallow the camera's gestures: pointermove /
        // wheel events are left alone (they reach OrbitControls), and
        // pointerdown/pointerup are NEVER stopPropagation'd, so drags and
        // wheel/pinch always orbit/zoom the model — marker or no marker.
        pointerEvents: ready ? "auto" : "none",
      }}
    >
      {ready && marker && (
        <span
          data-testid="viewer-pick-marker"
          aria-label="selected point marker"
          style={{
            position: "absolute",
            left: marker.x - MARKER_DOT_PX / 2,
            top: marker.y - MARKER_DOT_PX / 2,
            width: MARKER_DOT_PX,
            height: MARKER_DOT_PX,
            borderRadius: "50%",
            // The marker colour is the mandated red/high-contrast warm
            // (AGENTS.md: VLMs are marker-colour-fragile) — the same
            // compositeMarkedPng strokes the sent image with.
            backgroundColor: MARKER_COLOR,
            border: "2px solid #ffffff",
            boxSizing: "border-box",
            pointerEvents: "none",
          }}
        />
      )}
    </div>
  );
}

export default PickLayer;
