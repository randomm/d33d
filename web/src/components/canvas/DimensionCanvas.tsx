/**
 * DimensionCanvas — shared react-konva canvas for dimension capture and
 * lasso region selection (issue #6, workstream task-b).
 *
 * Implements the Fusion 360 attached-canvas pattern for dimension mode: the
 * user draws a 2-D segment between two features on the reference photo,
 * types the real mm value, and the whole frame is scaled from that single
 * anchor.
 *
 * In lasso mode, the user clicks to place polygon vertices on one of the
 * six orthographic views, closes the polygon (double-click, Enter, or
 * clicking near the first point), and the closed polygon is emitted as a
 * `LassoRegionEvent` in photo-pixel coordinates plus the view id — pixel
 * coordinates only, never module identifiers (that resolution happens in
 * ModelViewer.resolveLassoSelection against the loaded per-module meshes).
 *
 * Mode-agnostic API
 * -----------------
 * The component's public API is mode-agnostic: it accepts a `mode` prop
 * (`"dimension" | "lasso"`) so ticket #6 switches the same component to
 * lasso selection without a rewrite. Dimension lines and lasso regions can
 * coexist on one canvas — the mode determines what the user is *drawing*;
 * existing annotations from other modes remain visible and interactive.
 *
 * 2-D → 3-D axis mapping (dimension mode)
 * -----------------------------------------
 * A drawn segment maps to a 3-D axis constraint by its dominant
 * direction in the photo's 2-D plane:
 *
 * - A segment whose horizontal extent ≥ vertical extent → maps to the
 *   **X** axis (width) of the photo frame.
 * - A segment whose vertical extent > horizontal extent → maps to the
 *   **Y** axis (height) of the photo frame.
 *
 * This is the "one line → one axis" convention the spec asks the canvas
 * API to pin down. The derived `scaleFactor` (mm per photo-pixel) is
 * the typed mm value divided by the segment's pixel length, so one
 * anchor line scales the entire frame — exactly the Fusion 360 pattern.
 *
 * The emitted event carries the 2-D endpoints (photo pixel coords), the
 * typed mm value, the derived scale factor, and the inferred 3-D axis —
 * giving the design loop an unambiguous ground-truth scale anchor.
 *
 * Lasso markers (red/warm requirement)
 * -------------------------------------
 * The lasso outline reuses the same red stroke (`#FF3300`) already used
 * for dimension lines — VLM marker-fragility evidence (arXiv 2512.17875)
 * requires selection markers to be red or high-contrast warm; a restyle to
 * blue/green must not silently pass review, so `LASSO_STROKE_COLOR` is a
 * named, tested constant rather than an inline literal.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import { Stage, Layer, Line, Text, Image as KImage, Group } from "react-konva";
import type Konva from "konva";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

/** Canvas interaction mode. Dimension lines shipped in ticket #5;
 *  lasso mode (region selection) ships in this ticket (#6). */
export type CanvasMode = "dimension" | "lasso";

/** Which of the six orthographic render-worker views a lasso was drawn
 *  on. Selection is per-view: the same physical feature may be lassoed
 *  from any view that shows it clearly. */
export type ViewId =
  | "front"
  | "back"
  | "left"
  | "right"
  | "top"
  | "iso";

/** A 2-D point in photo pixel coordinates. */
export interface PhotoPoint {
  x: number;
  y: number;
}

/** A captured dimension line: two endpoints in photo pixel space + mm. */
export interface DimensionLine {
  id: string;
  start: PhotoPoint;
  end: PhotoPoint;
  /** The real-world length of this segment in millimetres. */
  mmValue: number;
}

/** The axis a dimension line maps to in the 3-D frame. */
export type AxisMapping = "X" | "Y";

/**
 * A completed lasso region: a closed polygon in photo-pixel coordinates,
 * on a specific view. This carries pixel coordinates ONLY — resolving the
 * polygon to named OpenSCAD module identifiers is ModelViewer's job
 * (`resolveLassoSelection`), never done here. Sending pixel coordinates
 * to the backend instead of module identifiers would defeat the whole
 * point of the ticket, so this event is explicitly not the edit request.
 */
export interface LassoRegionEvent {
  /** Closed polygon vertices in photo pixel coordinates. At least 3
   *  points (a degenerate 1- or 2-point "polygon" is rejected before
   *  this event is built). */
  points: PhotoPoint[];
  /** Which of the six views this polygon was drawn on. */
  viewId: ViewId;
}

/** The ground-truth scale event emitted to the design loop. */
export interface DimensionGroundTruthEvent {
  /** The two endpoints in photo pixel coordinates. */
  start: PhotoPoint;
  end: PhotoPoint;
  /** The real-world length in mm (the value the user typed). */
  mmValue: number;
  /** Pixel length of the drawn segment. */
  pixelLength: number;
  /** mm per photo pixel — the frame scale factor derived from this line. */
  scaleFactor: number;
  /** Which 3-D axis this line maps to (dominant-direction convention). */
  axis: AxisMapping;
}

/** Props for the DimensionCanvas component. */
export interface DimensionCanvasProps {
  /** The reference photo to overlay on. Must be a valid <img> src. */
  photoSrc: string;
  /** Natural width of the photo in pixels (for coordinate mapping). */
  photoWidth: number;
  /** Natural height of the photo in pixels (for coordinate mapping). */
  photoHeight: number;
  /** The interaction mode. Default "dimension". */
  mode?: CanvasMode;
  /** Callback invoked when the user completes a dimension capture:
   *  draw segment → type mm → confirm. The event carries the ground-truth
   *  scale anchor for the design loop. */
  onDimensionCaptured?: (event: DimensionGroundTruthEvent) => void;
  /** Optional: existing dimension lines to display (read-only overlay). */
  existingDimensions?: DimensionLine[];
  /** Which view this canvas instance is showing. Required in lasso mode
   *  so the emitted `LassoRegionEvent` carries the correct view id;
   *  ignored in dimension mode. Default "front". */
  viewId?: ViewId;
  /** Callback invoked when the user closes a lasso polygon (double-click,
   *  Enter, or clicking near the first vertex). Only wired in lasso mode. */
  onLassoCompleted?: (event: LassoRegionEvent) => void;
  /** Width of the canvas container in CSS pixels. Default 400. */
  width?: number;
  /** Height of the canvas container in CSS pixels. Default 300. */
  height?: number;
  /** Accessible label for the canvas. */
  "aria-label"?: string;
}

// ---------------------------------------------------------------------------
// Internal state
// ---------------------------------------------------------------------------

type DrawState =
  | { phase: "idle" }
  | { phase: "drawing-start"; start: PhotoPoint }
  | { phase: "drawing-end"; start: PhotoPoint; end: PhotoPoint }
  | { phase: "typing-mm"; start: PhotoPoint; end: PhotoPoint }
  | { phase: "done" };

type LassoDrawState =
  | { phase: "idle" }
  | { phase: "drawing"; points: PhotoPoint[] }
  | { phase: "closed"; points: PhotoPoint[] };

/** Distance (in *photo pixel* space) within which a click near the first
 *  vertex closes the polygon, mirroring common lasso-tool UX. */
const CLOSE_POLYGON_THRESHOLD_PX = 12;

/** Minimum vertex count for a valid (non-degenerate) polygon. */
const MIN_POLYGON_POINTS = 3;

/** Lasso outline colour. Reuses the dimension-line red so there is one
 *  red/warm marker convention across the whole canvas — VLM
 *  marker-fragility evidence (arXiv 2512.17875) requires markers to be
 *  red or high-contrast warm; a restyle to blue/green must not silently
 *  pass review, hence a named exported constant instead of an inline
 *  literal. */
export const LASSO_STROKE_COLOR = "#FF3300";

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Compute pixel length of a segment. */
export function pixelLength(a: PhotoPoint, b: PhotoPoint): number {
  const dx = b.x - a.x;
  const dy = b.y - a.y;
  return Math.sqrt(dx * dx + dy * dy);
}

/** Map a 2-D segment to a 3-D axis using the dominant-direction convention.
 *  Horizontal extent >= vertical extent → X; otherwise → Y. */
export function mapToAxis(start: PhotoPoint, end: PhotoPoint): AxisMapping {
  const hExtent = Math.abs(end.x - start.x);
  const vExtent = Math.abs(end.y - start.y);
  return hExtent >= vExtent ? "X" : "Y";
}

/** Derive the scale factor (mm per pixel) from a typed mm value and
 *  the segment's pixel length. Returns 0 for degenerate (zero-length)
 *  segments — the caller must reject these. */
export function deriveScaleFactor(mmValue: number, pxLen: number): number {
  if (pxLen <= 0) return 0;
  return mmValue / pxLen;
}

/** Validate a typed mm value: must be a positive finite number. */
export function isValidMm(value: string): boolean {
  const n = Number.parseFloat(value);
  return Number.isFinite(n) && n > 0;
}

/** Build the ground-truth event from a completed dimension capture. */
export function buildGroundTruthEvent(
  start: PhotoPoint,
  end: PhotoPoint,
  mmValue: number,
): DimensionGroundTruthEvent {
  const pxLen = pixelLength(start, end);
  return {
    start,
    end,
    mmValue,
    pixelLength: pxLen,
    scaleFactor: deriveScaleFactor(mmValue, pxLen),
    axis: mapToAxis(start, end),
  };
}

/** A polygon with fewer than 3 vertices cannot enclose an area — reject it
 *  rather than emitting a degenerate lasso selection. */
export function isValidPolygon(points: PhotoPoint[]): boolean {
  return points.length >= MIN_POLYGON_POINTS;
}

/** True if `point` is within `thresholdPx` of `target` — used to detect a
 *  click near the first vertex that should close the polygon. */
export function isNearPoint(
  point: PhotoPoint,
  target: PhotoPoint,
  thresholdPx: number = CLOSE_POLYGON_THRESHOLD_PX,
): boolean {
  return pixelLength(point, target) <= thresholdPx;
}

/** Build the LassoRegionEvent from a closed polygon. Throws if the
 *  polygon is degenerate (fewer than 3 points) — callers must validate
 *  with `isValidPolygon` first; this function is the single source of
 *  truth for what a valid emitted event looks like. */
export function buildLassoRegionEvent(
  points: PhotoPoint[],
  viewId: ViewId,
): LassoRegionEvent {
  if (!isValidPolygon(points)) {
    throw new Error(
      `lasso polygon needs at least ${MIN_POLYGON_POINTS} points, got ${points.length}`,
    );
  }
  return { points, viewId };
}

// ---------------------------------------------------------------------------
// Component
// ---------------------------------------------------------------------------

export function DimensionCanvas({
  photoSrc,
  photoWidth,
  photoHeight,
  mode = "dimension",
  onDimensionCaptured,
  existingDimensions = [],
  viewId = "front",
  onLassoCompleted,
  width = 400,
  height = 300,
  "aria-label": ariaLabel = "dimension canvas",
}: DimensionCanvasProps) {
  // Konva image ref for the reference photo
  const imageRef = useRef<HTMLImageElement | null>(null);
  const [image, setImage] = useState<HTMLImageElement | null>(null);

  // Drawing state
  const [draw, setDraw] = useState<DrawState>({ phase: "idle" });
  const [mmInput, setMmInput] = useState("");
  const [mmError, setMmError] = useState<string | null>(null);

  // Lasso drawing state
  const [lasso, setLasso] = useState<LassoDrawState>({ phase: "idle" });

  // Scale the photo to fit the canvas container while preserving aspect
  const scale = Math.min(width / photoWidth, height / photoHeight);
  const displayWidth = photoWidth * scale;
  const displayHeight = photoHeight * scale;

  // Load the reference photo
  useEffect(() => {
    const img = new window.Image();
    img.src = photoSrc;
    img.onload = () => {
      imageRef.current = img;
      setImage(img);
    };
    return () => {
      img.src = "";
    };
  }, [photoSrc]);

  // Convert a Konva stage click point to photo pixel coordinates
  const stageToPhoto = useCallback(
    (stageX: number, stageY: number): PhotoPoint => {
      // The photo is centred in the canvas; offset = (canvasSize - displaySize) / 2
      const offsetX = (width - displayWidth) / 2;
      const offsetY = (height - displayHeight) / 2;
      // Convert from display coords to photo pixel coords (divide by scale)
      const photoX = (stageX - offsetX) / scale;
      const photoY = (stageY - offsetY) / scale;
      // Clamp to photo bounds
      return {
        x: Math.max(0, Math.min(photoWidth, photoX)),
        y: Math.max(0, Math.min(photoHeight, photoY)),
      };
    },
    [width, height, photoWidth, photoHeight, scale, displayWidth, displayHeight],
  );

  const handleStageClick = useCallback(
    (e: Konva.KonvaEventObject<MouseEvent>) => {
      const target = e.target;
      // Only act on clicks on the background (the photo image or the stage)
      // — not on already-drawn annotations or the mm input
      const stage = target.getStage();
      const isBackground =
        target === stage ||
        target.getClassName() === "Image" ||
        target.getClassName() === "Rect";

      if (!isBackground) return;

      const point = stageToPhoto(target.x(), target.y());

      if (mode === "lasso") {
        setLasso((prev) => {
          if (prev.phase === "idle" || prev.phase === "closed") {
            return { phase: "drawing", points: [point] };
          }
          if (prev.phase === "drawing") {
            // Clicking near the first vertex closes the polygon (common
            // lasso-tool UX), mirroring the Enter-key close path below.
            const first = prev.points[0];
            if (
              prev.points.length >= MIN_POLYGON_POINTS &&
              first &&
              isNearPoint(point, first)
            ) {
              const closedPoints = prev.points;
              onLassoCompleted?.(buildLassoRegionEvent(closedPoints, viewId));
              return { phase: "closed", points: closedPoints };
            }
            return { phase: "drawing", points: [...prev.points, point] };
          }
          return prev;
        });
        return;
      }

      if (mode !== "dimension") return;

      setMmError(null);

      if (draw.phase === "idle" || draw.phase === "done") {
        setDraw({ phase: "drawing-start", start: point });
      } else if (draw.phase === "drawing-start") {
        setDraw({ phase: "drawing-end", start: draw.start, end: point });
      }
    },
    [mode, draw, stageToPhoto, viewId, onLassoCompleted],
  );

  /** Close the in-progress polygon explicitly (double-click or Enter). */
  const handleLassoClose = useCallback(() => {
    setLasso((prev) => {
      if (prev.phase !== "drawing" || !isValidPolygon(prev.points)) {
        return prev;
      }
      onLassoCompleted?.(buildLassoRegionEvent(prev.points, viewId));
      return { phase: "closed", points: prev.points };
    });
  }, [viewId, onLassoCompleted]);

  const handleLassoCancel = useCallback(() => {
    setLasso({ phase: "idle" });
  }, []);

  const handleLassoKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLDivElement>) => {
      if (mode !== "lasso") return;
      if (e.key === "Enter") handleLassoClose();
      if (e.key === "Escape") handleLassoCancel();
    },
    [mode, handleLassoClose, handleLassoCancel],
  );

  // MM input handling
  const handleMmChange = useCallback((e: React.ChangeEvent<HTMLInputElement>) => {
    setMmInput(e.target.value);
    setMmError(null);
  }, []);

  const handleMmConfirm = useCallback(() => {
    if (draw.phase !== "typing-mm") return;
    if (!isValidMm(mmInput)) {
      setMmError("Enter a positive mm value");
      return;
    }
    const pxLen = pixelLength(draw.start, draw.end);
    if (pxLen <= 0) {
      setMmError("Degenerate segment — try drawing a longer line");
      return;
    }
    const event = buildGroundTruthEvent(draw.start, draw.end, Number.parseFloat(mmInput));
    onDimensionCaptured?.(event);
    setDraw({ phase: "done" });
    setMmInput("");
  }, [draw, mmInput, onDimensionCaptured]);

  const handleMmKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLInputElement>) => {
      if (e.key === "Enter") handleMmConfirm();
      if (e.key === "Escape") {
        setDraw({ phase: "idle" });
        setMmInput("");
        setMmError(null);
      }
    },
    [handleMmConfirm],
  );

  const handleClear = useCallback(() => {
    setDraw({ phase: "idle" });
    setMmInput("");
    setMmError(null);
  }, []);

  // When mode changes, reset drawing state
  useEffect(() => {
    setDraw({ phase: "idle" });
    setMmInput("");
    setMmError(null);
    setLasso({ phase: "idle" });
  }, [mode]);

  // -----------------------------------------------------------------------
  // Render
  // -----------------------------------------------------------------------

  const currentLine =
    draw.phase === "drawing-end" || draw.phase === "typing-mm"
      ? { start: draw.start, end: draw.end }
      : draw.phase === "drawing-start"
        ? { start: draw.start, end: draw.start }
        : null;

  // Photo-pixel polygon points converted to display (stage) coordinates.
  const toStagePoints = useCallback(
    (points: PhotoPoint[]): number[] => {
      const ox = (width - displayWidth) / 2;
      const oy = (height - displayHeight) / 2;
      return points.flatMap((p) => [ox + p.x * scale, oy + p.y * scale]);
    },
    [width, height, displayWidth, displayHeight, scale],
  );

  return (
    <div
      data-testid="dimension-canvas-container"
      style={{ position: "relative", width, height }}
      aria-label={ariaLabel}
      tabIndex={mode === "lasso" ? 0 : undefined}
      onDoubleClick={mode === "lasso" ? handleLassoClose : undefined}
      onKeyDown={mode === "lasso" ? handleLassoKeyDown : undefined}
    >
      {/* Canvas */}
      <Stage
        width={width}
        height={height}
        onClick={handleStageClick}
        data-testid="dimension-canvas-stage"
      >
        <Layer>
          {/* Reference photo */}
          {image && (
            <KImage
              image={image}
              x={(width - displayWidth) / 2}
              y={(height - displayHeight) / 2}
              width={displayWidth}
              height={displayHeight}
              data-testid="reference-photo"
            />
          )}

          {/* Existing dimension lines (read-only overlay) */}
          {existingDimensions.map((d) => {
            const ox = (width - displayWidth) / 2;
            const oy = (height - displayHeight) / 2;
            const pts = [
              ox + d.start.x * scale,
              oy + d.start.y * scale,
              ox + d.end.x * scale,
              oy + d.end.y * scale,
            ];
            return (
              <Group key={d.id} data-testid={`dim-line-${d.id}`}>
                <Line
                  points={pts}
                  stroke="#FF3300"
                  strokeWidth={2}
                  lineCap="round"
                />
                <Text
                  text={`${d.mmValue} mm`}
                  x={(pts[0] + pts[2]) / 2}
                  y={(pts[1] + pts[3]) / 2 - 14}
                  fontSize={12}
                  fill="#FF3300"
                  align="center"
                  height={16}
                />
              </Group>
            );
          })}

          {/* In-progress drawing */}
          {currentLine && (
            <Group data-testid="in-progress-line">
              <Line
                points={[
                  (width - displayWidth) / 2 + currentLine.start.x * scale,
                  (height - displayHeight) / 2 + currentLine.start.y * scale,
                  (width - displayWidth) / 2 + currentLine.end.x * scale,
                  (height - displayHeight) / 2 + currentLine.end.y * scale,
                ]}
                stroke="#00AAFF"
                strokeWidth={2}
                dashed
                lineCap="round"
              />
            </Group>
          )}

          {/* Lasso polygon: in-progress (open, dashed) or closed (filled outline) */}
          {mode === "lasso" && lasso.phase === "drawing" && lasso.points.length > 0 && (
            <Group data-testid="lasso-in-progress">
              <Line
                points={toStagePoints(lasso.points)}
                stroke={LASSO_STROKE_COLOR}
                strokeWidth={2}
                dashed
                lineCap="round"
                lineJoin="round"
                closed={false}
              />
            </Group>
          )}
          {mode === "lasso" && lasso.phase === "closed" && (
            <Group data-testid="lasso-region">
              <Line
                points={toStagePoints(lasso.points)}
                stroke={LASSO_STROKE_COLOR}
                strokeWidth={2}
                lineCap="round"
                lineJoin="round"
                closed
                fill="rgba(255,51,0,0.15)"
              />
            </Group>
          )}
        </Layer>
      </Stage>

      {/* MM input overlay (HTML, not Konva — for a11y) */}
      {draw.phase === "typing-mm" && (
        <div
          data-testid="mm-input-overlay"
          style={{
            position: "absolute",
            top: 8,
            left: 8,
            display: "flex",
            flexDirection: "column",
            gap: 4,
            background: "rgba(255,255,255,0.95)",
            padding: 8,
            borderRadius: 4,
            boxShadow: "0 1px 4px rgba(0,0,0,0.2)",
          }}
        >
          <label htmlFor="mm-input" style={{ fontSize: 12, fontWeight: 600 }}>
            Dimension (mm):
          </label>
          <input
            id="mm-input"
            data-testid="mm-input"
            type="number"
            min="0.01"
            step="0.01"
            value={mmInput}
            onChange={handleMmChange}
            onKeyDown={handleMmKeyDown}
            placeholder="e.g. 42.5"
            style={{
              padding: 4,
              width: 100,
              fontSize: 14,
              border: "1px solid #ccc",
              borderRadius: 3,
            }}
          />
          {mmError && (
            <span data-testid="mm-error" style={{ color: "#c00", fontSize: 12 }}>
              {mmError}
            </span>
          )}
          <div style={{ display: "flex", gap: 4 }}>
            <button
              data-testid="mm-confirm"
              onClick={handleMmConfirm}
              style={{
                padding: "2px 8px",
                fontSize: 12,
                background: "#2563eb",
                color: "white",
                border: "none",
                borderRadius: 3,
                cursor: "pointer",
              }}
            >
              Confirm
            </button>
            <button
              data-testid="mm-cancel"
              onClick={handleClear}
              style={{
                padding: "2px 8px",
                fontSize: 12,
                background: "#6b7280",
                color: "white",
                border: "none",
                borderRadius: 3,
                cursor: "pointer",
              }}
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {/* Drawing hint */}
      {draw.phase === "idle" && mode === "dimension" && (
        <div
          data-testid="drawing-hint"
          style={{
            position: "absolute",
            bottom: 8,
            left: 8,
            fontSize: 11,
            color: "rgba(255,255,255,0.8)",
            background: "rgba(0,0,0,0.5)",
            padding: "2px 6px",
            borderRadius: 3,
          }}
        >
          Click two points to draw a dimension line
        </div>
      )}

      {/* Lasso hint */}
      {mode === "lasso" && lasso.phase !== "closed" && (
        <div
          data-testid="lasso-hint"
          style={{
            position: "absolute",
            bottom: 8,
            left: 8,
            fontSize: 11,
            color: "rgba(255,255,255,0.8)",
            background: "rgba(0,0,0,0.5)",
            padding: "2px 6px",
            borderRadius: 3,
          }}
        >
          {lasso.phase === "drawing"
            ? "Click to add points; double-click or Enter to close, Esc to cancel"
            : "Click to start a lasso region"}
        </div>
      )}

      {/* Lasso empty-selection diagnostic slot: rendered by the parent once
          ModelViewer.resolveLassoSelection returns zero matches for the
          closed polygon (this component only emits the polygon; it does
          not know about the 3-D scene). */}
    </div>
  );
}
