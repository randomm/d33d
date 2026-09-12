/**
 * Unit tests for the marked-PNG compositing helper (issue #29 test surface).
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
    moveTo: ReturnType<typeof vi.fn>;
    lineTo: ReturnType<typeof vi.fn>;
    closePath: ReturnType<typeof vi.fn>;
    stroke: ReturnType<typeof vi.fn>;
    strokeStyle: string;
    lineWidth: number;
  };
  let getContextSpy: ReturnType<typeof vi.fn>;
  let toDataURLSpy: ReturnType<typeof vi.fn>;
  let originalGetContext: typeof HTMLCanvasElement.prototype.getContext;
  let originalToDataURL: typeof HTMLCanvasElement.prototype.toDataURL;

  beforeEach(() => {
    fakeCtx = {
      drawImage: vi.fn(),
      beginPath: vi.fn(),
      moveTo: vi.fn(),
      lineTo: vi.fn(),
      closePath: vi.fn(),
      stroke: vi.fn(),
      strokeStyle: "",
      lineWidth: 0,
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
    const polygon = [
      { x: 1, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 10 },
    ];

    const result = compositeMarkedPng(source, polygon, 100, 80);

    expect(result.length).toBeGreaterThan(0);
    expect(result.startsWith("data:")).toBe(false);
    expect(result).toBe("ZmFrZS1wbmc=");
  });

  it("strokes the polygon in the mandated red marker colour, at DPR=1 (css size == drawing-buffer size)", () => {
    const source = makeSourceCanvas();
    const polygon = [
      { x: 1, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 10 },
    ];

    compositeMarkedPng(source, polygon, 100, 80);

    expect(fakeCtx.strokeStyle).toBe(MARKER_COLOR);
    expect(MARKER_COLOR).toBe("#FF3300");
    expect(fakeCtx.stroke).toHaveBeenCalled();
    expect(fakeCtx.moveTo).toHaveBeenCalledWith(1, 1);
    expect(fakeCtx.lineTo).toHaveBeenCalledWith(10, 1);
    expect(fakeCtx.lineTo).toHaveBeenCalledWith(10, 10);
    expect(fakeCtx.closePath).toHaveBeenCalled();
  });

  it("scales polygon points from CSS-pixel space into drawing-buffer-pixel space when devicePixelRatio > 1", () => {
    // Drawing-buffer canvas is 2x the CSS size, matching
    // renderer.setPixelRatio(2) + renderer.setSize(cssWidth, cssHeight).
    const source = makeSourceCanvas(200, 160);
    const polygon = [
      { x: 1, y: 1 },
      { x: 10, y: 1 },
      { x: 10, y: 10 },
    ];

    compositeMarkedPng(source, polygon, 100, 80);

    expect(fakeCtx.moveTo).toHaveBeenCalledWith(2, 2);
    expect(fakeCtx.lineTo).toHaveBeenCalledWith(20, 2);
    expect(fakeCtx.lineTo).toHaveBeenCalledWith(20, 20);
  });

  it("copies the source canvas frame via drawImage", () => {
    const source = makeSourceCanvas(64, 48);
    compositeMarkedPng(source, [], 64, 48);
    expect(fakeCtx.drawImage).toHaveBeenCalledWith(source, 0, 0, 64, 48);
  });

  it("does not stroke a path for a polygon with fewer than 2 points", () => {
    const source = makeSourceCanvas();
    compositeMarkedPng(source, [{ x: 5, y: 5 }], 100, 80);
    expect(fakeCtx.stroke).not.toHaveBeenCalled();
  });

  it("throws if the 2-D canvas context is unavailable", () => {
    getContextSpy.mockReturnValue(null);
    const source = makeSourceCanvas();
    expect(() => compositeMarkedPng(source, [], 100, 80)).toThrow(
      /2D canvas context unavailable/,
    );
  });
});
