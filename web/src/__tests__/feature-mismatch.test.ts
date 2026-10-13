// Issue #433: the reply sentence for a stated-vs-made feature mismatch is
// written by the SPA from copy.ts (the server sends data only).
import { describe, expect, it } from "vitest";
import { confirmOffer, mm } from "../copy";
import { featureMismatchSentences } from "../featureMismatch";

describe("featureMismatchSentences (issue #433)", () => {
  it("builds one copy.ts sentence per mismatch record", () => {
    const out = featureMismatchSentences([
      { noun: "hole", stated_mm: 5, made_mm: 8, label: "Hole diameter" },
    ]);
    expect(out).toEqual([confirmOffer.featureMismatch("hole", 5, "Hole diameter", 8)]);
    expect(out[0]).toContain("You asked for a hole");
    expect(out[0]).toContain(mm(5));
    expect(out[0]).toContain(mm(8));
    expect(out[0].endsWith(".")).toBe(true);
  });

  it("uses 'an' before a vowel-initial feature noun", () => {
    const [sentence] = featureMismatchSentences([
      { noun: "opening", stated_mm: 4, made_mm: 6, label: "Opening width" },
    ]);
    expect(sentence.startsWith("You asked for an opening")).toBe(true);
  });

  it("returns nothing for an absent, empty or malformed list", () => {
    expect(featureMismatchSentences(undefined)).toEqual([]);
    expect(featureMismatchSentences("You asked for a hole 5.0 mm;")).toEqual([]);
    expect(
      featureMismatchSentences([{ noun: "hole", stated_mm: "5", made_mm: 8, label: "x" }]),
    ).toEqual([]);
  });
});
