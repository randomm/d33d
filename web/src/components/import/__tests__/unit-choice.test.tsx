/**
 * UnitChoice — the `isPartUnit` closed-set guard (issue #350 fix round).
 *
 * The option rows render only units in the closed mm/cm/inch set; a row
 * whose unit is outside it is a contract violation the UI must not show.
 */

import { describe, it, expect, vi } from "vitest";
import { render, screen } from "@testing-library/react";
import { isPartUnit, UnitChoice } from "../UnitChoice";
import type { PartOption } from "../../../lib/api";

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

  it("renders only in-set option rows and drops an out-of-set unit (wire-shape fixture)", () => {
    // Mirrors the wire: the payload arrives as parsed JSON and is assigned
    // straight to `PartOption[]` — the only narrowing the codebase does at
    // the boundary. The "custom" option is out of the closed set and must
    // not render.
    const options: PartOption[] = JSON.parse(
      JSON.stringify([
        { unit: "mm", scale: 1, extents_mm: [10, 10, 10], fits_envelope: true, at_least_5mm: true },
        { unit: "custom", scale: 1, extents_mm: [10, 10, 10], fits_envelope: true, at_least_5mm: true },
      ]),
    ) as PartOption[];
    render(
      <UnitChoice
        testIdPrefix="import-report"
        options={options}
        escapeAxis="W"
        onEscapeAxis={vi.fn()}
        escapeMm=""
        onEscapeMm={vi.fn()}
        onSettleUnit={vi.fn()}
        onSettleAxis={vi.fn()}
        busy={false}
      />,
    );
    expect(screen.getByTestId("import-report-option-mm")).toBeTruthy();
    expect(screen.queryByTestId("import-report-option-custom")).toBeNull();
  });
});
