/**
 * briefRowHelpers — formatValue unit handling (issue #390).
 *
 * Unitless numeric params (unit: null) render as a bare number: an integer
 * as "6" (not "6.0"), a non-integer as-is. "deg" renders °, "mm" renders
 * mm, and null is never formatted.
 */

import { describe, it, expect } from "vitest";
import { formatValue } from "../briefRowHelpers";

describe("formatValue", () => {
  it("renders a null-unit integer as a bare integer (no decimal)", () => {
    // A count (e.g. bolt_count: 6, unit: null) renders "6", not "6.0".
    expect(formatValue(6, null)).toBe("6");
  });

  it("renders a null-unit non-integer as-is (no decimal padding)", () => {
    expect(formatValue(4.5, null)).toBe("4.5");
  });

  it("renders an integer with a unit suffix unchanged (mm keeps one decimal)", () => {
    expect(formatValue(6, "mm")).toBe("6.0\u202fmm");
  });

  it("renders a deg value with the degree symbol", () => {
    expect(formatValue(20, "deg")).toBe("20.0\u202f°");
  });

  it("returns null for a null value (never a number)", () => {
    expect(formatValue(null, null)).toBeNull();
    expect(formatValue(null, "mm")).toBeNull();
  });
});
