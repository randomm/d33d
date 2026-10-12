/**
 * Issue #437: diameters render with one decimal, half-up, and the
 * point-first copy exists. The shared sample values are the same ones
 * pinned in tests/test_hole_position.py (the Python `fmt_mm1` side).
 */
import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import copy, { dia, fmtMm1 } from "../copy";

describe("fmtMm1 / dia — one decimal, half-up (issue #437)", () => {
  it.each([
    [3.99429, "4.0"],
    [4, "4.0"],
    [3.25, "3.3"],
    [3.35, "3.4"],
    [38, "38.0"],
  ])("fmtMm1(%s) === %s", (value, expected) => {
    expect(fmtMm1(value)).toBe(expected);
  });

  it("dia() renders the same one-decimal value with the unit", () => {
    expect(dia(38)).toBe("Ø38.0\u202Fmm");
    expect(dia(3.25)).toBe("Ø3.3\u202Fmm");
    expect(dia(3.35)).toBe("Ø3.4\u202Fmm");
  });
});

describe("fillRecut.pointFirst (issue #437)", () => {
  it("exists and matches the backend FRILL_POINT_FIRST_REPLY sentence", () => {
    const expected =
      "Point at the hole on the part first, then I'll fill it and cut a new one there.";
    expect(copy.fillRecut.pointFirst).toBe(expected);

    // The backend constant is the same sentence (the parity pin, read
    // from the Python source so the two cannot drift silently).
    const here = dirname(fileURLToPath(import.meta.url));
    const py = readFileSync(
      join(here, "..", "..", "..", "d33d", "fill_recut.py"),
      "utf-8",
    );
    expect(py).toContain(`FRILL_POINT_FIRST_REPLY = (\n    "${expected}"\n)`);
  });
});
