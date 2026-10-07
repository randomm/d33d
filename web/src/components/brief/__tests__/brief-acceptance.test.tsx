/**
 * Brief — the acceptance surface (issue #123 / W9).
 *
 * The house anti-pattern in its purest form is a number rendered for a value
 * the system does not actually know. The unknown state is the whole point of
 * the Brief, so these tests pin each of the four provenance states, the
 * in-flight states, the twenty-parameter promotion, the failed-pass footer,
 * and the live-pin chip collapse.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
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
const measured = (name: string, value: number | null): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value,
  unit: value === null ? null : "mm",
  provenance: "measured",
});
const unknown = (name: string): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value: null,
  unit: null,
  provenance: "unknown",
});
const disagrees = (name: string, measuredMm: number, statedMm: number): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value: measuredMm,
  unit: "mm",
  provenance: "disagrees",
  stated_value: statedMm,
});
/** A USER-source disagrees row: `disagrees_source === "user"` — the
 *  user's stated value the measurement contradicts (a promoted axis
 *  param measured out of tolerance, issue #264). Renders the USER
 *  `disagreement` sentence, never the model sentence. */
const disagreesUser = (name: string, label: string, statedMm: number, measuredMm: number): DesignStateEntry => ({
  name,
  kind: "param",
  label,
  value: measuredMm,
  unit: "mm",
  provenance: "disagrees",
  stated_value: statedMm,
  disagrees_source: "user",
});
/** A MODEL-source disagrees row (issue #264): an assumed param the
 *  measurement contradicts — `disagrees_source: "model"`, so the expanded
 *  sentence names the model's own value, never "you asked for". */
const disagreesModel = (name: string, label: string, modelMm: number, measuredMm: number): DesignStateEntry => ({
  name,
  kind: "param",
  label,
  value: measuredMm,
  unit: "mm",
  provenance: "disagrees",
  stated_value: modelMm,
  disagrees_source: "model",
});
const assumed = (name: string, value: number): DesignStateEntry => ({
  name,
  kind: "param",
  label: name,
  value,
  unit: "mm",
  provenance: "assumed",
});
/** An AXIS row — the dimension protocol's own W/D/H axis (the user's
 *  stated evidence), never a parameter. Renders the axis word (Width/
 *  Depth/Height) and the `brief-row-axis-<name>` testid. */
const axisStated = (axis: "W" | "D" | "H", value: number): DesignStateEntry => ({
  name: axis,
  kind: "axis",
  label: axis,
  value,
  unit: "mm",
  provenance: "stated",
});

const baseProps = { isChip: false, inset: 24, conversationCollapsed: false } as const;

/** A settled imported part (issue #338): the two-zone Brief reads W/D/H
 *  from `bbox_mm` (settled-unit mm) and shows the part-zone note. */
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

describe("Brief — provenance states", () => {
  it("an unknown parameter renders the not-established control and NO number", () => {
    const onAsk = vi.fn();
    render(<Brief {...baseProps} entries={[unknown("wall_gap")]} onAsk={onAsk} />);
    const btn = screen.getByTestId("brief-unknown-btn");
    expect(btn.textContent).toBe(copy.brief.unknownValue);
    // The whole row must carry no digit — the value cell is the control.
    const row = screen.getByTestId("brief-row-wall_gap");
    expect(row.textContent).not.toMatch(/\d/);
    // The control is a real control: it sends the assistant a question.
    fireEvent.click(btn);
    expect(onAsk).toHaveBeenCalledWith("wall_gap");
  });

  it("a disagreeing parameter renders BOTH numbers collapsed — stated first, then measured (issue #385)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[disagrees("wall_gap", 37.8, 38.0)]}
      />,
    );
    const row = screen.getByTestId("brief-row-wall_gap");
    // The collapsed cell shows stated first, then measured (issue #385),
    // both in the mono face — the stated number is no longer relegated
    // to the expanded sentence only.
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe(copy.brief.disagreesInline(38.0, 37.8));
    expect(valueCell?.textContent).toContain("38.0");
    expect(valueCell?.textContent).toContain("37.8");
    expect((valueCell as HTMLElement)?.style.fontFamily).toBe("var(--font-mono)");
    // The stated one appears in the disagreement sentence (expanded row).
    expect(screen.queryByTestId("brief-disagreement")).toBeNull();
    // The expand handler sits on the row's inner flex div (label + mark +
    // value); click it, not the outer row wrapper.
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const sentence = screen.getByTestId("brief-disagreement");
    expect(sentence.textContent).toContain("38.0");
    expect(sentence.textContent).toContain("37.8");
    expect(sentence.textContent).toContain(copy.brief.disagreement(38.0, 37.8));
  });

  it("a model-source disagreeing parameter renders BOTH numbers collapsed and the model sentence, never 'You asked for' (issue #264)", () => {
    // Issue #264: an assumed axis param the measurement contradicts renders
    // the MODEL-source copy — the label named, the model's value first, the
    // measured one second, and never the user-source phrasing (the user
    // gave no value). 40 → 43.8 is 3.8 mm > max(20% of 40, 5) = 8 mm?
    // No — 3.8 ≤ 8 would be quiet, so this row pins the MAJOR case by
    // carrying `disagrees_major: true`: the ochre blocked token.
    render(
      <Brief
        {...baseProps}
        entries={[{ ...disagreesModel("spacer_width", "Spacer width", 40, 43.8), disagrees_major: true }]}
      />,
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    // The collapsed cell shows BOTH numbers — the model's 40 first (stated
    // position), the measured 43.8 second (issue #385).
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe(copy.brief.disagreesInline(40, 43.8));
    expect(valueCell?.textContent).toContain("40.0");
    expect(valueCell?.textContent).toContain("43.8");
    // The mark: the blocked token, never the #FF3300 marker colour.
    const markStyle = (row.querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
    expect(markStyle).toContain("--color-blocked");
    expect(markStyle).not.toContain("#ff3300");
    expect(markStyle).not.toContain("255, 51, 0");
    // The sentence is hidden until expanded.
    expect(screen.queryByTestId("brief-disagreement")).toBeNull();
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const sentence = screen.getByTestId("brief-disagreement");
    expect(sentence.textContent).toContain(copy.brief.disagreementModel("Spacer width", 40, 43.8));
    expect(sentence.textContent).toContain("I set Spacer width to 40.0\u202Fmm");
    expect(sentence.textContent).toContain("43.8");
    // The user-source phrasing must NOT appear — the user gave no value.
    expect(sentence.textContent).not.toContain("You asked for");
  });

  it("a user-source disagreeing param row (disagrees_source = 'user') renders the USER 'You asked for' sentence, never the model sentence (issue #264)", () => {
    // A promoted axis param the measurement contradicts carries
    // `disagrees_source: "user"` — the Brief must select the USER
    // `disagreement` copy (the user gave the value), not the model copy.
    render(
      <Brief {...baseProps} entries={[disagreesUser("spacer_width", "Spacer width", 40, 43.8)]} />,
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    // The collapsed cell shows BOTH numbers — stated 40 first, measured
    // 43.8 second (issue #385), in the mono face.
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe(copy.brief.disagreesInline(40, 43.8));
    expect((valueCell as HTMLElement)?.style.fontFamily).toBe("var(--font-mono)");
    // The sentence is hidden until expanded.
    expect(screen.queryByTestId("brief-disagreement")).toBeNull();
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const sentence = screen.getByTestId("brief-disagreement");
    // The USER copy: the stated 40 first, the measured 43.8 second.
    expect(sentence.textContent).toContain(copy.brief.disagreement(40, 43.8));
    expect(sentence.textContent).toContain("You asked for 40.0\u202fmm");
    expect(sentence.textContent).toContain("43.8");
    // Never the model phrasing (the user DID give the value here).
    expect(sentence.textContent).not.toContain("I set");
    expect(sentence.textContent).toContain("You asked for");
  });

  it("the D-lid four disagreeing rows (QA 2026-10-04) render stated-first with both numbers collapsed, both in mono, marks per source (issue #385)", () => {
    // The lid regression fixture from QA 2026-10-04 (REVIEW.md §5):
    // four rows, all provenance "disagrees", that read as plain measured
    // values when the stated number is hidden. The box rows are
    // user-source (the user stated the box's inside dimensions); rim_drop
    // and skirt_height are model-source WITHOUT disagrees_major (small,
    // model-only differences — the quiet mark, per QA 2026-10-05). Every
    // collapsed cell shows stated first, then measured, both in the mono
    // face.
    const entries: DesignStateEntry[] = [
      { ...disagreesUser("box_inner_width", "Box inner width", 60, 55) },
      { ...disagreesUser("box_inner_depth", "Box inner depth", 45, 40) },
      disagreesModel("rim_drop", "Rim drop", 2, 4),
      disagreesModel("skirt_height", "Skirt height", 5, 4),
    ];
    render(<Brief {...baseProps} entries={entries} />);
    const checks: Array<[string, number, number]> = [
      ["brief-row-box_inner_width", 60, 55],
      ["brief-row-box_inner_depth", 45, 40],
      ["brief-row-rim_drop", 2, 4],
      ["brief-row-skirt_height", 5, 4],
    ];
    for (const [testId, statedMm, measuredMm] of checks) {
      const row = screen.getByTestId(testId);
      // Both numbers, stated first then measured — e.g. "60.0 mm · measures 55.0 mm".
      const valueCell = row.querySelector("[data-testid='brief-value']");
      expect(valueCell?.textContent).toBe(copy.brief.disagreesInline(statedMm, measuredMm));
      expect(valueCell?.textContent).toContain(`${statedMm.toFixed(1)}`);
      expect(valueCell?.textContent).toContain(`${measuredMm.toFixed(1)}`);
      // Both numbers in the mono face.
      expect((valueCell as HTMLElement)?.style.fontFamily).toBe("var(--font-mono)");
      // Never the marker colour, even where ochre appears.
      const markStyle = (row.querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
      expect(markStyle).not.toContain("#ff3300");
      expect(markStyle).not.toContain("255, 51, 0");
    }
    // Mark per source: user-source rows are ochre (--color-blocked);
    // model-source rows without disagrees_major are the quiet tier
    // (issue #274, kept per QA 2026-10-05) — the neutral measured mark.
    const userMark = (screen.getByTestId("brief-row-box_inner_width").querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
    expect(userMark).toContain("--color-blocked");
    for (const id of ["brief-row-rim_drop", "brief-row-skirt_height"]) {
      const quietMark = (screen.getByTestId(id).querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
      expect(quietMark).toContain("--color-faint");
      expect(quietMark).not.toContain("--color-blocked");
    }
  });

  it("an assumed parameter with a declared axis stays assumed within tolerance (issue #264)", () => {
    // The within-tolerance case renders as a plain assumed row — no
    // disagreement sentence, no blocked mark, no promotion.
    render(
      <Brief {...baseProps} entries={[{ ...assumed("spacer_width", 40), axis: "W" as const }]} />
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    expect(row.getAttribute("data-provenance")).toBe("assumed");
    expect(screen.queryByTestId("brief-disagreement")).toBeNull();
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const expanded = screen.getByTestId("brief-row-expanded");
    expect(expanded.textContent).toContain("Nobody said this");
    expect(expanded.textContent).not.toContain("disagrees");
  });

  it("a stated parameter renders its number in the value cell", () => {
    render(<Brief {...baseProps} entries={[stated("rod_bore", 34)]} />);
    const valueCell = screen
      .getByTestId("brief-row-rod_bore")
      .querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toContain("34.0");
    expect(valueCell?.textContent).toContain("mm");
  });

  it("a measured parameter renders its number in the value cell", () => {
    render(<Brief {...baseProps} entries={[measured("wall_gap", 45)]} />);
    const valueCell = screen
      .getByTestId("brief-row-wall_gap")
      .querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toContain("45.0");
  });

  it("an assumed parameter renders its number in the value cell with the half-dot mark (issue #246)", () => {
    render(<Brief {...baseProps} entries={[assumed("spacer_height", 12)]} />);
    const row = screen.getByTestId("brief-row-spacer_height");
    // The row carries the new provenance and renders the number in the mono face.
    expect(row.getAttribute("data-provenance")).toBe("assumed");
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toContain("12.0");
    expect(valueCell?.textContent).toContain("mm");
    expect((valueCell as HTMLElement)?.style.fontFamily).toBe("var(--font-mono)");
    // The mark is the half-filled faint dot (issue #246): a linear-gradient
    // in the faint token, not the stated/measured/unknown/disagrees shapes.
    const mark = screen.getByTestId("brief-mark");
    expect(mark.style.background).toContain("var(--color-faint)");
    expect(mark.style.background.toLowerCase()).toContain("gradient");
  });

  it("the expanded assumed row reads 'Nobody said this. I picked {value}.' plus the Change-it action (issue #246)", () => {
    render(<Brief {...baseProps} entries={[assumed("spacer_height", 12)]} onChange={vi.fn()} />);
    const row = screen.getByTestId("brief-row-spacer_height");
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const expanded = screen.getByTestId("brief-row-expanded");
    // The sentence: no reason clause (issue #246 does not invent one).
    expect(expanded.textContent).toContain("Nobody said this. I picked 12.0\u202Fmm.");
    // The existing Change-it action is present on the expanded assumed row.
    expect(screen.getByTestId("brief-action-change").textContent).toBe(
      copy.brief.rowActions.change,
    );
  });
});

describe("Brief — in-flight states", () => {
  it("a row in flight renders the old AND new value, not the new one alone", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34)]}
        inFlight={{ rod_bore: { old: 34.0, new: 38.0 } }}
      />,
    );
    const valueCell = screen
      .getByTestId("brief-row-rod_bore")
      .querySelector("[data-testid='brief-value']");
    const text = valueCell?.textContent ?? "";
    // Both numbers are present — the old one is never dropped.
    expect(text).toContain("34.0");
    expect(text).toContain("38.0");
    expect(text).toContain("→");
  });

  it("a row being re-measured shows the re-measuring phrase, never the stale number", () => {
    render(
      <Brief
        {...baseProps}
        entries={[measured("wall_gap", 45)]}
        reMeasuring={["wall_gap"]}
      />,
    );
    const valueCell = screen
      .getByTestId("brief-row-wall_gap")
      .querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe(copy.brief.remeasuring);
    // The stale number is gone — showing it as if fresh is the lie.
    expect(valueCell?.textContent).not.toContain("45");
  });

  it("a measured row on a FIRST pass reads awaitingFirstMeasure, never a number", () => {
    render(<Brief {...baseProps} entries={[measured("wall_gap", null)]} />);
    const valueCell = screen
      .getByTestId("brief-row-wall_gap")
      .querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe(copy.brief.awaitingFirstMeasure);
    expect(valueCell?.textContent).not.toMatch(/\d/);
  });
});

describe("Brief — the list that does not grow", () => {
  it("at twenty parameters the unknowns are promoted and the collapsible params fold behind one count", () => {
    const entries: DesignStateEntry[] = [
      stated("W", 60),
      stated("D", 45),
      stated("H", 80),
      stated("rod_bore", 34),
      stated("wall_gap", 8),
      stated("screw_1", 4),
      stated("screw_2", 4),
      stated("screw_3", 4),
      stated("screw_4", 4),
      unknown("backplate_width"),
      unknown("screw_length"),
    ];
    // 9 collapsible settled params (> 7 → grouped), 2 unknowns (promoted).
    // The 11 entries exercise BOTH list maps (the promoted unknowns and the
    // group-collapsed resolved list) — the console spy asserts neither emits
    // a React key warning (issue #196 regression tripwire).
    const consoleErrorSpy = vi.spyOn(console, "error");
    render(<Brief {...baseProps} entries={entries} />);
    expect(
      consoleErrorSpy.mock.calls.filter((c) =>
        String(c[0]).includes('Each child in a list should have a unique "key" prop'),
      ),
    ).toEqual([]);
    consoleErrorSpy.mockRestore();
    // The unknowns render OUTSIDE the group, in their own block.
    const unknownsBlock = screen.getByTestId("brief-unknowns");
    expect(unknownsBlock.textContent).toContain("backplate_width");
    expect(unknownsBlock.textContent).toContain("screw_length");
    // The 9 collapsible settled params fold behind one honest count
    // (issue #274) — W/D/H are PARAM rows here (the axisStated helper is
    // not used), so all nine fold and N = 9.
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(9),
    );
    // The individual resolved rows are NOT rendered in the list (they sit
    // behind the count) — param row `W` is hidden.
    expect(screen.queryByTestId("brief-row-W")).toBeNull();
    // Expanding the disclosure reveals the collapsed rows.
    fireEvent.click(screen.getByTestId("brief-groups-count"));
    expect(screen.getByTestId("brief-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
  });

  it("a block of 3 axis rows + 13 params (3 disagrees) shows all axis and disagrees rows; the rest fold as 'N more parameters' (issue #274)", () => {
    // The acceptance criterion's exact shape: 3 axis rows, 13 params of
    // which 3 disagree → 10 collapsible settled params (> 7 → folded),
    // N = 10 (the collapsible count, not the resolved or param count).
    const entries: DesignStateEntry[] = [
      axisStated("W", 60),
      axisStated("D", 45),
      axisStated("H", 80),
      stated("rod_bore", 34),
      stated("wall_gap", 8),
      stated("screw_1", 4),
      stated("screw_2", 4),
      stated("screw_3", 4),
      stated("screw_4", 4),
      stated("screw_5", 4),
      stated("screw_6", 4),
      stated("screw_7", 4),
      stated("screw_8", 4),
      disagrees("lid_gap", 37.8, 38.0),
      disagreesUser("wall_thick", "Wall thickness", 4, 5.4),
      disagreesModel("base_plate", "Base plate", 20, 102),
    ];
    render(<Brief {...baseProps} entries={entries} />);
    // Every axis row and every disagrees row renders as a row.
    expect(screen.getByTestId("brief-row-axis-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-D")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-H")).toBeTruthy();
    expect(screen.getByTestId("brief-row-lid_gap")).toBeTruthy();
    expect(screen.getByTestId("brief-row-wall_thick")).toBeTruthy();
    expect(screen.getByTestId("brief-row-base_plate")).toBeTruthy();
    // The 10 settled params are hidden behind one line: "10 more parameters".
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(10),
    );
    expect(screen.queryByTestId("brief-row-rod_bore")).toBeNull();
    // Expanding restores the collapsed rows (all 10).
    fireEvent.click(screen.getByTestId("brief-groups-count"));
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
    expect(screen.getByTestId("brief-row-screw_8")).toBeTruthy();
  });

  it("a block of 3 axis rows + 5 params shows everything, with no collapse (issue #274)", () => {
    // 5 collapsible settled params ≤ 7: no group, every row visible — even
    // though the total resolved count (8) would exceed the old threshold.
    const entries: DesignStateEntry[] = [
      axisStated("W", 60),
      axisStated("D", 45),
      axisStated("H", 80),
      stated("rod_bore", 34),
      stated("wall_gap", 8),
      measured("screw_1", 4),
      assumed("screw_2", 4),
      unknown("backplate_width"),
    ];
    render(<Brief {...baseProps} entries={entries} />);
    expect(screen.queryByTestId("brief-groups")).toBeNull();
    for (const id of [
      "brief-row-axis-W",
      "brief-row-axis-D",
      "brief-row-axis-H",
      "brief-row-rod_bore",
      "brief-row-wall_gap",
      "brief-row-screw_1",
      "brief-row-screw_2",
    ]) {
      expect(screen.getByTestId(id), `${id} must be a visible row`).toBeTruthy();
    }
  });

  it("the QA box block (3 axis rows + settled params) renders one row per axis and folds the rest as the exact count (issue #316)", () => {
    // The de-dup happens in state_block_for_version (the server already
    // dropped the agreeing W/D/H param rows) — the Brief renders the
    // collapsed block verbatim: the 3 axis rows once each (never the
    // six-row repetition QA quoted), the settled params behind one
    // honest count that matches what is actually hidden.
    const entries: DesignStateEntry[] = [
      axisStated("W", 40),
      axisStated("D", 40),
      axisStated("H", 12),
      stated("box_wall", 2),
      stated("screw_1", 4),
      stated("screw_2", 4),
      stated("screw_3", 4),
      stated("screw_4", 4),
      stated("screw_5", 4),
      stated("screw_6", 4),
      stated("screw_7", 4),
      stated("screw_8", 4),
      stated("screw_9", 4),
      stated("screw_10", 4),
    ];
    render(<Brief {...baseProps} entries={entries} />);
    // One row per axis — exactly 3 axis rows, no param W/D/H duplicates.
    expect(screen.getByTestId("brief-row-axis-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-D")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-H")).toBeTruthy();
    expect(screen.queryByTestId("brief-row-W")).toBeNull();
    expect(screen.queryByTestId("brief-row-D")).toBeNull();
    expect(screen.queryByTestId("brief-row-H")).toBeNull();
    // The settled params fold behind one honest count. The fixture is
    // 3 axis rows (never collapsible) + 11 param rows (box_wall +
    // screw_1..screw_10), so the count is EXACTLY 11 — pinned literally
    // (a self-referential assertion that re-parses the rendered number
    // would pass for any count and pin nothing).
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(11),
    );
    expect(screen.queryByTestId("brief-row-screw_1")).toBeNull();
    // Expanding reveals all 11 hidden params — every one of them, pinned
    // by name so a dropped row anywhere in the fixture fails the test.
    fireEvent.click(screen.getByTestId("brief-groups-count"));
    expect(screen.getByTestId("brief-row-box_wall")).toBeTruthy();
    for (let i = 1; i <= 10; i += 1) {
      expect(screen.getByTestId(`brief-row-screw_${i}`)).toBeTruthy();
    }
  });

  it("unknown rows are never collapsed, whatever the collapsible count (issue #274)", () => {
    // 9 collapsible settled params (> 7 → the group exists) + 2 unknowns:
    // the unknowns render in their own block, never inside the group.
    const entries: DesignStateEntry[] = [
      ...Array.from({ length: 9 }, (_, i) => stated(`p${i}`, 10 + i)),
      unknown("backplate_width"),
      unknown("screw_length"),
    ];
    render(<Brief {...baseProps} entries={entries} />);
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(9),
    );
    const unknownsBlock = screen.getByTestId("brief-unknowns");
    expect(unknownsBlock.textContent).toContain("backplate_width");
    expect(unknownsBlock.textContent).toContain("screw_length");
    // Even with the group expanded, the unknowns stay in their own block.
    fireEvent.click(screen.getByTestId("brief-groups-count"));
    expect(screen.getByTestId("brief-unknowns").textContent).toContain(
      "backplate_width",
    );
  });

  it("a model-source 30 → 31 mm disagreement renders the quiet mark and BOTH numbers collapsed (issues #274, #385)", () => {
    // |30 − 31| = 1 mm ≤ max(20% of 30, 5) → the backend omits
    // `disagrees_major`; the SPA renders the quiet MARKS.measured ring,
    // never the ochre blocked token. Issue #385 (QA 2026-10-05):
    // the quiet tier is KEPT, but the collapsed cell still shows BOTH
    // numbers (stated first) — a small model-only difference is never
    // ochre, but the stated number is no longer hidden either.
    render(
      <Brief
        {...baseProps}
        entries={[{ ...disagreesModel("spacer_width", "Spacer width", 30, 31) }]}
      />,
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    const markStyle = (row.querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
    expect(markStyle).not.toContain("--color-blocked");
    expect(markStyle).toContain("--color-faint");
    expect(markStyle).not.toContain("#ff3300");
    // The expanded sentence is STILL the model sentence — mark selection
    // must not couple to sentence selection.
    const inner = row.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(inner);
    const sentence = screen.getByTestId("brief-disagreement");
    expect(sentence.textContent).toContain(copy.brief.disagreementModel("Spacer width", 30, 31));
  });

  it("a model-source 20 → 102 mm disagreement (disagrees_major true) renders ochre (issue #274)", () => {
    // |20 − 102| = 82 mm > max(20% of 20, 5) → the backend sets
    // `disagrees_major: true`; the SPA renders the ochre blocked token.
    render(
      <Brief
        {...baseProps}
        entries={[{ ...disagreesModel("base_plate", "Base plate", 20, 102), disagrees_major: true }]}
      />,
    );
    const row = screen.getByTestId("brief-row-base_plate");
    const markStyle = (row.querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
    expect(markStyle).toContain("--color-blocked");
    expect(markStyle).not.toContain("#ff3300");
  });

  it("a user-source 40 → 43.8 mm disagreement renders ochre, never quiet (issue #274)", () => {
    render(
      <Brief
        {...baseProps}
        entries={[disagreesUser("spacer_width", "Spacer width", 40, 43.8)]}
      />,
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    const markStyle = (row.querySelector("[data-testid='brief-mark']")?.getAttribute("style") ?? "").toLowerCase();
    expect(markStyle).toContain("--color-blocked");
    expect(markStyle).not.toContain("#ff3300");
  });

  it("coexistence: a param W row and an axis W row render as TWO rows with distinct identities (issue #246 review)", () => {
    // The HIGH finding: the model emits a `W` param (assumed) AND the user
    // stated `W` (the axis row) — the block carries BOTH, `name` alone is
    // not unique, and `kind`+`name` is the identity. Rendered through the
    // real Brief: two rows, distinct testids, no React key warning, and
    // expanding one row does not expand the other.
    const consoleErrorSpy = vi.spyOn(console, "error");
    render(
      <Brief
        {...baseProps}
        entries={[assumed("W", 60), axisStated("W", 60)]}
      />,
    );
    // No key warning across the coexistence block.
    const keyWarnings = consoleErrorSpy.mock.calls.filter((c) =>
      String(c[0]).includes('Each child in a list should have a unique "key" prop'),
    );
    expect(keyWarnings).toEqual([]);
    consoleErrorSpy.mockRestore();
    // Both rows are visible, with distinct testids (param W vs axis W).
    const paramRow = screen.getByTestId("brief-row-W");
    const axisRow = screen.getByTestId("brief-row-axis-W");
    expect(paramRow).toBeTruthy();
    expect(axisRow).toBeTruthy();
    expect(paramRow.getAttribute("data-provenance")).toBe("assumed");
    expect(axisRow.getAttribute("data-provenance")).toBe("stated");
    // The axis row renders its axis word (Width), the param row its name.
    expect(axisRow.textContent).toContain(copy.brief.axisLabel.W);
    expect(axisRow.textContent).toContain("Width");
    expect(paramRow.textContent).toContain("W");
    // Expanding the param row does not expand the axis row (identity is
    // per row, not per name).
    const paramInner = paramRow.querySelector("[data-testid='brief-value']")?.parentElement as HTMLElement;
    fireEvent.click(paramInner);
    const expanded = screen.getByTestId("brief-row-expanded");
    expect(paramRow.contains(expanded)).toBe(true);
    expect(axisRow.contains(expanded)).toBe(false);
    // The param row's expand sentence is the assumed one; the axis row
    // still carries no expanded node.
    expect(expanded.textContent).toContain("Nobody said this");
  });

  it("below the group threshold the resolved rows render individually", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("W", 60), stated("D", 45), unknown("H")]}
      />,
    );
    expect(screen.queryByTestId("brief-groups")).toBeNull();
    expect(screen.getByTestId("brief-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-D")).toBeTruthy();
    expect(screen.getByTestId("brief-unknowns")).toBeTruthy();
  });

  it("rows carry unique, stable keys — no key warning when entries reorder or refetch (issue #196)", () => {
    // The key must be the stable entry name, not the index: an index key
    // would silence the warning but re-attach row state to the wrong row on
    // reorder. Render with several entries, then re-render reordered and
    // spy console.error across the whole cycle.
    const consoleErrorSpy = vi.spyOn(console, "error");
    const entries: DesignStateEntry[] = [
      stated("W", 60),
      stated("D", 45),
      stated("H", 80),
      unknown("backplate_width"),
      unknown("screw_length"),
    ];
    const { rerender } = render(<Brief {...baseProps} entries={entries} />);
    // Reorder + a new entry arriving (the refetch shape): rows must keep
    // their identity, no warning may fire.
    rerender(
      <Brief
        {...baseProps}
        entries={[unknown("screw_length"), stated("H", 80), stated("D", 45), stated("W", 60), stated("new_param", 5)]}
      />,
    );
    const keyWarnings = consoleErrorSpy.mock.calls.filter((c) =>
      String(c[0]).includes('Each child in a list should have a unique "key" prop'),
    );
    expect(keyWarnings).toEqual([]);
    consoleErrorSpy.mockRestore();
    // Identity held: the re-rendered rows are still found by their name testids
    // (which are keyed off the same identity as the list key).
    expect(screen.getByTestId("brief-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-new_param")).toBeTruthy();
  });
});

describe("Brief — a failed pass", () => {
  it("a failed pass leaves every row unchanged and adds the ochre footer", () => {
    const entries = [stated("W", 60), stated("D", 45), unknown("H")];
    const { container, rerender } = render(
      <Brief {...baseProps} entries={entries} />,
    );
    const before = container.innerHTML;
    // Fire the failure.
    rerender(
      <Brief {...baseProps} entries={entries} failedPass="The 380 mm rail" />,
    );
    const after = container.innerHTML;
    // The footer is present and names the failed candidate.
    const footer = screen.getByTestId("brief-failed-footer");
    expect(footer.textContent).toBe(copy.brief.failedFooter("The 380 mm rail"));
    // The rows themselves are byte-identical (the failed candidate was
    // never accepted — nothing moved). The footer is the ONLY new node.
    const beforeRows = container.querySelectorAll(".brief-row").length;
    const afterRows = container.querySelectorAll(".brief-row").length;
    expect(afterRows).toBe(beforeRows);
    expect(after.startsWith(before)).toBe(false); // the footer changed the tree
    // Every row's content is unchanged (spot-check W — unknowns promote
    // first, so the first row in the tree is H, not W).
    expect(container.querySelector("[data-testid='brief-row-W']")?.textContent).toContain("60.0");
  });

  it("the footer is absent when no pass has failed", () => {
    render(<Brief {...baseProps} entries={[stated("W", 60)]} />);
    expect(screen.queryByTestId("brief-failed-footer")).toBeNull();
  });
});

describe("Brief — live pin collapses to chip", () => {
  it("a live pin collapses the Brief to its chip, and the chip retains the resolved row", () => {
    render(
      <Brief
        {...baseProps}
        isChip={false}
        entries={[stated("W", 60), stated("D", 45), unknown("H")]}
        hasLivePin
      />,
    );
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("chip");
    // The chip retains the resolved rows (name · value) and the unknown count.
    const chip = screen.getByTestId("brief-chip");
    expect(chip.textContent).toContain("W · 60.0\u202Fmm");
    expect(chip.textContent).toContain("D · 45.0\u202Fmm");
    expect(chip.textContent).toContain(copy.brief.collapsedUnknowns(1));
  });

  it("a small window (isChip) also collapses to the chip form", () => {
    render(
      <Brief
        inset={24}
        conversationCollapsed={false}
        isChip
        entries={[stated("W", 60)]}
      />,
    );
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("chip");
    expect(screen.getByTestId("brief-chip-resolved").textContent).toContain("60.0");
  });
});

describe("Brief — two-zone split (issue #338)", () => {
  // The zone SPLIT logic is pinned in brief-zones.test.tsx (the module
  // itself). These cases pin the two-zone shape at the ACCEPTANCE surface
  // the issue names: the part's W/D/H rows (measured provenance, mono)
  // survive the MAX_LIST_ROWS collapse, and no-part projects render exactly
  // as today.

  it("a project with an imported part shows 'The part you brought' W/D/H in mono, measured provenance", () => {
    render(
      <Brief
        {...baseProps}
        entries={[stated("rod_bore", 34), stated("wall_gap", 8)]}
        part={settledPart()}
      />,
    );
    // The part-zone header renders.
    expect(screen.getByTestId("brief-zone-part-header").textContent).toBe(
      "The part you brought",
    );
    // The W/D/H part rows render in measured provenance (the hollow ring).
    for (const axis of ["W", "D", "H"] as const) {
      const row = screen.getByTestId(`brief-part-row-${axis}`);
      expect(row.getAttribute("data-provenance")).toBe("measured");
    }
    // The values come from part.bbox_mm, in mm and mono.
    expect(
      screen.getByTestId("brief-part-row-W").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("60.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-D").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("45.0\u202Fmm");
    expect(
      screen.getByTestId("brief-part-row-H").querySelector("[data-testid='brief-value']")?.textContent,
    ).toBe("80.0\u202Fmm");
  });

  it("the part's W/D/H rows survive the MAX_LIST_ROWS collapse (never folded)", () => {
    // 8 change rows (> 7 → the changes zone folds) + a settled part. The
    // part rows must ALWAYS be visible; only the changes zone folds.
    const entries: DesignStateEntry[] = Array.from(
      { length: 8 },
      (_, i) => stated(`p${i}`, 10 + i),
    );
    render(<Brief {...baseProps} entries={entries} part={settledPart()} />);
    // All three part rows remain visible.
    expect(screen.getByTestId("brief-part-row-W")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-D")).toBeTruthy();
    expect(screen.getByTestId("brief-part-row-H")).toBeTruthy();
    // The changes zone folds behind its count disclosure.
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.moreParameters(8),
    );
    // The first change row is hidden (folded) — the part rows were not.
    expect(screen.queryByTestId("brief-row-p0")).toBeNull();
  });

  it("no-part projects render exactly as today: a single list, no zone headers", () => {
    // A W/D/H axis row on a no-part project is part of the SINGLE list — it
    // is NOT a separate part zone, and no zone header renders.
    render(
      <Brief
        {...baseProps}
        entries={[axisStated("W", 60), axisStated("D", 45), stated("rod_bore", 34)]}
        part={null}
      />,
    );
    expect(screen.queryByTestId("brief-zone-part-header")).toBeNull();
    expect(screen.queryByTestId("brief-zone-changes-header")).toBeNull();
    // The rows render in the single list.
    expect(screen.getByTestId("brief-row-axis-W")).toBeTruthy();
    expect(screen.getByTestId("brief-row-axis-D")).toBeTruthy();
    expect(screen.getByTestId("brief-row-rod_bore")).toBeTruthy();
  });
});
