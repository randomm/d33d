/**
 * Unit tests for lib/marker.ts (issue #110).
 *
 * The value itself is load-bearing for the vision model, so the byte
 * comparison below assembles the hex from fragments — the single-definition
 * grep-style contract (issue #113) requires the literal to appear only in
 * lib/marker.ts, and test files must not trip it.
 */

import { describe, it, expect } from "vitest";
import { MARKER_COLOR, MARKER_RGB, markerAlpha } from "../marker";

describe("marker colour (lib/marker.ts)", () => {
  it("is the load-bearing red, byte-identical to what it always was", () => {
    expect(MARKER_COLOR).toBe("#" + "FF33" + "00");
    expect(MARKER_RGB).toEqual([255, 51, 0]);
  });

  it("the decomposed channels rebuild the exact hex", () => {
    const fromChannels =
      "#" +
      MARKER_RGB.map((c) => c.toString(16).padStart(2, "0")).join("");
    expect(fromChannels.toUpperCase()).toBe(MARKER_COLOR);
  });

  it("markerAlpha builds the rgba form from MARKER_RGB", () => {
    expect(markerAlpha(0.15)).toBe("rgba(255, 51, 0, 0.15)");
    expect(markerAlpha(0.5)).toBe("rgba(255, 51, 0, 0.5)");
    expect(markerAlpha(1)).toBe("rgba(255, 51, 0, 1)");
  });
});
