/**
 * UnitChoice — the `isPartUnit` closed-set guard (issue #350 fix round).
 *
 * The option rows render only units in the closed mm/cm/inch set; a row
 * whose unit is outside it is a contract violation the UI must not show.
 */

import { describe, it, expect } from "vitest";
import { isPartUnit } from "../UnitChoice";

describe("isPartUnit — the closed mm/cm/inch option guard", () => {
  it("accepts only the closed mm/cm/inch set and rejects anything else", () => {
    expect(isPartUnit("mm")).toBe(true);
    expect(isPartUnit("cm")).toBe(true);
    expect(isPartUnit("inch")).toBe(true);
    expect(isPartUnit("m")).toBe(false);
    expect(isPartUnit("inches")).toBe(false);
    expect(isPartUnit("")).toBe(false);
    expect(isPartUnit(null)).toBe(false);
    expect(isPartUnit(1)).toBe(false);
  });
});
