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
 * A polygon with fewer than 3 points is still composited (nothing extra
 * drawn) — callers are expected to have already validated the polygon via
 * `resolveLassoSelection`'s ranked-list gate before reaching this helper;
 * this function does not itself reject degenerate input, since the source
 * canvas capture is valid regardless.
 */
export function compositeMarkedPng(
  sourceCanvas: HTMLCanvasElement,
  polygon: MarkedPngPoint[],
): string {
  const width = sourceCanvas.width;
  const height = sourceCanvas.height;

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
  if (polygon.length >= 2) {
    ctx.beginPath();
    ctx.moveTo(polygon[0].x, polygon[0].y);
    for (let i = 1; i < polygon.length; i++) {
      ctx.lineTo(polygon[i].x, polygon[i].y);
    }
    ctx.closePath();
    ctx.strokeStyle = MARKER_COLOR;
    ctx.lineWidth = 3;
    ctx.stroke();
  }

  const dataUrl = out.toDataURL("image/png");
  return stripDataUrlPrefix(dataUrl);
}
