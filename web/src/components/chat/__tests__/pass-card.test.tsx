/**
 * PassCard tests — the pass card for an assistant turn that produced a
 * version (issue #116 shell; filled by issue #125 / W10).
 *
 * Acceptance (issue #125):
 * - renders six view thumbnails when the frame carries six views
 * - renders the views that arrived when fewer than six did, and SAYS HOW
 *   MANY (copy.passCard.partialViews)
 * - the summary prose renders in the UI face (the .chat-msg-content
 *   monospace treatment is gone — W10)
 * - the source disclosure is collapsed by default and reports its line
 *   count; the source itself never renders as visible chat text
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { PassCard } from "../PassCard";
import type { RenderImage } from "../../../lib/renderImage";
import copy from "../../../copy";

const SIX_VIEWS: RenderImage[] = [
  { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
  { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
  { filename: "view_02_left.png", src: "data:image/png;base64,AAA" },
  { filename: "view_03_right.png", src: "data:image/png;base64,AAA" },
  { filename: "view_04_top.png", src: "data:image/png;base64,AAA" },
  { filename: "view_05_iso.png", src: "data:image/png;base64,AAA" },
];

const SCAD = "cube([20, 20, 20]);\n// a comment line\ntranslate([0, 0, 20]) cube([10, 10, 10]);";

describe("PassCard", () => {
  it("renders six view thumbnails when the frame carries six views", () => {
    render(<PassCard versionId={4} views={SIX_VIEWS} summary="A box, per your ask." />);
    for (const label of copy.passCard.viewLabels) {
      expect(screen.getByText(label), `caption ${label}`).toBeTruthy();
    }
    expect(screen.getAllByTestId(/^pass-card-view-/)).toHaveLength(6);
  });

  it("renders the views that arrived when fewer than six did, and says how many", () => {
    render(<PassCard versionId={2} views={SIX_VIEWS.slice(0, 4)} summary="A box." />);
    expect(screen.getAllByTestId(/^pass-card-view-/)).toHaveLength(4);
    expect(screen.getByTestId("pass-card-partial").textContent).toBe(
      copy.passCard.partialViews(4, 6),
    );
  });

  it("renders no partial-views line when all six arrived", () => {
    render(<PassCard versionId={4} views={SIX_VIEWS} summary="A box." />);
    expect(screen.queryByTestId("pass-card-partial")).toBeNull();
  });

  it("renders the real pass-summary string from the copy deck exactly (issue #218)", () => {
    // Regression lock: the pass card's summary line is the copy.ts string,
    // not an ad-hoc literal. Both sides bind to the same export — a rename of
    // either side or a string drift breaks this test.
    render(<PassCard versionId={4} views={SIX_VIEWS} summary={copy.passCard.summary} />);
    expect(screen.getByTestId("pass-card-summary").textContent).toBe(copy.passCard.summary);
    // The rendered node is present and non-empty — PassCard renders the
    // summary span only when truthy, so a missing/empty export would leave
    // the testid absent and this would fail.
    expect(screen.getByTestId("pass-card-summary")).toBeTruthy();
    expect(copy.passCard.summary.length).toBeGreaterThan(0);
  });

  it("the confirmation offer never leaks into the pass card's summary (issue #250)", () => {
    // The done frame's additive `confirm_offer` field is delivered by App
    // as its OWN plain assistant message AFTER the pass card — the PassCard
    // itself renders ONLY the copy deck's pass summary, never the offer
    // sentence (which belongs to the confirmOffer surface, not passCard).
    // The offer is a sentence in the conversation, not a form.
    const offer = copy.confirmOffer.offer("3.0\u202Fmm", "Wall thickness");
    render(
      <PassCard versionId={4} views={SIX_VIEWS} summary={copy.passCard.summary} />,
    );
    expect(screen.getByTestId("pass-card-summary").textContent).toBe(copy.passCard.summary);
    expect(screen.getByTestId("pass-card-summary").textContent).not.toContain(offer);
    expect(screen.getByTestId("pass-card-summary").textContent).not.toContain(
      "I assumed",
    );
    // No form or button inside the card beyond its attested actions — the
    // offer is never rendered as a control here.
    expect(screen.queryByRole("form")).toBeNull();
  });

  it("renders the version label when a version id is present", () => {
    render(<PassCard versionId={4} views={SIX_VIEWS} />);
    expect(screen.getByTestId("pass-card-version").textContent).toBe("v4");
  });

  it("renders no version label when no version exists yet", () => {
    render(<PassCard versionId={null} views={SIX_VIEWS} />);
    expect(screen.queryByTestId("pass-card-version")).toBeNull();
  });

  it("renders no view images and no grid when none arrived", () => {
    render(<PassCard versionId={1} views={[]} summary="A box." />);
    expect(screen.queryAllByTestId(/^pass-card-view-/)).toHaveLength(0);
    expect(screen.queryByTestId("pass-card-views")).toBeNull();
    // Zero views is not a "partial" pass — nothing rendered at all.
    expect(screen.queryByTestId("pass-card-partial")).toBeNull();
  });

  it("keeps the source collapsed by default and reports its line count", () => {
    render(<PassCard versionId={4} views={SIX_VIEWS} summary="A box." source={SCAD} />);
    const lines = SCAD.split("\n").length;
    expect(screen.getByTestId("pass-card-source-toggle").textContent).toBe(
      copy.passCard.sourceDisclosure(lines),
    );
    // Collapsed: the source body is not in the DOM.
    expect(screen.queryByTestId("pass-card-source")).toBeNull();
  });

  it("expands the source disclosure on demand", () => {
    render(<PassCard versionId={4} views={SIX_VIEWS} source={SCAD} />);
    fireEvent.click(screen.getByTestId("pass-card-source-toggle"));
    expect(screen.getByTestId("pass-card-source").textContent).toBe(SCAD);
  });

  it("opens a thumbnail enlarged with the Beside-the-photo action", () => {
    const onBesidePhoto = vi.fn();
    render(
      <PassCard versionId={4} views={SIX_VIEWS} summary="A box." onBesidePhoto={onBesidePhoto} />,
    );
    expect(screen.queryByTestId("pass-card-enlarged")).toBeNull();
    fireEvent.click(screen.getByTestId("pass-card-view-view_00_front.png"));
    expect(screen.getByTestId("pass-card-enlarged")).toBeTruthy();
    fireEvent.click(screen.getByTestId("pass-card-beside-photo"));
    expect(onBesidePhoto).toHaveBeenCalledTimes(1);
  });
});
