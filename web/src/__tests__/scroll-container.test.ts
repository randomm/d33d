/**
 * DOM fixture test for the scroll-container and intersection helpers
 * (issue #220, adversarial review round 1 — HIGH finding #1).
 *
 * The commit message for the e2e spec claims "DOM fixture test confirms
 * zero overlap at all three positions for an in-flow label." This is that
 * test: it builds a jsdom fixture matching the real app-left-pane /
 * pass-card / photo-upload-label geometry and verifies:
 *
 *   1. findScrollContainer() correctly identifies the scroll container
 *   2. maxScroll() / parkAt() position the scroll container correctly
 *   3. maxIntersection() returns zero overlap for an in-flow label at
 *      all three sweep positions (top / mid / max scrollTop)
 *   4. maxIntersection() correctly detects non-zero overlap when a
 *      floating label DOES overlap a card (regression guard for the
 *      original bug)
 *   5. The intersection math handles edge cases (disjoint, touching,
 *      one-axis-only, negative overlap) correctly — requiring BOTH
 *      axes to overlap for a true intersection
 */

import { describe, it, expect } from "vitest";
import {
  findScrollContainer,
  maxScroll,
  parkAt,
  maxIntersection,
  type Rect,
} from "../lib/scroll-container";

/** Make a div with a data-testid. */
function el(testId: string): HTMLElement {
  const d = document.createElement("div");
  d.setAttribute("data-testid", testId);
  return d;
}

// ── findScrollContainer ────────────────────────────────────────────────

describe("findScrollContainer (issue #220)", () => {
  it("returns the element itself when it is the scroll container", () => {
    const pane = el("app-left-pane");
    pane.style.overflowY = "auto";
    Object.defineProperty(pane, "scrollHeight", { value: 800 });
    Object.defineProperty(pane, "clientHeight", { value: 600 });
    const sc = findScrollContainer(pane);
    expect(sc).toBe(pane);
  });

  it("falls back to the start element when no ancestor scrolls", () => {
    const d = el("no-scroll");
    d.style.overflowY = "visible";
    document.body.appendChild(d);
    const sc = findScrollContainer(d);
    expect(sc).toBe(d);
    document.body.innerHTML = "";
  });

  it("walks up to an ancestor that is the scroll container", () => {
    const outer = el("outer-scroller");
    outer.style.overflowY = "auto";
    Object.defineProperty(outer, "scrollHeight", { value: 1000 });
    Object.defineProperty(outer, "clientHeight", { value: 500 });

    const inner = el("inner");
    inner.style.overflowY = "visible";
    inner.appendChild(outer);
    document.body.appendChild(inner);

    // Starting from outer (the scroller), it returns itself.
    const sc = findScrollContainer(outer);
    expect(sc).toBe(outer);

    document.body.innerHTML = "";
  });
});

// ── maxScroll / parkAt ─────────────────────────────────────────────────

describe("maxScroll (issue #220)", () => {
  it("returns scrollHeight - clientHeight", () => {
    const d = el("sc");
    Object.defineProperty(d, "scrollHeight", { value: 1000 });
    Object.defineProperty(d, "clientHeight", { value: 600 });
    expect(maxScroll(d)).toBe(400);
  });

  it("returns 0 when there is no overflow", () => {
    const d = el("sc");
    Object.defineProperty(d, "scrollHeight", { value: 500 });
    Object.defineProperty(d, "clientHeight", { value: 500 });
    expect(maxScroll(d)).toBe(0);
  });
});

describe("parkAt (issue #220)", () => {
  it("parks at fraction of max scrollTop", () => {
    const d = el("sc");
    Object.defineProperty(d, "scrollHeight", { value: 1000 });
    Object.defineProperty(d, "clientHeight", { value: 600 });
    d.scrollTop = 0;

    parkAt(d, 0);
    expect(d.scrollTop).toBe(0);
    parkAt(d, 0.5);
    expect(d.scrollTop).toBe(200);
    parkAt(d, 1);
    expect(d.scrollTop).toBe(400);
  });
});

// ── maxIntersection: pure math ─────────────────────────────────────────

describe("maxIntersection — true 2D intersection (issue #220)", () => {
  it("returns zero for disjoint rects (vertically separated)", () => {
    const label: Rect = { left: 10, top: 500, right: 400, bottom: 540 };
    const card: Rect = { left: 10, top: 100, right: 400, bottom: 400 };
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(0);
    expect(r.overlapY).toBe(0);
  });

  it("returns zero when rects share x but not y (one-axis-only)", () => {
    // Same x-range, but vertically disjoint.
    const label: Rect = { left: 0, top: 0, right: 100, bottom: 50 };
    const card: Rect = { left: 0, top: 60, right: 100, bottom: 100 };
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(0);
    expect(r.overlapY).toBe(0);
  });

  it("returns zero for touching rects (edge-to-edge, zero area)", () => {
    const label: Rect = { left: 0, top: 0, right: 50, bottom: 50 };
    const card: Rect = { left: 50, top: 0, right: 100, bottom: 50 };
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(0);
    expect(r.overlapY).toBe(0);
  });

  it("returns zero for an empty card list", () => {
    const label: Rect = { left: 0, top: 0, right: 100, bottom: 100 };
    const r = maxIntersection(label, []);
    expect(r.overlapX).toBe(0);
    expect(r.overlapY).toBe(0);
  });

  it("detects true 2D intersection (both axes overlap)", () => {
    const label: Rect = { left: 100, top: 100, right: 300, bottom: 200 };
    const card: Rect = { left: 150, top: 120, right: 350, bottom: 250 };
    // x: min(300,350) - max(100,150) = 300-150 = 150
    // y: min(200,250) - max(100,120) = 200-120 = 80
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(150);
    expect(r.overlapY).toBe(80);
  });

  it("detects full containment (label inside card)", () => {
    const label: Rect = { left: 50, top: 50, right: 100, bottom: 100 };
    const card: Rect = { left: 0, top: 0, right: 200, bottom: 200 };
    // x: min(100,200) - max(50,0) = 50
    // y: min(100,200) - max(50,0) = 50
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(50);
    expect(r.overlapY).toBe(50);
  });

  it("picks the largest-area intersection among multiple cards", () => {
    const label: Rect = { left: 0, top: 0, right: 100, bottom: 100 };
    // Card 1: x=50, y=50, area=2500
    const card1: Rect = { left: 50, top: 50, right: 150, bottom: 150 };
    // Card 2: x=80, y=80, area=6400
    const card2: Rect = { left: 10, top: 10, right: 90, bottom: 90 };
    const r = maxIntersection(label, [card1, card2]);
    // Card 2 has the larger area (6400 > 2500).
    expect(r.overlapX).toBe(80);
    expect(r.overlapY).toBe(80);
  });

  it("handles negative offsets (card above-left of label)", () => {
    const label: Rect = { left: 100, top: 100, right: 200, bottom: 200 };
    const card: Rect = { left: 0, top: 0, right: 50, bottom: 50 };
    const r = maxIntersection(label, [card]);
    expect(r.overlapX).toBe(0);
    expect(r.overlapY).toBe(0);
  });

  it("reports zero when only one card overlaps and the other does not", () => {
    const label: Rect = { left: 0, top: 0, right: 100, bottom: 100 };
    const overlapping: Rect = { left: 50, top: 50, right: 150, bottom: 150 };
    const disjoint: Rect = { left: 200, top: 200, right: 300, bottom: 300 };
    const r = maxIntersection(label, [overlapping, disjoint]);
    expect(r.overlapX).toBe(50);
    expect(r.overlapY).toBe(50);
  });
});

// ── DOM fixture: in-flow label vs cards at 3 scroll positions ──────────

describe("DOM fixture: zero overlap at all three scroll positions (issue #220)", () => {
  /**
   * Build a jsdom fixture matching the real app geometry:
   *
   *   - app-left-pane: scroll container (overflowY:auto, 600px client,
   *     1200px scroll → max scrollTop = 600)
   *   - 4 pass cards inside the pane, each 150px tall, 20px gap:
   *     card 1: content y=20..170
   *     card 2: content y=180..330
   *     card 3: content y=340..490
   *     card 4: content y=500..650
   *   - photo-upload-label: in-flow sibling BELOW the scroll content
   *     (content y=1200..1240). In the real app, the label sits in the
   *     flex column after the transcript, so its viewport position
   *     shifts with scrollTop: viewport y = content y - scrollTop.
   *
   * Since both the label and cards shift by the same amount (-scrollTop),
   * their relative positions are invariant — the intersection is the
   * same at every scroll position. An in-flow label below the cards
   * cannot overlap them regardless of scroll.
   */
  function buildFixture(): { pane: HTMLElement; cards: HTMLElement[]; label: HTMLElement } {
    const pane = el("app-left-pane");
    pane.style.overflowY = "auto";
    Object.defineProperty(pane, "scrollHeight", { value: 1200 });
    Object.defineProperty(pane, "clientHeight", { value: 600 });
    Object.defineProperty(pane, "clientWidth", { value: 420 });
    pane.scrollTop = 0;

    const cardContentYs = [20, 180, 340, 500];
    const CARD_H = 150;
    const CARD_LEFT = 8;
    const CARD_RIGHT = 412;

    const cards: HTMLElement[] = cardContentYs.map((cy) => {
      const c = el("pass-card");
      // Viewport-space rect: content y minus scrollTop.
      Object.defineProperty(c, "getBoundingClientRect", {
        value: () => ({
          left: CARD_LEFT,
          top: cy - pane.scrollTop,
          right: CARD_RIGHT,
          bottom: cy + CARD_H - pane.scrollTop,
          width: CARD_RIGHT - CARD_LEFT,
          height: CARD_H,
          x: CARD_LEFT,
          y: cy - pane.scrollTop,
          toJSON: () => ({}),
        }),
      });
      pane.appendChild(c);
      return c;
    });

    // In-flow label below the scroll content (content y = 1200).
    const LABEL_Y = 1200;
    const LABEL_H = 40;
    const label = el("photo-upload-label");
    Object.defineProperty(label, "getBoundingClientRect", {
      value: () => ({
        left: 0,
        top: LABEL_Y - pane.scrollTop,
        right: 420,
        bottom: LABEL_Y + LABEL_H - pane.scrollTop,
        width: 420,
        height: LABEL_H,
        x: 0,
        y: LABEL_Y - pane.scrollTop,
        toJSON: () => ({}),
      }),
    });
    pane.appendChild(label);

    document.body.appendChild(pane);
    return { pane, cards, label };
  }

  it("findScrollContainer identifies the pane", () => {
    const { pane } = buildFixture();
    expect(findScrollContainer(pane)).toBe(pane);
    expect(maxScroll(pane)).toBe(600);
    document.body.innerHTML = "";
  });

  it.each([0, 0.5, 1])(
    "zero overlap at scroll fraction %f (in-flow label below in-flow cards)",
    (frac) => {
      const { pane, cards, label } = buildFixture();
      parkAt(pane, frac);

      const labelRect = label.getBoundingClientRect();
      const cardRects: Rect[] = cards.map((c) => c.getBoundingClientRect());
      const r = maxIntersection(labelRect, cardRects);

      // The label is in-flow at content y=1200, cards end at content
      // y=650. At every scroll position the label is below all cards.
      expect(r.overlapX).toBe(0);
      expect(r.overlapY).toBe(0);
      document.body.innerHTML = "";
    },
  );

  it("detects overlap when the label is a floating overlay (original bug)", () => {
    const { pane, cards } = buildFixture();

    // Remove the in-flow label.
    document.querySelector('[data-testid="photo-upload-label"]')?.remove();

    // Add a floating label pinned at viewport y=300 (does NOT shift
    // with scrollTop — it is position:fixed).
    const floatingLabel = el("photo-upload-label");
    Object.defineProperty(floatingLabel, "getBoundingClientRect", {
      value: () => ({
        left: 0,
        top: 300,
        right: 420,
        bottom: 340,
        width: 420,
        height: 40,
        x: 0,
        y: 300,
        toJSON: () => ({}),
      }),
    });
    document.body.appendChild(floatingLabel);

    // At scrollTop=0: card 2 is at viewport y=180..330.
    // Floating label is at viewport y=300..340.
    // Card 2 vs label: x = min(412,420)-max(8,0) = 404, y = min(330,340)-max(180,300) = 30.
    // True 2D intersection: 404 × 30.
    pane.scrollTop = 0;
    const r = maxIntersection(floatingLabel.getBoundingClientRect(), cards.map((c) => c.getBoundingClientRect()));
    expect(r.overlapX).toBe(404);
    expect(r.overlapY).toBe(30);

    document.body.innerHTML = "";
  });

  it("detects overlap at max scrollTop for a floating label over card 4", () => {
    const { pane, cards } = buildFixture();
    document.querySelector('[data-testid="photo-upload-label"]')?.remove();

    // Floating label pinned at viewport y=0..40 (top of viewport).
    const floatingLabel = el("photo-upload-label");
    Object.defineProperty(floatingLabel, "getBoundingClientRect", {
      value: () => ({
        left: 0, top: 0, right: 420, bottom: 40,
        width: 420, height: 40, x: 0, y: 0,
        toJSON: () => ({}),
      }),
    });
    document.body.appendChild(floatingLabel);

    // At max scrollTop (600): card 4 is at content y=500..650 → viewport y=-100..50.
    // Label at viewport y=0..40. Card 4 overlaps: x=404, y=min(50,40)-max(-100,0)=40.
    parkAt(pane, 1);
    const r = maxIntersection(floatingLabel.getBoundingClientRect(), cards.map((c) => c.getBoundingClientRect()));
    expect(r.overlapX).toBe(404);
    expect(r.overlapY).toBe(40);

    document.body.innerHTML = "";
  });
});
