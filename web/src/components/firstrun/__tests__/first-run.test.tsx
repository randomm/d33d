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

  it("renders the body", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-body").textContent).toBe(copy.firstRun.body);
  });

  it("renders two equal cards: describe and file", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-describe-card")).toBeTruthy();
    expect(screen.getByTestId("first-run-file-card")).toBeTruthy();
  });

  it("renders the describe card label 'Describe it'", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-describe-label").textContent).toBe(
      copy.firstRun.describeLabel,
    );
  });

  it("renders the file card label 'Start from a file'", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-file-label").textContent).toBe(
      copy.firstRun.fileLabel,
    );
  });

  it("renders the large input with the deck's placeholder", () => {
    render(<FirstRun {...baseProps()} />);
    const input = screen.getByTestId("first-run-input") as HTMLInputElement;
    expect(input.placeholder).toBe(copy.firstRun.placeholder);
  });

  it("renders the file drop area", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-file-drop")).toBeTruthy();
  });

  it("renders the file caption", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-file-caption").textContent).toBe(
      copy.firstRun.fileCardCaption,
    );
  });

  it("renders the photo line under both cards", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-photo-hint").textContent).toBe(
      copy.firstRun.photoLine,
    );
  });

  it("the Add-a-photo button stays under the cards and routes to onPhotoSelect (issue #334 photo decision)", () => {
    const onPhotoSelect = vi.fn();
    render(<FirstRun {...baseProps({ onPhotoSelect })} />);
    const btn = screen.getByTestId("first-run-photo-btn");
    expect(btn.textContent).toBe(copy.firstRun.photoBtn);
    // The button sits under the two cards (a later sibling of the card row)
    // and still routes to the same photo input (onPhotoSelect).
    expect(btn).toBeTruthy();
    fireEvent.click(btn);
    expect(onPhotoSelect).toHaveBeenCalled();
  });

  it("the photo button is disabled while a design loop is in flight", () => {
    render(<FirstRun {...baseProps({ inFlight: true })} />);
    expect(screen.getByTestId("first-run-photo-btn")).toBeDisabled();
  });

  it("the Start button is disabled while a design loop is in flight, and a click on it does NOT fire onSend (issue #388 — the pre-first-frame window queues, never double-POSTs)", () => {
    // Regression guard for the 409 window (issue #388, operator decision 1):
    // the FirstRun composer is the active composer while the screen is up
    // (no version yet), so a fast second send before the first design-loop
    // frame must not produce a second POST. The inFlight gate disables the
    // Start button, which is the closed path — the sibling workstream owns
    // the queue logic in App.tsx (the inFlight value that reaches
    // FirstRun); this test pins FirstRun's contract: while inFlight, a
    // click on the disabled Start button is a no-op (the browser swallows
    // the click on a disabled button), so onSend is never reached.
    const onSend = vi.fn();
    render(<FirstRun {...baseProps({ onSend, inFlight: true })} />);
    const startBtn = screen.getByTestId("first-run-start-btn");
    expect(startBtn).toBeDisabled();
    // A click on the disabled button is a no-op — the browser swallows it,
    // and jsdom's fireEvent.click on a disabled button also does not fire
    // the handler. This is the observable contract the sibling's queue
    // logic relies on.
    fireEvent.click(startBtn);
    expect(onSend).not.toHaveBeenCalled();
  });

  it("the starter buttons are disabled while a design loop is in flight", () => {
    render(<FirstRun {...baseProps({ inFlight: true })} />);
    screen.getAllByTestId("first-run-starter").forEach((btn) => {
      expect(btn).toBeDisabled();
    });
  });

  it("renders the Start button", () => {
    render(<FirstRun {...baseProps()} />);
    expect(screen.getByTestId("first-run-start-btn")).toBeTruthy();
  });

  it("renders exactly four starters, each a complete sentence from the deck", () => {
    render(<FirstRun {...baseProps()} />);
    const starters = screen.getAllByTestId("first-run-starter");
    expect(starters).toHaveLength(4);
    starters.forEach((el, i) => {
      expect(el.textContent).toBe(copy.firstRun.starters[i]);
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
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.change(screen.getByTestId("first-run-input"), { target: { value: "   " } });
    fireEvent.click(screen.getByTestId("first-run-start-btn"));
    expect(onSend).not.toHaveBeenCalled();
  });

  it("fires onPartFile when a file is selected from the file input", () => {
    const onPartFile = vi.fn();
    render(<FirstRun {...baseProps({ onPartFile })} />);
    const fileInput = screen.getByTestId("first-run-file-input") as HTMLInputElement;
    const file = new File([new ArrayBuffer(8)], "test.stl", { type: "model/stl" });
    fireEvent.change(fileInput, { target: { files: [file] } });
    expect(onPartFile).toHaveBeenCalledWith(file);
  });

  it("fires onPartFile when a file is dropped on the file card", () => {
    const onPartFile = vi.fn();
    render(<FirstRun {...baseProps({ onPartFile })} />);
    const fileCard = screen.getByTestId("first-run-file-card");
    const file = new File([new ArrayBuffer(8)], "drop.stl", { type: "model/stl" });
    fireEvent.drop(fileCard, { dataTransfer: { files: [file] } });
    expect(onPartFile).toHaveBeenCalledWith(file);
  });
});

describe("PlateBackdrop", () => {
  it("caps the plate SVG's width below 52vh — the width cap's vh term stays reduced (issue #214)", () => {
    const { container } = render(<PlateBackdrop x={320} y={320} z={300} verified={true} />);
    const svg = container.querySelector("svg.plate-backdrop");
    expect(svg).not.toBeNull();
    // The SVG's width cap is min(Nvh, 46vw); the vh term pins how tall the
    // whole centred column (SVG + caption + note) can grow, which sets the
    // caption's vertical position against the FirstRun card's photo hint.
    // jsdom cannot lay out — this tripwire only pins the string so a silent
    // reversion of the vh term back to 52 or above is caught here; the real
    // geometric clearance gate is the manual-only Playwright spec.
    const width = (svg as unknown as { style: CSSStyleDeclaration }).style.width;
    const match = /min\((\d+(?:\.\d+)?)vh,\s*46vw\)/.exec(width);
    expect(match).not.toBeNull();
    const vhTerm = Number(match![1]);
    expect(vhTerm).toBeLessThan(52);
    // Lower bound: the cap must still be a real plate — a degenerate value
    // (0vh, 0.5vh) would be a non-sensical reversion this tripwire should not
    // silently pass. 40 is the floor of the "reduced but meaningful" band.
    expect(vhTerm).toBeGreaterThanOrEqual(40);
  });

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
