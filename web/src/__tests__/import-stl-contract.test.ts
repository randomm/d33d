/**
 * Design contract tests for the STL/3MF import feature (issue #334).
 *
 * Verifies that:
 * - The copy.ts Screen 1 and Screen 2 strings match the spec verbatim
 */

import { describe, expect, it } from "vitest";
import copy from "../copy";

describe("copy.ts Screen 1 strings", () => {
  it("firstRun.body matches the spec", () => {
    expect(copy.firstRun.body).toBe(
      "Describe it from scratch, or start from a file you already have and tell me what to change.",
    );
  });

  it("firstRun.describeLabel is 'Describe it'", () => {
    expect(copy.firstRun.describeLabel).toBe("Describe it");
  });

  it("firstRun.fileLabel is 'Start from a file'", () => {
    expect(copy.firstRun.fileLabel).toBe("Start from a file");
  });

  it("firstRun.fileCardCaption is verbatim", () => {
    expect(copy.firstRun.fileCardCaption).toBe(
      "The file becomes the part. I can add to it and cut from it — drill, slot, extend, split it for the bed. I can't resize what's already in it.",
    );
  });

  it("firstRun.fileDropLine is verbatim", () => {
    expect(copy.firstRun.fileDropLine).toBe("Drop an STL or 3MF here");
  });

  it("partUpload.dropLine is verbatim (the chat-pane drop area's own copy, distinct from firstRun.fileDropLine)", () => {
    // Issue #347, decision 1: the 📦 glyph is gone — text-only everywhere.
    expect(copy.partUpload.dropLine).toBe("Drop an STL or 3MF here");
  });

  it("partUpload.photoAttachLine is the text-only photo label (issue #347, decision 1)", () => {
    // The 📎 glyph is gone; the string now lives in the deck (it was
    // inlined in PhotoUpload.tsx, a user-string-contract violation).
    expect(copy.partUpload.photoAttachLine).toBe("Attach reference photo");
  });

  it("partUpload.uploading is verbatim (the in-flight status line)", () => {
    expect(copy.partUpload.uploading).toBe("Uploading…");
  });

  it("firstRun.fileChooseLine is verbatim", () => {
    expect(copy.firstRun.fileChooseLine).toBe("or choose a file");
  });

  it("firstRun.photoLine is verbatim", () => {
    expect(copy.firstRun.photoLine).toBe(
      "A photo is different: it's a reference to design against, not the part itself.",
    );
  });

  it("firstRun.placeholder matches the spec", () => {
    expect(copy.firstRun.placeholder).toBe(
      "A bracket for a 34 mm curtain rod, two M4 screws…",
    );
  });
});

describe("copy.ts Screen 2 strings", () => {
  it("partReport.iRead returns the correct string", () => {
    expect(copy.partReport.iRead("bracket.stl")).toBe("I read bracket.stl");
  });

  it("partReport.waitingOnUnits is verbatim", () => {
    expect(copy.partReport.waitingOnUnits).toBe("waiting on units");
  });

  it("partReport.assumedLine is verbatim with interpolation", () => {
    expect(copy.partReport.assumedLine("38", "24", "8")).toBe(
      "I read it as millimetres: 38 × 24 × 8. If it's in inches, tell me.",
    );
  });

  it("partReport.changeUnits is verbatim", () => {
    expect(copy.partReport.changeUnits).toBe("Change the units");
  });

  it("partReport.unitLabels.inch is verbatim", () => {
    expect(copy.partReport.unitLabels.inch).toBe("Inches");
  });

  it("partReport.unitLabels.mm is verbatim", () => {
    expect(copy.partReport.unitLabels.mm).toBe(
      "Millimetres — it really is that small",
    );
  });

  it("partReport.escapeLine is verbatim", () => {
    expect(copy.partReport.escapeLine).toBe(
      "Or tell me one real measurement — “the base is 60 mm wide” — and I'll scale from that.",
    );
  });

  it("partReport.escapePlaceholder is verbatim", () => {
    expect(copy.partReport.escapePlaceholder).toBe(
      "e.g. the base is 60 mm wide",
    );
  });

  it("partReport.unsettledCaption is verbatim", () => {
    expect(copy.partReport.unsettledCaption).toBe(
      "The shape is known. Its size isn't, until the units are.",
    );
  });

  it("partReport.settle is verbatim", () => {
    expect(copy.partReport.settle).toBe("Settle");
  });

  it("brief.partBroughtNoteAssumed is verbatim (issue #350: the assumed-Brief note)", () => {
    expect(copy.brief.partBroughtNoteAssumed).toBe(
      "Read as millimetres — if it's in inches, tell me.",
    );
  });

  it("brief.partBroughtNoteFromFile is the spec's exact 3MF sentence (issue #352, operator decision 3)", () => {
    expect(copy.brief.partBroughtNoteFromFile).toBe(
      "Measured, in the file's own millimetres. Its own features are fixed — I can add and cut, not resize.",
    );
    // The file-derived variant is distinct from the user-confirmed one —
    // "you confirmed" must be absent from the 3MF note.
    expect(copy.brief.partBroughtNote("mm")).not.toBe(
      copy.brief.partBroughtNoteFromFile,
    );
    expect(copy.brief.partBroughtNoteFromFile).not.toContain("you confirmed");
  });

  it("brief.partBroughtNoteMeasured is the measurement-escape note (issue #352, #350 follow-up comment)", () => {
    expect(copy.brief.partBroughtNoteMeasured).toBe(
      "Scaled from the measurement you gave. Its own features are fixed — I can add and cut, not resize.",
    );
    // It never reads "in custom you confirmed" — the pre-#352
    // contradiction for a part settled via the measurement escape.
    expect(copy.brief.partBroughtNoteMeasured).not.toContain("you confirmed");
    expect(copy.brief.partBroughtNoteMeasured).not.toContain("custom");
    // Distinct from the user-settled and the 3MF file-own notes.
    expect(copy.brief.partBroughtNoteMeasured).not.toBe(
      copy.brief.partBroughtNote("custom"),
    );
    expect(copy.brief.partBroughtNoteMeasured).not.toBe(
      copy.brief.partBroughtNoteFromFile,
    );
  });

  it("deterministicAnswer.sizeUnknownWhileUnsettled is the size-unknown sentence (issue #352, operator decision 1)", () => {
    // The backend's UNSETTLED_SIZE_REPLY carries the same sentence (the
    // parity pin is in design-contract.test.ts + the backend test).
    expect(copy.deterministicAnswer.sizeUnknownWhileUnsettled).toBe(
      "I can't give you a size until the part's units are settled — pick mm, cm, or inch (or give one measured axis) and the dimensions will be real.",
    );
    // No number: a size-unknown reply never carries a measurement.
    expect(copy.deterministicAnswer.sizeUnknownWhileUnsettled).not.toMatch(/\d/);
  });

  it("failure.survivedNoExport names the survivor without the export claim (issue #352, operator decision 4)", () => {
    expect(copy.failure.survivedNoExport("v4")).toBe("v4 is unchanged.");
    // Distinct from the exportable variant — no "still exportable" claim
    // when the export button is disabled.
    expect(copy.failure.survivedNoExport("v4")).not.toBe(
      copy.failure.survived("v4"),
    );
    expect(copy.failure.survivedNoExport("v4")).not.toContain("exportable");
  });

  it("partReport.watertightGaps is singular for n=1", () => {
    expect(copy.partReport.watertightGaps(1)).toBe(
      "watertight, after closing 1 small gap",
    );
  });

  it("partReport.watertightGaps is plural for n=3", () => {
    expect(copy.partReport.watertightGaps(3)).toBe(
      "watertight, after closing 3 small gaps",
    );
  });

  it("partReport.bodiesAfterRepair is the spec's exact drop line (issue #375, operator rule)", () => {
    expect(copy.partReport.bodiesAfterRepair(2, 1)).toBe("2 bodies → 1 after repair");
  });

  it("partReport.bodiesAfterRepair is singular for before=1", () => {
    expect(copy.partReport.bodiesAfterRepair(1, 1)).toBe("1 body → 1 after repair");
  });
});
