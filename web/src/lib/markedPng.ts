/**
 * Marked-PNG compositing helper (issue #29).
 *
 * Composites the live ModelViewer canvas with the lasso selection polygon
 * stroked over it in the mandated red/high-contrast-warm marker colour
 * (arXiv 2512.17875: VLMs are marker-colour-fragile), producing the
 * `marked_png_base64` payload `RegionEditRequest` requires.
 *
 * This module owns ONLY the compositing step: given the viewer's rendered
 * canvas element and the closed lasso polygon (viewport-pixel space, the
 * same space it was drawn in), it draws the polygon outline onto a copy of
 * the canvas and returns the resulting PNG as base64 — no `data:` prefix,
 * matching `RegionEditRequest.marked_png_base64` and the backend's
 * `base64.b64decode(..., validate=True)` contract (a `data:` prefix would
 * fail to decode).
 */

import { MARKER_COLOR } from "../components/chat/ChatPanel";

/** A 2-D point in viewport-pixel coordinates (matches ModelViewer's
 *  `ScreenPoint` — the same space the lasso overlay draws in). */
export interface MarkedPngPoint {
  x: number;
  y: number;
}

/** Strip the `data:image/...;base64,` prefix from a data URL, returning
 *  only the base64 payload — the shape `RegionEditRequest.marked_png_base64`
 *  and the server's `b64decode` expect. */
export function stripDataUrlPrefix(dataUrl: string): string {
  const commaIdx = dataUrl.indexOf(",");
  return commaIdx >= 0 ? dataUrl.slice(commaIdx + 1) : dataUrl;
}

/**
 * Composite the lasso polygon (stroked in the red marker colour) onto a
 * copy of the source canvas, returning the resulting PNG as base64 with
 * no `data:` URL prefix.
 *
 * `polygon` is in CSS-pixel viewport space (the same space
 * `ViewportLassoOverlay` draws in, via `getBoundingClientRect()`) — NOT
 * the WebGL drawing-buffer's pixel space. `sourceCanvas.width/height` are
 * the drawing-buffer dimensions, i.e. the CSS size scaled by
 * `renderer.getPixelRatio()` (see `WebGLRenderer.setSize`). On any display
 * with `devicePixelRatio !== 1` those two spaces differ, so the polygon
 * must be scaled by `drawing-buffer / css` before being stroked onto the
 * (drawing-buffer-sized) output canvas — otherwise the marker lands at a
 * fraction of its intended offset from the origin. Callers must pass the
 * CSS-pixel size of the source canvas (e.g. `renderer.getSize(new
 * Vector2())`, as `App.tsx` already does for the raycast NDC conversion).
 *
 * A polygon with fewer than 3 points is still composited (nothing extra
 * drawn) — callers are expected to have already validated the polygon via
 * `resolveLassoSelection`'s ranked-list gate before reaching this helper;
 * this function does not itself reject degenerate input, since the source
 * canvas capture is valid regardless.
 */
export function compositeMarkedPng(
  sourceCanvas: HTMLCanvasElement,
  polygon: MarkedPngPoint[],
  cssWidth: number,
  cssHeight: number,
): string {
  const width = sourceCanvas.width;
  const height = sourceCanvas.height;
  const scaleX = cssWidth > 0 ? width / cssWidth : 1;
  const scaleY = cssHeight > 0 ? height / cssHeight : 1;

  const out = document.createElement("canvas");
  out.width = width;
  out.height = height;
  const ctx = out.getContext("2d");
  if (!ctx) {
    throw new Error("2D canvas context unavailable — cannot composite marked PNG");
  }

  // Copy the source frame.
  ctx.drawImage(sourceCanvas, 0, 0, width, height);

  // Stroke the lasso polygon in the mandated red/high-contrast-warm
  // marker colour, closing the path so it reads as a region outline.
  // Points are CSS pixels; scale into drawing-buffer pixels to match the
  // canvas this is drawn onto (see the DPR note in the doc comment above).
  if (polygon.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(polygon[0].x * scaleX, polygon[0].y * scaleY);
    for (let i = 1; i < polygon.length; i++) {
      ctx.lineTo(polygon[i].x * scaleX, polygon[i].y * scaleY);
    }
    ctx.closePath();
    ctx.strokeStyle = MARKER_COLOR;
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  const dataUrl = out.toDataURL("image/png");
  return stripDataUrlPrefix(dataUrl);
}
