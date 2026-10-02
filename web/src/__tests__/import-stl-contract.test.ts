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
    expect(copy.partUpload.dropLine).toBe("📦 Drop an STL or 3MF here");
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
});
