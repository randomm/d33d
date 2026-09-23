/**
 * E2E: the "Attach reference photo" label vs pass-card overlap (issue #220).
 * MANUAL-ONLY — NOT run by CI. Run with:
 * `cd web && npx playwright test test_photo_upload_scroll.spec.ts`
 *
 * The ticket's acceptance criterion: the "Attach reference photo" label
 * (photo-upload-label, the PINNED flex: 0 0 auto sibling below the
 * transcript) does not overlap or obscure any conversation/pass-card
 * content at any scroll position, in BOTH layout branches — the docked
 * bar (window height < 900px: 1024 × 640 and 1280 × 720) and the
 * floating 420px column (window height ≥ 900px: 1280 × 900, the 900px
 * threshold itself — App.tsx uses strict less-than, so exactly 900 is
 * floating). The original bug: the label sat pinned over the scrolling
 * column (measured 143px of horizontal overlap with the pass-card column
 * in the docked layout), and the collision was scroll-position-dependent.
 * The fix (adversarial round 1): the transcript is the delivered sole
 * scroll container (the pane's overflowY:auto is intentionally left
 * inert); the label is a pinned sibling below it, so the overlap is
 * structurally impossible. The spec sweeps THREE scroll positions
 * (top, mid, max) per viewport to guard against regression, and asserts
 * ZERO rectangle intersection with every pass card box at each.
 *
 * Per-branch geometry: in the docked layout the bar is full-width and the
 * label sits in the in-flow flex column with the transcript, so the
 * horizontal axis is the non-vacuous check there (the 143px figure was
 * horizontal). In the floating layout the pane itself is 420px wide, so a
 * same-column sibling cannot overlap horizontally — the vertical
 * intersection (content scrolled behind the label) is the non-vacuous
 * check there. The assertion is the same code in both cases: intersection
 * on BOTH axes must be zero.
 *
 * Determinism & what is real vs intercepted:
 *   - Only the SSE transport is intercepted (GET /api/stream/{id}, the
 *     issue #182 mode (b) pattern from test_small_viewport.spec.ts):
 *     every stream for the spec's project is fulfilled with a version-
 *     created frame carrying the genuine rendered fixture
 *     web/tests/fixtures/viewer/mini-box.stl as `stl_data_uri` AND a
 *     non-empty `views` map (the issue's gap-gate finding: a version-
 *     created frame with an empty `views` map attaches no thumbnails, so
 *     the pass card renders without its view grid — the non-empty map is
 *     what makes each pass card the tall, scroll-filling content this
 *     spec drives), then a terminal done frame. NOT a real design-loop
 *     pass — no LLM, no Docker render dependency.
 *   - The `views` map is keyed by the render worker's fixed view
 *     filenames (view_00_front.png … view_05_iso.png) so the pass card
 *     renders its full 3×2 thumbnail grid plus source disclosure.
 *     The thumbnails point at a 100×100 PNG (not 1×1) so that at the
 *     floating layout's ~354px content width each thumbnail renders at
 *     ~118px tall, making four cards comfortably overflow the ~645px
 *     pane and the overflow precondition robust.
 *   - Project creation (lazy, issue #192) and every postChat round-trip
 *     run for real; only the bytes behind the stream are the fixture.
 *
 * Scroll-settle race: ChatPanel's smooth scrollTo (on the transcript, its
 * sole scroll container) fires on every message change and animates the
 * scrollTop. Before each measurement the spec parks the scroll container
 * (resolved via the shared findScrollContainer) at the target position and
 * then polls until the scrollTop is stable across consecutive frames, so
 * the assertion never races an in-flight auto-scroll.
 *
 * The intersection math and scroll-container logic live in
 * web/src/lib/scroll-container.ts and are verified by a passing DOM
 * fixture unit test (web/src/__tests__/scroll-container.test.ts) that
 * confirms zero overlap at all three positions for an in-flow label.
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";
import { findScrollContainer, maxIntersection, maxScroll, parkAt } from "../../src/lib/scroll-container";

/** The genuine rendered STL the stream delivers (the same real STL the
 *  render worker emits elsewhere in the suite). */
const STL_FIXTURE_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../fixtures/viewer/mini-box.stl",
);

/** The render worker's six fixed view filenames (VIEWS contract) — the
 *  pass card derives its caption labels from these stems, so keying the
 *  fulfilled `views` map by them renders the full thumbnail grid. */
const VIEW_FNS = [
  "view_00_front.png",
  "view_01_back.png",
  "view_02_left.png",
  "view_03_right.png",
  "view_04_top.png",
  "view_05_iso.png",
];

/** A 100×100 transparent PNG (iVBORw0KGgo… decodes to a 100×100
 *  RGBA image). Large enough that at the floating layout's ~354px
 *  content width each of the six thumbnails renders at ~118px tall,
 *  making four pass cards comfortably overflow the ~645px pane so
 *  the overflow precondition is robust. A 1×1 PNG would render at
 *  ~118px only if the browser scales it to the column width, but
 *  the 100×100 source makes the height deterministic across engines. */
const THUMBNAIL_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAGQAAABkCAYAAACEfmrnAAAAPklEQVR4nO3BMQEAAADCoPVPbQsvoAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAgKcBnKQAAXlHjfgAAAAASUVORGlc0us=";

const VIEWS_MAP: Record<string, string> = Object.fromEntries(
  VIEW_FNS.map((fn) => [fn, THUMBNAIL_PNG]),
);

const SOURCE_SNIPPET =
  "// test fixture source (issue #220 scroll-overlap spec)\n" +
  Array.from({ length: 24 }, (_, i) => `  box [${i}] = cube(8.0); // line ${i + 1}`).join("\n");

/** One SSE stream body: token (source disclosure) + version-created
 *  (genuine STL + non-empty views map) + done. */
function streamFrames(stlDataUri: string): string {
  return [
    `event: token\ndata: ${JSON.stringify({ text: SOURCE_SNIPPET })}\n\n`,
    `event: progress\ndata: ${JSON.stringify({
      step: "version-created",
      version_id: 1,
      stl_data_uri: stlDataUri,
      views: VIEWS_MAP,
    })}\n\n`,
    `event: done\ndata: ${JSON.stringify({
      message: "Design loop passed validation",
    })}\n\n`,
  ].join("");
}

/** The three viewports the acceptance criterion names. 900px is the
 *  FLOATING threshold itself (height < 900 → docked, strict less-than,
 *  so exactly 900 is floating); 640 and 720 are the docked side.
 *  Deliberately pinned — an 899px viewport would silently flip the
 *  branch under test. */
const VIEWPORTS: ReadonlyArray<{ w: number; h: number; branch: "docked" | "floating" }> = [
  { w: 1024, h: 640, branch: "docked" },
  { w: 1280, h: 720, branch: "docked" },
  { w: 1280, h: 900, branch: "floating" },
];

/** How many version-created frames (→ pass cards) the spec drives before
 *  measuring — enough to overflow the pane at every viewport above. */
const CARD_COUNT = 4;

/** Atomic intersection measurement at a scroll position (fraction of max
 *  scrollTop, 0 = top, 1 = max): real getBoundingClientRect (not
 *  boundingBox(), which is already rounded), the label's rectangle vs
 *  every pass card box, in a single evaluate. Uses the shared
 *  findScrollContainer + maxIntersection from scroll-container.ts so
 *  the math is the same code the unit test verifies. */
function readAt(fraction: number) {
  const pane = document.querySelector<HTMLElement>('[data-testid="app-left-pane"]');
  if (!pane) throw new Error("missing app-left-pane");
  const sc = findScrollContainer(pane);
  parkAt(sc, fraction);
  const label = document.querySelector<HTMLElement>('[data-testid="photo-upload-label"]');
  if (!label) throw new Error("missing photo-upload-label");
  const labelRect = label.getBoundingClientRect();
  const cards = Array.from(
    document.querySelectorAll<HTMLElement>('[data-testid="pass-card"]'),
  ).map((c) => c.getBoundingClientRect());
  const { overlapX, overlapY, detail } = maxIntersection(labelRect, cards);
  return {
    overlapX,
    overlapY,
    detail,
    cardCount: cards.length,
    scrollY: sc.scrollTop,
    maxScroll: maxScroll(sc),
  };
}

/** Wait until the scroll container's scrollTop is stable across
 *  consecutive frames (the smooth auto-scroll has settled). The
 *  previous-frame scrollTop is kept in the evaluate closure's scope
 *  (not stashed on the DOM node). Uses findScrollContainer() consistently
 *  with readAt/parkAndSettle. */
async function settleScroll(page: Page): Promise<void> {
  await page.waitForFunction(
    () => {
      const pane = document.querySelector<HTMLElement>('[data-testid="app-left-pane"]');
      if (!pane) return true;
      const sc = findScrollContainer(pane);
      const prev = (globalThis as { __lastScroll?: number }).__lastScroll;
      (globalThis as { __lastScroll?: number }).__lastScroll = sc.scrollTop;
      return prev !== undefined && Math.abs(prev - sc.scrollTop) < 0.5;
    },
    null,
    { timeout: 10_000, polling: 50 },
  );
}

/** Park the scroll container at `fraction` of max scrollTop, then wait
 *  for the scrollTop to be stable (the auto-scroll must not re-fire
 *  mid-measurement). Uses findScrollContainer() consistently. */
async function parkAndSettle(page: Page, fraction: number): Promise<void> {
  await page.evaluate((f) => {
    const pane = document.querySelector<HTMLElement>('[data-testid="app-left-pane"]');
    if (!pane) throw new Error("missing app-left-pane");
    const sc = findScrollContainer(pane);
    parkAt(sc, f);
  }, fraction);
  await settleScroll(page);
}

for (const { w, h, branch } of VIEWPORTS) {
  test(`photo-upload label has zero intersection with any pass card at every scroll position — ${branch} at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // -- Real send path: lazy project creation (issue #192) --------------
    // On a fresh mount the first-run screen is the input surface (the
    // chat composer is hidden while versions.length === 0 && messages.
    // length === 0), so the first Enter press targets first-run-input.
    const projectResp = page.waitForResponse(
      (r) => r.url().endsWith("/api/projects") && r.request().method() === "POST",
    );
    const firstRunInput = page.getByTestId("first-run-input");
    await expect(firstRunInput).toBeVisible();
    await firstRunInput.fill("a small box");
    await firstRunInput.press("Enter");
    const { id: projectId } = (await (await projectResp).json()) as { id: number };

    // -- Intercept the SSE transport (issue #182 mode (b)) ----------------
    // Every stream for this project is fulfilled with a version-created
    // frame (genuine STL + non-empty views map) and a done frame — so
    // each chat send produces one pass card, deterministically.
    const stlBytes = readFileSync(STL_FIXTURE_PATH);
    const stlDataUri = `data:model/stl;base64,${stlBytes.toString("base64")}`;
    const frames = streamFrames(stlDataUri);
    await page.route(
      (url) => url.pathname === `/api/stream/${projectId}`,
      async (route) => {
        await route.fulfill({
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
          body: frames,
        });
      },
    );

    // First send completes: a version exists, the first-run screen is
    // gone, the composer is the input surface, and the design loop flag
    // is released (the done frame) so the Send button is enabled.
    await expect(page.getByTestId("version-filmstrip")).toBeVisible();
    await expect(page.getByTestId("first-run")).toHaveCount(0);
    const send = page.getByTestId("chat-send-btn");
    await expect(send).toBeEnabled({ timeout: 15_000 });

    // The remaining (CARD_COUNT - 1) sends, one at a time (the in-flight
    // flag 409s a concurrent loop; the done frame releases it).
    for (let i = 1; i < CARD_COUNT; i++) {
      await page.getByTestId("chat-input").fill(`make it ${i} more boxes`);
      await expect(send).toBeEnabled();
      const postChat: Promise<unknown> = page.waitForResponse(
        (r) =>
          r.url() === `/api/projects/${projectId}/chat` &&
          r.request().method() === "POST",
      );
      await send.click();
      await postChat;
      await expect(send).toBeDisabled();
      // This send's stream completes (the done frame released the flag).
      await expect(send).toBeEnabled({ timeout: 15_000 });
    }

    // -- Preconditions (distinct failure messages, issue #214 pattern) ---
    const passCards = page.getByTestId("pass-card");
    await expect
      .soft(
        passCards,
        `PRECONDITION (issue #220, ${w}x${h}/${branch}): all ${CARD_COUNT} pass cards must be present before any overlap measurement — a missing card means a stream frame was not applied`,
      )
      .toHaveCount(CARD_COUNT);
    await expect
      .soft(
        page.getByTestId("photo-upload-label"),
        `PRECONDITION (issue #220, ${w}x${h}/${branch}): the photo-upload label must be visible — it is the element being measured against the pass cards`,
      )
      .toBeVisible();

    // The pane must actually overflow (the defect is only measurable in a
    // scrollable transcript — a non-overflowing pane would make the sweep
    // vacuous). The overflow precondition is on the resolved scroll
    // container (the transcript, per findScrollContainer), not the pane
    // itself — the pane's own overflowY:auto is intentionally left inert
    // (the transcript is the active scroller, issue #220 adversarial
    // round 1).
    const overflow = await page.evaluate(() => {
      const pane = document.querySelector<HTMLElement>('[data-testid="app-left-pane"]');
      if (!pane) return { overflows: false, scrollHeight: 0, clientHeight: 0 };
      const sc = findScrollContainer(pane);
      return {
        overflows: sc.scrollHeight > sc.clientHeight,
        scrollHeight: sc.scrollHeight,
        clientHeight: sc.clientHeight,
      };
    });
    expect(
      overflow,
      `PRECONDITION (issue #220, ${w}x${h}/${branch}): the transcript must genuinely overflow and scroll (scrollHeight ${overflow.scrollHeight} vs clientHeight ${overflow.clientHeight}) — without overflow the scroll sweep below is vacuous`,
    ).toMatchObject({ overflows: true });

    // Sole-active-scroller precondition (adversarial round 1 finding 2):
    // the transcript is the only ACTIVE scroll container in the pane.
    // The `.pass-card-source` disclosure has overflow:auto in CSS but is
    // collapsed (not rendered) in this spec, so no nested scroller can be
    // active here. If a future change makes the transcript unbounded
    // (the pane's overflowY:auto becomes the effective scroller again),
    // this fails with a distinct message naming the nested scroller.
    const nested = await page.evaluate(() => {
      const pane = document.querySelector<HTMLElement>('[data-testid="app-left-pane"]');
      if (!pane) return [] as Array<{ cls: string; sh: number; ch: number }>;
      const sc = findScrollContainer(pane);
      const out: Array<{ cls: string; sh: number; ch: number }> = [];
      for (const el of sc.querySelectorAll<HTMLElement>("*")) {
        const oy = getComputedStyle(el).overflowY;
        if ((oy === "auto" || oy === "scroll") && el.scrollHeight > el.clientHeight) {
          out.push({ cls: el.className || el.tagName.toLowerCase(), sh: el.scrollHeight, ch: el.clientHeight });
        }
      }
      return out;
    });
    expect(
      nested,
      `PRECONDITION (issue #220, ${w}x${h}/${branch}): the transcript must be the SOLE ACTIVE scroll container in the pane — found nested active scrollers: ${nested.map((n) => `${n.cls} (scroll ${n.sh} > client ${n.ch})`).join(", ") || "none"}`,
    ).toHaveLength(0);

    // Let the last auto-scroll (smooth scrollTo on the final message
    // change) finish before the sweep begins.
    await settleScroll(page);

    // -- The sweep: zero intersection at three scroll positions -----------
    // The label is a PINNED sibling below the transcript (flex: 0 0 auto,
    // never scrolled), so the label's position relative to the pane is
    // invariant across scroll offsets — the overlap at any position is
    // by construction zero when the transcript is bounded. The sweep
    // asserts the invariant at three positions (top, mid, max) so that a
    // regression that re-introduces a pinned/fixed label or an unbounded
    // transcript (the pre-fix geometry) fails at the position where the
    // overlap first appears, not at a single max-scrollTop check.
    const positions: ReadonlyArray<{ frac: number; name: string }> = [
      { frac: 0, name: "top" },
      { frac: 0.5, name: "mid" },
      { frac: 1, name: "max scrollTop" },
    ];
    for (const { frac, name } of positions) {
      await parkAndSettle(page, frac);
      const m = await page.evaluate((f) => readAt(f), frac);
      expect(m.cardCount, "the measurement must have seen the pass cards").toBe(CARD_COUNT);
      expect(
        m.overlapX,
        `at ${w}x${h}/${branch}, scroll position "${name}" (${m.scrollY.toFixed(1)}/${m.maxScroll.toFixed(1)}px): the photo-upload label must have ZERO horizontal intersection with any pass card — measured ${m.overlapX.toFixed(1)}px (${m.detail})`,
      ).toBe(0);
      expect(
        m.overlapY,
        `at ${w}x${h}/${branch}, scroll position "${name}" (${m.scrollY.toFixed(1)}/${m.maxScroll.toFixed(1)}px): the photo-upload label must have ZERO vertical intersection with any pass card — measured ${m.overlapY.toFixed(1)}px (${m.detail})`,
      ).toBe(0);
    }
  });
}
