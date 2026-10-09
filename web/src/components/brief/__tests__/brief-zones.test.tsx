/**
 * BriefZones — the two-zone split module (issue #338, Screen 3).
 *
 * The zone logic lives in this small module so Brief.tsx does not grow.
 * These tests pin the split: "The part you brought" renders only when the
 * design-state `part` block is present; "Your changes" renders always.
 * The no-part project renders a single merged list (no zone headers),
 * byte-identical to the pre-#338 shape.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Brief } from "../Brief";
import copy from "../../../copy";
import type { DesignStateEntry, PartReportInfo } from "../../../lib/api";

const stated = (name: string, value: number): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value,
  unit: "mm",
  provenance: "stated",
});
const assumed = (name: string, value: number): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value,
  unit: "mm",
  provenance: "assumed",
});
const axisStated = (axis: "W" | "D" | "H", value: number): DesignStateEntry => ({
  name: axis,
  kind: "axis",
  label: axis,
  value,
  unit: "mm",
  provenance: "stated",
});

const baseProps = { isChip: false, inset: 24, conversationCollapsed: false } as const;

/** The settled part fixture — a part with `unit_status: "settled"` and a
 *  `bbox_mm` (the settled-unit mm extents the W/D/H rows read from). */
const settledPart = (): PartReportInfo => ({
  filename: "motor-mount.stl",
  format: "stl",
  unit: "mm",
  unit_status: "settled",
  bbox_mm: [60, 45, 80],
  scale: 1,
  report: {
    triangles: 12,
    bodies: 1,
    watertight: true,
    gaps_closed: 0,
    bbox_file_units: [60, 45, 80],
  },
  options: null,
});

const unsettledPart = (): PartReportInfo => ({
  filename: "motor-mount.stl",
  format: "stl",
  unit: null,
  unit_status: "unsettled",
  bbox_mm: null,
  scale: null,
  report: {
    triangles: 12,
    bodies: 1,
    watertight: true,
    gaps_closed: 0,
    bbox_file_units: [60, 45, 80],
  },
  options: null,
});

/** The assumed part fixture (issue #350) — a part the server read as mm
 *  (unit_status "assumed", scale 1) with an established mm bbox. */
const assumedPart = (): PartReportInfo => ({
  filename: "motor-mount.stl",
  format: "stl",
  unit: "mm",
  unit_status: "assumed",
  bbox_mm: [60, 45, 80],
  scale: 1,
  report: {
    triangles: 12,
    bodies: 1,
    watertight: true,
    gaps_closed: 0,
    bbox_file_units: [60, 45, 80],
  },
  options: null,
});

describe("BriefZones — no part (single list, no zone headers)", () => {
  it("renders the single list with NO zone headers when there is no part", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("W", 60), stated("D", 45), stated("rod_bore", 34)]}
        part={null}
      />,
    );
    // No zone headers at all — the single-list shape is byte-identical to
    // before #338.
    expect(screen.queryByTestId("brief-zone-part-header")).toBeNull();
    expect(screen.queryByTestId("brief-zone-changes-header")).toBeNull();
    // The rows render in the single list.
    expect(screen.getByTestId("brief-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-D")).toBeTruthy();
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
  });
});

describe("BriefZones — part present (two zones)", () => {
  it('"The part you brought" renders when the part block is present', () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34), stated("wall_gap", 8)]}
        part={settledPart()}
      />,
    );
    // The part-zone header is present (the literal is pinned in the
    // design-contract test — assert it here only against copy.ts).
    expect(
      screen.getByTestId("brief-zone-part-header").textContent,
    ).toBe(copy.brief.partBroughtHeader);
    // The three W/D/H part rows are present, in measured provenance.
    expect(screen.getByTestId("brief-part-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-D")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-H")).toBeTruthy();
    // The W/D/H values come from `part.bbox_mm` (settled-unit mm).
    expect(
      screen.getByTestId("brief-part-row-W").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("60.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-D").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("45.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-H").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("80.0\u202Fmm");
    // The note is present (settled part). Pinned against copy.ts AND as a
    // literal string so a plural-map regression is caught here, not just in
    // the design-contract test (issue #390, item 2).
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      copy.brief.partBroughtNote("mm"),
    );
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      "Measured, in millimetres you confirmed. Its own features are fixed — I can add and cut, not resize.",
    );
    // The W/D/H rows carry measured provenance (a settled part).
    for (const axis of ["W", "D", "H"] as const) {
      expect(
        screen.getByTestId(`brief-part-row-${axis}`).getAttribute("data-provenance"),
      ).toBe("measured");
    }
  });

  it('"Your changes" header renders only when it has at least one row', () => {
    // No change rows (only W/D/H axis rows) → no "Your changes" header.
    render(
      <Brief
        {...baseProps}
        entries={[axisStated("W", 60), axisStated("D", 45), axisStated("H", 80)]}
        part={settledPart()}
      />,
    );
    expect(screen.queryByTestId("brief-zone-changes-header")).toBeNull();
    // The part zone is still present.
    expect(screen.getByTestId("brief-zone-part-header")).toBeTruthy();
  });

  it('"Your changes" header renders when there is at least one change row', () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34), assumed("wall_gap", 8)]}
        part={settledPart()}
      />,
    );
    expect(
      screen.getByTestId("brief-zone-changes-header").textContent,
    ).toBe(copy.brief.yourChangesHeader);
    expect(
      screen.getByTestId("brief-zone-changes-header").textContent,
    ).toBe("Your changes");
    // The change rows render under the header.
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
    expect(screen.getByTestId("brief-row-wall_gap")).toBeTruthy();
  });

  it("unsettled part: rows show 'waiting on units' (no number), and the note is NOT shown", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        part={unsettledPart()}
      />,
    );
    // The part zone is present (the part block exists).
    expect(screen.getByTestId("brief-zone-part-header")).toBeTruthy();
    // Each W/D/H row shows the waiting control, never a number.
    for (const axis of ["W", "D", "H"] as const) {
      const row = screen.getByTestId(`brief-part-row-${axis}`);
      const valueCell = row.querySelector("[data-testid='brief-value']");
      expect(valueCell?.textContent).toBe(copy.brief.waitingOnUnits);
      expect(valueCell?.textContent).toBe("waiting on units");
      // No digit in the value cell.
      expect(valueCell?.textContent).not.toMatch(/\d/);
    }
    // The note is NOT shown (unsettled part).
    expect(screen.queryByTestId("brief-zone-part-note")).toBeNull();
  });

  it("unsettled part with ZERO design-state entries: W/D/H show 'waiting on units', never 'Nothing yet' (issue #352)", () => {
    render(
      <Brief {...baseProps} entries={[]} part={unsettledPart()} />,
    );
    // The part zone is present (the part block exists).
    expect(screen.getByTestId("brief-zone-part-header")).toBeTruthy();
    // Each W/D/H row shows the waiting control, never a number.
    for (const axis of ["W", "D", "H"] as const) {
      const row = screen.getByTestId(`brief-part-row-${axis}`);
      const valueCell = row.querySelector("[data-testid='brief-value']");
      expect(valueCell?.textContent).toBe(copy.brief.waitingOnUnits);
      expect(valueCell?.textContent).not.toMatch(/\d/);
    }
    // The 'Nothing yet' empty body is suppressed — the part zone IS the
    // content (the fallthrough the ticket calls out).
    expect(screen.queryByTestId("brief-empty")).toBeNull();
    // No 'Your changes' zone: zero change entries → no header, no rows.
    expect(screen.queryByTestId("brief-zone-changes-header")).toBeNull();
    expect(screen.queryByTestId("brief-rows")).toBeNull();
    // The note is NOT shown (unsettled part).
    expect(screen.queryByTestId("brief-zone-part-note")).toBeNull();
  });

  it("a 3MF part (settled) renders the file-own note, not 'you confirmed' (issue #352, operator decision 3)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        part={{ ...settledPart(), format: "3mf", filename: "bracket.3mf" }}
      />,
    );
    // The file-own note — the spec's exact sentence.
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      copy.brief.partBroughtNoteFromFile,
    );
    // ...and it does not say "you confirmed" (the units came from the
    // file, not a user choice).
    expect(screen.getByTestId("brief-zone-part-note").textContent).not.toContain(
      "you confirmed",
    );
    // The W/D/H values still render from `part.bbox_mm`.
    expect(
      screen.getByTestId("brief-part-row-W").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("60.0\u202Fmm");
  });

  it("a user-settled STL part keeps the 'you confirmed' note (issue #352)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        part={settledPart()}
      />,
    );
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      copy.brief.partBroughtNote("mm"),
    );
    expect(screen.getByTestId("brief-zone-part-note").textContent).toContain(
      "you confirmed",
    );
  });

  it("a measurement-escape part (settled, unit 'custom') gets the measurement note, never 'in custom you confirmed' (issue #352, #350 follow-up)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        part={{ ...settledPart(), unit: "custom", scale: 0.5 }}
      />,
    );
    // The measurement note — the part was measured, not settled in a unit
    // the user chose.
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      copy.brief.partBroughtNoteMeasured,
    );
    // Never "in custom you confirmed" (the pre-#352 contradiction).
    expect(screen.getByTestId("brief-zone-part-note").textContent).not.toContain(
      "you confirmed",
    );
    expect(screen.getByTestId("brief-zone-part-note").textContent).not.toContain(
      "in custom",
    );
    // The W/D/H values still render from `part.bbox_mm` (the derived mm).
    expect(
      screen.getByTestId("brief-part-row-W").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("60.0\u202Fmm");
  });

  it("zero entries and NO part: 'Nothing yet' still renders (the empty state is untouched)", () => {
    render(<Brief {...baseProps} entries={[]} part={null} />);
    expect(screen.getByTestId("brief-empty").textContent).toBe(
      copy.brief.emptyBody,
    );
    // No part zone at all.
    expect(screen.queryByTestId("brief-zone-part-header")).toBeNull();
  });

  it("assumed part: W/D/H rows show the mm values (not 'waiting on units') with assumed provenance + the assumed note (issue #350)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        part={assumedPart()}
      />,
    );
    // The part zone is present (the part block exists).
    expect(screen.getByTestId("brief-zone-part-header")).toBeTruthy();
    // Each W/D/H row shows the mm value from `part.bbox_mm` — never the
    // waiting control (the empty-Brief symptom QA §4 item 1).
    expect(
      screen.getByTestId("brief-part-row-W").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("60.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-D").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("45.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-H").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("80.0\u202Fmm");
    // Assumed provenance on the rows (not measured — the mm reading is
    // assumed, not confirmed).
    for (const axis of ["W", "D", "H"] as const) {
      expect(
        screen.getByTestId(`brief-part-row-${axis}`).getAttribute("data-provenance"),
      ).toBe("assumed");
    }
    // The assumed note (issue #350, operator decision 3) — not the
    // settled "you confirmed" note.
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      copy.brief.partBroughtNoteAssumed,
    );
    expect(screen.getByTestId("brief-zone-part-note").textContent).toBe(
      "Read as millimetres — if it's in inches, tell me.",
    );
  });

  it("the part rows survive the MAX_LIST_ROWS collapse (never folded)", () => {
    // 8 change rows (> 7 → the changes zone folds) + a settled part.
    // The part rows must ALWAYS be visible; only the changes fold.
    const entries: DesignStateEntry[] = [
      ...Array.from({ length: 8 }, (_, i) => stated(`p${i}`, 10 + i)),
    ];
    render(<Brief {...baseProps} entries={entries} part={settledPart()} />);
    // The part rows are all visible (never folded).
    expect(screen.getByTestId("brief-part-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-D")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-H")).toBeTruthy();
    // The changes zone folds (the count disclosure is present).
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(8),
    );
    // The change rows are hidden (folded).
    expect(screen.queryByTestId("brief-row-p0")).toBeNull();
  });

  it("part_width/part_depth/part_height param rows are never under 'Your changes' (issue #416, defect 2)", () => {
    // The model emits `part_width` / `part_depth` / `part_height` params
    // (the part's own W/D/H as named parameters). These are the part's own
    // dimensions — they must NOT appear under "Your changes". They are
    // excluded by `isPartRow` (the same rule that excludes literal W/D/H
    // param rows).
    const entries: DesignStateEntry[] = [
      { name: "part_width", kind: "param", label: "Part width", value: 20, unit: "mm", provenance: "stated" },
      { name: "part_depth", kind: "param", label: "Part depth", value: 20, unit: "mm", provenance: "stated" },
      { name: "part_height", kind: "param", label: "Part height", value: 20, unit: "mm", provenance: "stated" },
      stated("rod_bore", 34),
    ];
    render(<Brief {...baseProps} entries={entries} part={settledPart()} />);
    // The part zone is present.
    expect(screen.getByTestId("brief-zone-part-header")).toBeTruthy();
    // The "Your changes" header is present (rod_bore is a change).
    expect(screen.getByTestId("brief-zone-changes-header")).toBeTruthy();
    // The part_width/part_depth/part_height rows are NOT rendered as change rows.
    expect(screen.queryByTestId("brief-row-part_width")).toBeNull();
    expect(screen.queryByTestId("brief-row-part_depth")).toBeNull();
    expect(screen.queryByTestId("brief-row-part_height")).toBeNull();
    // The rod_bore row IS rendered.
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
  });
});

describe("BriefZones — part block absent (part=null)", () => {
  it("no part zone renders when part is null (even if W/D/H axis rows are present)", () => {
    // A no-part project that happens to have W/D/H axis rows: the W/D/H
    // rows are part of the single list, NOT a separate part zone.
    render(
      <Brief
        {...baseProps}
        entries={[axisStated("W", 60), axisStated("D", 45), stated("rod_bore", 34)]}
        part={null}
      />,
    );
    // No part-zone header.
    expect(screen.queryByTestId("brief-zone-part-header")).toBeNull();
    // No "Your changes" header (no part → single list).
    expect(screen.queryByTestId("brief-zone-changes-header")).toBeNull();
    // The W/D/H axis rows render in the single list.
    expect(screen.getByTestId("brief-row-axis-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-D")).toBeTruthy();
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
  });
});
