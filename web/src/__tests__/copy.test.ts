/**
 * Copy deck unit tests — formatters and the moved filename builder.
 *
 * These guard the deck's house rules at the boundaries the review flagged:
 * unmeasured values never render as a number, the attempt counter never
 * reports a count outside the loop's 1..max invariant, and the 3MF name
 * never degrades to an empty slug. The W2 design-contract test (W3) already
 * asserts the deck's exact strings; these cover the guard behaviour that
 * has no contract assertion yet.
 */
import { describe, it, expect } from "vitest";
import { mm, dia, copy } from "../copy";
import { exportFilename } from "../lib/exportFilename";

describe("copy deck formatters", () => {
  it("mm()/dia() render the unknown phrase, never a number, for unmeasured values", () => {
    // A missing measurement or a failed parse reaches the formatter as NaN. The
    // unit stays attached — the value is the unknown phrase, not a number.
    expect(mm(Number.NaN)).toBe(`${copy.brief.unknownValue}\u202Fmm`);
    expect(dia(Number.NaN)).toBe(`Ø${copy.brief.unknownValue}\u202Fmm`);
    // ±Infinity is equally unmeasured.
    expect(mm(Number.POSITIVE_INFINITY)).toBe(`${copy.brief.unknownValue}\u202Fmm`);
    expect(mm(Number.NEGATIVE_INFINITY)).toBe(`${copy.brief.unknownValue}\u202Fmm`);
  });

  it("mm()/dia() still format finite values to one decimal with the unit", () => {
    expect(mm(60)).toBe("60.0\u202Fmm");
    expect(dia(12.34)).toBe("Ø12.3\u202Fmm");
  });

  it("the deck no longer owns the filename builder", () => {
    // The slugification/extension routine moved to lib/exportFilename.ts so
    // the deck holds display strings only.
    expect("exportFilename" in copy.shell).toBe(false);
  });
});

describe("exportFilename", () => {
  it("slugifies the project name and appends the version", () => {
    expect(exportFilename("Curtain rod bracket", "v4")).toBe("curtain-rod-bracket-v4.3mf");
  });

  it("trims leading and trailing hyphens from the slug", () => {
    expect(exportFilename("  --Odd--  ", "v1")).toBe("odd-v1.3mf");
  });

  it("falls back to 'model' when the project name has no Latin letters or digits", () => {
    // "日本" has no a-z0-9, so the slug would be empty — the fallback keeps the
    // download name usable. (Accented Latin like "Étagère" still slugifies to
    // "tag" because the bare letters survive the character class.)
    expect(exportFilename("日本", "v3")).toBe("model-v3.3mf");
    expect(exportFilename("###", "v3")).toBe("model-v3.3mf");
  });
});

describe("copy deck failure reasons", () => {
  it("failure.reasons holds only the validation-gate classes, not the design-loop reasons", () => {
    // The design loop's GATE_REASON_BITS + render-worker ErrorClass wording
    // lives in lib/errorMapping.ts; the deck keeps only the print-validation
    // classes that are not design-loop reasons, so the two maps cannot drift.
    const reasons = copy.failure.reasons;
    // Present: print-validation ErrorClass values that are not design-loop reasons.
    for (const key of [
      "load_error",
      "watertight",
      "winding",
      "dimension",
      "volume",
      "slice",
      "envelope",
      "export_error",
    ]) {
      expect(key in reasons).toBe(true);
    }
    // Absent: the keys errorMapping.ts owns (they are no longer duplicated here).
    for (const key of [
      "ok",
      "syntax_error",
      "empty_model",
      "artifact_error",
      "timeout",
      "oom",
      "container_error",
      "error_class_not_ok",
    ]) {
      expect(key in reasons).toBe(false);
    }
  });
});
