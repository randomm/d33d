/**
 * FirstRun tests — the first-run screen (issue #128, W14).
 *
 * The screen is the centre column a new project sees before any version
 * exists: a headline, the millimetre contract stated once, one large input,
 * a photo button, Start, four complete starter sentences, a photo hint, and
 * the build plate drawn to scale behind everything — its caption's numbers
 * coming from the envelope (the API), never from a literal in the SPA.
 *
 * The plate is DRAWN TO SCALE: its SVG viewBox is derived from the envelope
 * dimensions, so if the API's numbers change the drawing changes with them.
 * A plate drawn at a fixed aspect with the caption fetched separately would
 * be the same defect wearing a nicer coat.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import { FirstRun } from "../FirstRun";
import { PlateBackdrop } from "../PlateBackdrop";
import copy from "../../../copy";

function baseProps(overrides: Partial<React.ComponentProps<typeof FirstRun>> = {}) {
  return {
    onSend: () => {},
    onPhotoSelect: () => {},
    inFlight: false,
    ...overrides,
  };
}

describe("FirstRun", () => {
  it("renders the headline", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-headline").textContent).toBe(copy.firstRun.headline);
  });

  it("renders the body (the millimetre contract stated once)", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-body").textContent).toBe(copy.firstRun.body);
  });

  it("renders the large input with the deck's placeholder", () => {
    render(<FirstRun {...baseProps()} />);
    const input = screen.getByTestId("first-run-input") as HTMLInputElement;
    expect(input.placeholder).toBe(copy.firstRun.placeholder);
  });

  it("renders the photo button and the Start button", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-photo-btn")).toBeTruthy();
    expect(screen.getByTestId("first-run-start-btn")).toBeTruthy();
  });

  it("renders exactly four starters, each a complete sentence from the deck", () => {
    render(<FirstRun {...baseProps()} />);
    const starters = screen.getAllByTestId("first-run-starter");
    expect(starters).toHaveLength(4);
    // Each starter's text matches the deck exactly — the starters are not
    // retyped into the component.
    starters.forEach((el, i) => {
      expect(el.textContent).toBe(copy.firstRun.starters[i]);
    });
  });

  it("renders the photo hint", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-photo-hint").textContent).toBe(copy.firstRun.photoHint);
  });

  it("fires onSend with the trimmed text on submit", () => {
    const onSend = vi.fn();
    render(<FirstRun {...baseProps({ onSend })} />);
    const input = screen.getByTestId("first-run-input");
    fireEvent.change(input, { target: { value: "  a bracket  " } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    expect(onSend).toHaveBeenCalledWith("a bracket");
  });

  it("does not fire onSend when the input is empty or whitespace-only", () => {
    const onSend = vi.fn();
    render(<FirstRun {...baseProps({ onSend })} />);
    // empty
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    expect(onSend).not.toHaveBeenCalled();
    // whitespace-only
    fireEvent.change(screen.getByTestId("first-run-input"), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    expect(onSend).not.toHaveBeenCalled();
  });
});

describe("PlateBackdrop", () => {
  it("draws the plate to scale — the SVG viewBox matches the envelope dimensions", () => {
    const { container } = render(<PlateBackdrop x={320} y={320} z={300} />);
    const svg = container.querySelector("svg.plate-backdrop");
    expect(svg).not.toBeNull();
    // The viewBox is `0 0 x z` — the plate's top view: x wide, z deep.
    // This is the "drawn to scale" invariant: the drawing's coordinate space
    // IS the envelope, so if the API changes the drawing changes with it.
    expect(svg!.getAttribute("viewBox")).toBe("0 0 320 300");
  });

  it("draws a different plate for different envelope dimensions", () => {
    const { container } = render(<PlateBackdrop x={220} y={220} z={255} />);
    const svg = container.querySelector("svg.plate-backdrop");
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute("viewBox")).toBe("0 0 220 255");
  });

  it("renders the caption with the envelope numbers (from the deck)", () => {
    render(<PlateBackdrop x={320} y={320} z={300} />);
    expect(screen.getByTestId("plate-caption").textContent).toBe(copy.firstRun.plateCaption(320, 320, 300));
  });

  it("renders the plate note (from the deck)", () => {
    render(<PlateBackdrop x={320} y={320} z={300} />);
    expect(screen.getByTestId("plate-note").textContent).toBe(copy.firstRun.plateNote);
  });

  it("the plate is low-contrast — the outline uses the hairline colour, not the marker", () => {
    const { container } = render(<PlateBackdrop x={320} y={320} z={300} />);
    const rect = container.querySelector("rect.plate-outline");
    expect(rect).not.toBeNull();
    // The outline is the hairline colour at very low opacity — a backdrop,
    // not a model. The marker colour (#FF3300) must never appear here.
    const stroke = rect!.getAttribute("stroke") ?? "";
    expect(stroke).not.toContain("FF3300");
    expect(stroke).not.toContain("255, 51, 0");
  });
});
