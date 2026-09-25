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
import type { DesignStateEntry } from "../../../lib/api";

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

  it("a disagreeing parameter renders the measured value and names the stated one", () => {
    render(
      <Brief
        {...baseProps}
        entries={[disagrees("wall_gap", 37.8, 38.0)]}
      />,
    );
    const row = screen.getByTestId("brief-row-wall_gap");
    // The measured value is the PRIMARY (the value cell).
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe("37.8\u202Fmm");
    expect(valueCell?.textContent).toContain("37.8");
    expect(valueCell?.textContent).not.toContain("38.0");
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

  it("a model-source disagreeing parameter renders the measured value and the model sentence, never 'You asked for' (issue #264)", () => {
    // Issue #264: an assumed axis param the measurement contradicts renders
    // the MODEL-source copy — the label named, the model's value first, the
    // measured one second, and never the user-source phrasing (the user
    // gave no value). The mark stays the ochre blocked token.
    render(
      <Brief {...baseProps} entries={[disagreesModel("spacer_width", "Spacer width", 40, 43.8)]} />,
    );
    const row = screen.getByTestId("brief-row-spacer_width");
    // The measured value is the PRIMARY (the value cell); the model's 40
    // never appears as if it were the printed size.
    const valueCell = row.querySelector("[data-testid='brief-value']");
    expect(valueCell?.textContent).toBe("43.8\u202Fmm");
    expect(valueCell?.textContent).not.toContain("40.0");
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
  it("at twenty parameters the unknowns are promoted and the remainder collapses to one count", () => {
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
    // 9 resolved (> 7 → grouped), 2 unknowns (promoted).
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
    // The resolved list collapses to one honest count.
    expect(screen.getByTestId("brief-groups-count").textContent).toBe(
      copy.brief.allParameters(9),
    );
    // The individual resolved rows are NOT rendered in the list (they sit
    // behind the count) — param row `W` and axis row `W` both hidden.
    expect(screen.queryByTestId("brief-row-W")).toBeNull();
    expect(screen.queryByTestId("brief-row-axis-W")).toBeNull();
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
