/**
 * Marked-PNG compositing helper (issue #29, re-based for #98).
 *
 * Composites the live ModelViewer canvas with a RED high-contrast marker
 * at the picked point (issue #98), producing the `marked_png_base64`
 * payload `RegionEditRequest` requires. The marker colour is the mandated
 * red/high-contrast-warm (arXiv 2512.17875: VLMs are marker-colour-fragile)
 * — the same red the DOM pick marker renders with, so the sent image
 * shows exactly what the user pointed at.
 *
 * This module owns ONLY the compositing step: given the viewer's rendered
 * canvas element and the picked point (viewport CSS-pixel space), it draws
 * the marker onto a copy of the canvas and returns the resulting PNG as
 * base64 — no `data:` prefix, matching `RegionEditRequest.marked_png_base64`
 * and the backend's `base64.b64decode(..., validate=True)` contract (a
 * `data:` prefix would fail to decode).
 */

import { MARKER_COLOR } from "../components/chat/ChatPanel";

/** A 2-D point in viewport CSS-pixel coordinates (matches ModelViewer's
 *  `ScreenPoint` — the same space the pick layer records via
 *  `getBoundingClientRect()`). */
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
 * Composite a single picked point (a filled red marker with a white halo)
 * onto a copy of the source canvas, returning the resulting PNG as base64
 * with no `data:` URL prefix.
 *
 * `point` is in CSS-pixel viewport space (the same space the pick layer
 * records, via `getBoundingClientRect()`) — NOT the WebGL drawing-buffer's
 * pixel space. `sourceCanvas.width/height` are the drawing-buffer
 * dimensions, i.e. the CSS size scaled by `renderer.getPixelRatio()` (see
 * `WebGLRenderer.setSize`). On any display with `devicePixelRatio !== 1`
 * those two spaces differ, so the point must be scaled by
 * `drawing-buffer / css` before being drawn onto the (drawing-buffer-sized)
 * output canvas — otherwise the marker lands at a fraction of its intended
 * offset from the origin. Callers must pass the CSS-pixel size of the
 * source canvas (e.g. `renderer.getSize(new Vector2())`).
 */
export function compositeMarkedPng(
  sourceCanvas: HTMLCanvasElement,
  point: MarkedPngPoint,
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

  // Copy the source frame. Draw at EXPLICIT source+dest sizes: on a DPR>1
  // display `sourceCanvas.width/height` are the (larger) drawing-buffer
  // dimensions, so a zero-arg drawImage would scale the frame up and blow
  // the marker off the image. The explicit sizes copy buffer→buffer 1:1,
  // which is the identity when the source IS already buffer-sized (DPR=1)
  // and the correct downscale when it isn't.
  ctx.drawImage(sourceCanvas, 0, 0, sourceCanvas.width, sourceCanvas.height, 0, 0, width, height);

  // Draw the marker in the mandated red/high-contrast-warm colour, scaled
  // from CSS pixels into drawing-buffer pixels (see the DPR note above).
  // A white halo ring makes the red read against any model colour.
  const px = point.x * scaleX;
  const py = point.y * scaleY;
  const R = 8;
  ctx.beginPath();
  ctx.arc(px, py, R, 0, Math.PI * 2);
  ctx.fillStyle = "#FFFFFF";
  ctx.fill();
  ctx.beginPath();
  ctx.arc(px, py, R * 0.625, 0, Math.PI * 2);
  ctx.fillStyle = MARKER_COLOR;
  ctx.fill();

  const dataUrl = out.toDataURL("image/png");
  return stripDataUrlPrefix(dataUrl);
}
