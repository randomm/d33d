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

  it("keeps the input's placeholder from truncating (issue #193)", () => {
    render(<FirstRun {...baseProps()} />);
    const input = screen.getByTestId("first-run-input") as HTMLInputElement;
    // The placeholder is one long sentence (copy.firstRun.placeholder).
    // Nothing may clip or ellipsize it: no overflow-hidden on the input, no
    // text-overflow ellipsis, no fixed min/max width that could collapse it,
    // and it fills the card's content width (width 100%, border-box).
    // jsdom cannot measure the ellipsis itself, so this is the structural
    // assertion the ticket commits to.
    expect(input.style.overflow).not.toBe("hidden");
    expect(input.style.textOverflow).not.toBe("ellipsis");
    expect(input.style.minWidth).toBe("");
    expect(input.style.maxWidth).toBe("");
    expect(input.style.width).toBe("100%");
    expect(input.style.boxSizing).toBe("border-box");
  });

  it("is not a clipped or scrolling box (issue #193)", () => {
    render(<FirstRun {...baseProps()} />);
    // The card is the inner div (the flex-column child), not the outer
    // absolute-positioned "first-run" wrapper, which carries no background.
    const card = screen.getByTestId("first-run").firstElementChild as HTMLElement;
    expect(card).not.toBeNull();
    // The card must never be a clipped/scrolling box: no auto/scroll
    // overflow, no fixed max-height, and its width and box model keep the
    // content inside the stage (width is bounded, box is border-box).
    const overflow = card.style.overflowY ?? card.style.overflow;
    expect(overflow).not.toBe("auto");
    expect(overflow).not.toBe("scroll");
    expect(card.style.maxHeight).not.toMatch(/^\d+(px|rem|vh)$/);
    expect(card.style.width).toBe("min(560px, 92vw)");
    expect(card.style.boxSizing).toBe("border-box");
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

  it("renders the card as a faint panel — the background mixes the panel colour at 35-50% alpha (issue #190)", () => {
    render(<FirstRun {...baseProps()} />);
    // The card is the inner div (the flex-column child), not the outer
    // absolute-positioned "first-run" wrapper, which carries no background.
    const card = screen.getByTestId("first-run").firstElementChild as HTMLElement;
    expect(card).not.toBeNull();
    // jsdom does not resolve color-mix() or custom properties, so assert on
    // the inline style string rather than a computed colour.
    const background = card.style.background;
    const match = /color-mix\(in srgb,\s*var\(--color-panel\)\s*(\d+(?:\.\d+)?)%/.exec(background);
    expect(match).not.toBeNull();
    const alpha = Number(match![1]);
    expect(alpha).toBeGreaterThanOrEqual(35);
    expect(alpha).toBeLessThanOrEqual(50);
    expect(alpha).not.toBe(92);
  });

  it("keeps the input and starters on the recess background (issue #190)", () => {
    render(<FirstRun {...baseProps()} />);
    const input = screen.getByTestId("first-run-input") as HTMLInputElement;
    expect(input.style.background).toBe("var(--color-recess)");
    const starters = screen.getAllByTestId("first-run-starter");
    starters.forEach((starter) => {
      expect((starter as HTMLElement).style.background).toBe("var(--color-recess)");
    });
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
    const { container } = render(<PlateBackdrop x={320} y={320} z={300} verified={false} />);
    const svg = container.querySelector("svg.plate-backdrop");
    expect(svg).not.toBeNull();
    // The viewBox is `0 0 x z` — the plate's top view: x wide, z deep.
    // This is the "drawn to scale" invariant: the drawing's coordinate space
    // IS the envelope, so if the API changes the drawing changes with it.
    expect(svg!.getAttribute("viewBox")).toBe("0 0 320 300");
  });

  it("draws a different plate for different envelope dimensions", () => {
    const { container } = render(<PlateBackdrop x={220} y={220} z={255} verified={false} />);
    const svg = container.querySelector("svg.plate-backdrop");
    expect(svg).not.toBeNull();
    expect(svg!.getAttribute("viewBox")).toBe("0 0 220 255");
  });

  it("renders the caption with the envelope numbers (from the deck)", () => {
    render(<PlateBackdrop x={320} y={320} z={300} verified={true} />);
    expect(screen.getByTestId("plate-caption").textContent).toBe(copy.firstRun.plateCaption(320, 320, 300));
  });

  it("the unconfirmed envelope's caption carries the qualifier; the confirmed one does not", () => {
    const qualifier = copy.firstRun.plateCaptionUnverified;
    // verified: false — the caption must say the numbers are not yet confirmed.
    const first = render(<PlateBackdrop x={320} y={320} z={300} verified={false} />);
    const unconfirmedCaption = screen.getByTestId("plate-caption").textContent ?? "";
    // The dimensions are still stated — a to-scale backdrop is genuinely
    // useful; it just must not claim to be confirmed.
    expect(unconfirmedCaption).toContain(copy.firstRun.plateCaption(320, 320, 300));
    expect(unconfirmedCaption).toContain(qualifier);
    first.unmount();
    // verified: true — the same numbers, no qualifier.
    render(<PlateBackdrop x={320} y={320} z={300} verified={true} />);
    const confirmedCaption = screen.getByTestId("plate-caption").textContent ?? "";
    expect(confirmedCaption).not.toContain(qualifier);
  });

  it("renders the plate note (from the deck)", () => {
    render(<PlateBackdrop x={320} y={320} z={300} verified={false} />);
    expect(screen.getByTestId("plate-note").textContent).toBe(copy.firstRun.plateNote);
  });

  it("the plate is low-contrast — the outline uses the hairline colour, not the marker", () => {
    const { container } = render(<PlateBackdrop x={320} y={320} z={300} verified={false} />);
    const rect = container.querySelector("rect.plate-outline");
    expect(rect).not.toBeNull();
    // The outline is the hairline colour at very low opacity — a backdrop,
    // not a model. The marker colour (#FF3300) must never appear here.
    const stroke = rect!.getAttribute("stroke") ?? "";
    expect(stroke).not.toContain("FF3300");
    expect(stroke).not.toContain("255, 51, 0");
  });
});
