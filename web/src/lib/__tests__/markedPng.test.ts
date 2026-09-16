/**
 * Unit tests for the marked-PNG compositing helper (issue #29, re-based
 * for #98's single-point marker).
 *
 * jsdom has no real 2-D canvas backend (no native `canvas` package
 * installed), so `HTMLCanvasElement.getContext("2d")`/`toDataURL` are
 * stubbed here — this suite verifies `compositeMarkedPng` drives the
 * canvas API correctly and shapes its output correctly, not that a real
 * PNG was rasterized (that's covered by the server's own contract, not
 * this client-side helper's job to prove).
 */

import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { compositeMarkedPng, stripDataUrlPrefix } from "../markedPng";
import { MARKER_COLOR } from "../../components/chat/ChatPanel";

describe("stripDataUrlPrefix", () => {
  it("strips a data:image/png;base64, prefix", () => {
    expect(stripDataUrlPrefix("data:image/png;base64,AAAA")).toBe("AAAA");
  });

  it("returns the input unchanged if there is no comma", () => {
    expect(stripDataUrlPrefix("AAAA")).toBe("AAAA");
  });
});

describe("compositeMarkedPng", () => {
  let fakeCtx: {
    drawImage: ReturnType<typeof vi.fn>;
    beginPath: ReturnType<typeof vi.fn>;
    arc: ReturnType<typeof vi.fn>;
    fill: ReturnType<typeof vi.fn>;
    fillStyle: string;
  };
  let getContextSpy: ReturnType<typeof vi.fn>;
  let toDataURLSpy: ReturnType<typeof vi.fn>;
  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      arc: vi.fn(),
      fill: vi.fn(),
      fillStyle: "",
    };
    originalGetContext = HTMLCanvasElement.prototype.getContext;
    originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
    getContextSpy = vi.fn().mockReturnValue(fakeCtx);
    toDataURLSpy = vi.fn().mockReturnValue("data:image/png;base64,ZmFrZS1wbmc=");
    HTMLCanvasElement.prototype.getContext =
      getContextSpy as unknown as typeof HTMLCanvasElement.prototype.getContext;
    HTMLCanvasElement.prototype.toDataURL =
      toDataURLSpy as unknown as typeof HTMLCanvasElement.prototype.toDataURL;
  });

  afterEach(() => {
    HTMLCanvasElement.prototype.getContext = originalGetContext;
    HTMLCanvasElement.prototype.toDataURL = originalToDataURL;
  });

  function makeSourceCanvas(width = 100, height = 80): HTMLCanvasElement {
    const canvas = document.createElement("canvas");
    canvas.width = width;
    canvas.height = height;
    return canvas;
  }

  it("returns a non-empty base64 payload with no data-URL prefix", () => {
    const source = makeSourceCanvas();

    const result = compositeMarkedPng(source, { x: 1, y: 1 }, 100, 80);

    expect(result.length).toBeGreaterThan(0);
    expect(result.startsWith("data:")).toBe(false);
    expect(result).toBe("ZmFrZS1wbmc=");
  });

  it("draws the marker in the mandated red marker colour, at DPR=1 (css size == drawing-buffer size)", () => {
    const source = makeSourceCanvas();

    compositeMarkedPng(source, { x: 1, y: 1 }, 100, 80);

    // Two arcs are drawn: the white halo ring first, then the red marker
    // on top. The final fillStyle must be the mandated red (the marker
    // body) — the red→white halo→red sequence ends on MARKER_COLOR.
    expect(MARKER_COLOR).toBe("#FF3300");
    expect(fakeCtx.arc).toHaveBeenCalledTimes(2);
    expect(fakeCtx.fill).toHaveBeenCalledTimes(2);
    expect(fakeCtx.fillStyle).toBe(MARKER_COLOR);
    // Both arcs are centred on the clicked point, halo at full radius.
    expect(fakeCtx.arc).toHaveBeenNthCalledWith(1, 1, 1, 8, 0, Math.PI * 2);
    expect(fakeCtx.arc).toHaveBeenNthCalledWith(2, 1, 1, 5, 0, Math.PI * 2);
  });

  it("scales the marker from CSS-pixel space into drawing-buffer-pixel space when devicePixelRatio > 1", () => {
    // Drawing-buffer canvas is 2x the CSS size, matching
    // renderer.setPixelRatio(2) + renderer.setSize(cssWidth, cssHeight).
    const source = makeSourceCanvas(200, 160);

    compositeMarkedPng(source, { x: 10, y: 20 }, 100, 80);

    // The marker centre must be at (20, 40) in buffer pixels — the CSS
    // point (10, 20) scaled by the 2x drawing-buffer ratio.
    expect(fakeCtx.arc).toHaveBeenNthCalledWith(1, 20, 40, 8, 0, Math.PI * 2);
    expect(fakeCtx.arc).toHaveBeenNthCalledWith(2, 20, 40, 5, 0, Math.PI * 2);
  });

  it("copies the source canvas frame via drawImage (explicit source+dest sizes for DPR-safety)", () => {
    const source = makeSourceCanvas(64, 48);
    compositeMarkedPng(source, { x: 0, y: 0 }, 64, 48);
    // 9-arg form: src, sx, sy, sWidth, sHeight, dx, dy, dWidth, dHeight —
    // the explicit sizes keep a DPR>1 buffer from being scaled up (a
    // zero-arg drawImage would copy at source-px size, which is the
    // larger buffer size, not the css size).
    expect(fakeCtx.drawImage).toHaveBeenCalledWith(source, 0, 0, 64, 48, 0, 0, 64, 48);
  });

  it("throws if the 2-D canvas context is unavailable", () => {
    getContextSpy.mockReturnValue(null);
    const source = makeSourceCanvas();
    expect(() => compositeMarkedPng(source, { x: 0, y: 0 }, 100, 80)).toThrow(
      /2D canvas context unavailable/,
    );
  });
});
