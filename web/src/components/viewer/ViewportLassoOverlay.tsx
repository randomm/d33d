/**
 * ViewportLassoOverlay — a lasso-drawing surface over the live three.js
 * viewport (issue #29).
 *
 * DimensionCanvas's existing lasso mode draws over a reference PHOTO and
 * emits `PhotoPoint[]` (photo-pixel space, via its own `stageToPhoto`
 * scale/offset transform) — it is not reusable here. Per the settled
 * design decision (issue #29), the region-selection lasso is drawn
 * directly over the rendered 3-D viewport, so its polygon must already be
 * in on-screen viewport-pixel coordinates: the same space
 * `ModelViewer.resolveLassoSelection` raycasts through. This component is
 * therefore new, minimal, and purely a click-to-place polygon capture — no
 * photo, no coordinate transform, no react-konva (a transparent absolutely
 * positioned div is sufficient for capturing clicks against its own
 * bounding rect).
 *
 * Interaction: click to place vertices; double-click, Enter, or clicking
 * within `CLOSE_THRESHOLD_PX` of the first vertex closes the polygon and
 * fires `onLassoCompleted` with the closed point list plus the view id.
 * Escape cancels the in-progress polygon.
 */

import { useCallback, useState, type MouseEvent, type KeyboardEvent } from "react";
import type { ScreenPoint } from "./ModelViewer";
import type { RegionEditViewId } from "../../lib/api";
import { LASSO_STROKE_COLOR } from "../canvas/DimensionCanvas";

/** Distance (viewport pixels) within which a click near the first vertex
 *  closes the polygon — mirrors DimensionCanvas's lasso UX. */
const CLOSE_THRESHOLD_PX = 12;

/** Minimum vertex count for a valid (non-degenerate) polygon. */
const MIN_POLYGON_POINTS = 3;

export interface ViewportLassoCompletedEvent {
  points: ScreenPoint[];
  viewId: RegionEditViewId;
}

export interface ViewportLassoOverlayProps {
  /** Which view this overlay is drawn on (single always-visible 3-D
   *  viewport, so this is a fixed prop rather than six discrete canvases). */
  viewId: RegionEditViewId;
  /** Called when the user closes a valid (>=3 point) polygon. */
  onLassoCompleted: (event: ViewportLassoCompletedEvent) => void;
  /** Whether the overlay accepts input. When false (e.g. no model loaded
   *  yet), the overlay renders but ignores clicks. */
  disabled?: boolean;
  width?: number;
  height?: number;
}

function distance(a: ScreenPoint, b: ScreenPoint): number {
  const dx = a.x - b.x;
  const dy = a.y - b.y;
  return Math.sqrt(dx * dx + dy * dy);
}

export function ViewportLassoOverlay({
  viewId,
  onLassoCompleted,
  disabled = false,
  width = 600,
  height = 400,
}: ViewportLassoOverlayProps) {
  const [points, setPoints] = useState<ScreenPoint[]>([]);

  const closeIfValid = useCallback(
    (pts: ScreenPoint[]) => {
      if (pts.length < MIN_POLYGON_POINTS) return;
      onLassoCompleted({ points: pts, viewId });
      setPoints([]);
    },
    [onLassoCompleted, viewId],
  );

  const handleClick = useCallback(
    (e: MouseEvent<HTMLDivElement>) => {
      if (disabled) return;
      const rect = e.currentTarget.getBoundingClientRect();
      const point: ScreenPoint = {
        x: e.clientX - rect.left,
        y: e.clientY - rect.top,
      };

      setPoints((prev) => {
        if (prev.length >= MIN_POLYGON_POINTS && distance(point, prev[0]) <= CLOSE_THRESHOLD_PX) {
          // Closing click near the first vertex.
          onLassoCompleted({ points: prev, viewId });
          return [];
        }
        return [...prev, point];
      });
    },
    [disabled, onLassoCompleted, viewId],
  );

  const handleDoubleClick = useCallback(() => {
    if (disabled) return;
    closeIfValid(points);
  }, [disabled, points, closeIfValid]);

  const handleKeyDown = useCallback(
    (e: KeyboardEvent<HTMLDivElement>) => {
      if (disabled) return;
      if (e.key === "Enter") closeIfValid(points);
      if (e.key === "Escape") setPoints([]);
    },
    [disabled, points, closeIfValid],
  );

  return (
    <div
      data-testid="viewport-lasso-overlay"
      role="presentation"
      aria-label="Lasso a region of the 3D model"
      tabIndex={disabled ? undefined : 0}
      onClick={handleClick}
      onDoubleClick={handleDoubleClick}
      onKeyDown={handleKeyDown}
      style={{
        position: "absolute",
        top: 0,
        left: 0,
        width,
        height,
        cursor: disabled ? "default" : "crosshair",
        pointerEvents: disabled ? "none" : "auto",
      }}
    >
      {points.length > 0 && (
        <svg
          data-testid="viewport-lasso-in-progress"
          width={width}
          height={height}
          style={{ position: "absolute", top: 0, left: 0, pointerEvents: "none" }}
        >
          <polyline
            points={points.map((p) => `${p.x},${p.y}`).join(" ")}
            fill="none"
            stroke={LASSO_STROKE_COLOR}
            strokeWidth={2}
            strokeDasharray="4 3"
          />
        </svg>
      )}
    </div>
  );
}

export default ViewportLassoOverlay;
