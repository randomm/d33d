/**
 * E2E: photo-attach button vs pass-card overlap (issue #220).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_photo_upload_overlap.spec.ts`
 *
 * The ticket's acceptance criterion: the "Attach reference photo" label
 * (photo-upload-label) does not overlap or obscure any pass-card content
 * at any scroll position, in both the docked layout (window height < 900,
 * strict less-than in App.tsx) and the floating layout (height >= 900).
 * This is the primary acceptance gate for the fix — jsdom performs no
 * layout and cannot verify scroll-position overlap, so the measurement
 * here is real getBoundingClientRect geometry in a real browser (the
 * viewport-loop + atomic-measure shape of
 * test_plate_caption_clearance.spec.ts, the SSE-intercept + mini-box.stl
 * pattern of test_small_viewport.spec.ts).
 *
 * The layout (issue #220 mechanism): the transcript (div.chat-messages,
 * flex: 1 1 auto + min-height: 0 + overflow-y: auto) is the sole scroll
 * container; photo-upload and the dimension canvas sit as pinned
 * flex: 0 0 auto siblings BELOW it, in the pane's flex column. A pinned
 * block below the transcript cannot overlap it at any scroll offset —
 * but the transcript must be GENUINELY bounded (its scrollHeight exceeds
 * its clientHeight), else a sibling pushed below the visible pane would
 * re-create the exact overlap this ticket fixes. The spec asserts both
 * facts: the transcript overflows (precondition, distinct failure
 * message) and the label has zero intersection with every pass-card box.
 *
 * Determinism & what is real vs intercepted:
 *   - The only interception is the SSE transport (GET /api/stream/{id},
 *     issue #182 mode (b)): fulfilled per pass with a version-created
 *     frame carrying (a) the genuine rendered fixture
 *     web/tests/fixtures/viewer/mini-box.stl as `stl_data_uri` and (b)
 *     a NON-EMPTY `views` map (App.tsx attaches PassCard views only when
 *     the map is non-empty — the gap-gate finding for this ticket) with
 *     one real pixel as the thumbnail source, then a terminal done
 *     frame. Project creation (lazy, issue #192), the postChat
 *     round-trips and the SPA's own SSE demultiplexing all run for real;
 *     only the bytes behind the stream are the fixture.
 *   - Two sends are needed: the first creates the project and renders
 *     the first pass card (the first-run input); the second (the real
 *     chat composer) adds a second pass card, so the transcript
 *     overflows the pane at every enumerated viewport (precondition,
 *     asserted with a distinct failure message).
 */

import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const VIEWPORTS: ReadonlyArray<{ w: number; h: number; layout: "docked" | "floating" }> = [
  // height < 900 (strict less-than, App.tsx) → docked bottom band
  { w: 1024, h: 640, layout: "docked" },
  { w: 1280, h: 720, layout: "docked" },
  // height >= 900 → floating top-left column (the 900px threshold is the
  // minimum for floating — the docked viewports alone cannot exercise it)
  { w: 1280, h: 900, layout: "floating" },
];

/** The genuine rendered STL the stream delivers (a real artifact already
 *  used by the viewer test fixtures — not hand-typed geometry). */
const STL_FIXTURE_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "../fixtures/viewer/mini-box.stl",
);

/** A real 1×1 PNG — the views map must be non-empty (App.tsx gates the
 *  PassCard's views on it), and a real pixel keeps the thumbnails
 *  genuine image loads, not broken-image placeholders. */
const ONE_PIXEL_PNG =
  "data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAAL0UW0wAAAABJRU5ErkJggg==";

function versionCreatedFrames(versionId: number, stlDataUri: string): string {
  const views: Record<string, string> = {};
  for (const stem of ["front", "back", "left", "right", "top", "iso"]) {
    views[`view_00_${stem}.png`] = ONE_PIXEL_PNG;
  }
  return [
    `event: progress\ndata: ${JSON.stringify({
      step: "version-created",
      version_id: versionId,
      stl_data_uri: stlDataUri,
      views,
    })}\n\n`,
    `event: done\ndata: ${JSON.stringify({
      message: `Design loop passed validation (pass ${versionId})`,
    })}\n\n`,
  ].join("");
}

interface Geom {
  /** Intersections found: one entry per pass-card box touching the label. */
  hits: Array<{ version: string; overlap: string }>;
  scrollable: boolean;
}

/** Read the label's box, every pass-card's box and the transcript's
 *  scroll geometry in a single evaluate() (real
 *  getBoundingClientRect — the rounded boundingBox() is not precise
 *  enough for a zero-overlap assertion). */
async function measureOverlap(page: Page): Promise<Geom> {
  return page.evaluate(() => {
    const label = document.querySelector<HTMLElement>(
      '[data-testid="photo-upload-label"]',
    );
    const cards = Array.from(
      document.querySelectorAll<HTMLElement>('[data-testid="pass-card"]'),
    );
    const transcript = document.querySelector<HTMLElement>(
      '[data-testid="chat-panel"] .chat-messages',
    );
    if (!label || cards.length === 0) {
      throw new Error(
        "missing photo-upload-label or pass-card — both must exist before measuring overlap",
      );
    }
    const lr = label.getBoundingClientRect();
    const hits: Geom["hits"] = [];
    for (const card of cards) {
      const cr = card.getBoundingClientRect();
      const xOverlap = Math.min(lr.right, cr.right) - Math.max(lr.left, cr.left);
      const yOverlap = Math.min(lr.bottom, cr.bottom) - Math.max(lr.top, cr.top);
      if (xOverlap > 0 && yOverlap > 0) {
        hits.push({
          version: card.dataset.version ?? "?",
          overlap: `${xOverlap.toFixed(1)}x${yOverlap.toFixed(1)}px`,
        });
      }
    }
    // The transcript must genuinely overflow — the fix's premise is that
    // the label sits below a bounded, scrolling transcript. A transcript
    // that does not overflow means the pane's column was not bounded at
    // all (the pre-fix geometry: the pinned block rides below the visible
    // pane and the overlap returns).
    const scrollable =
      transcript !== null && transcript.scrollHeight > transcript.clientHeight + 1;
    return { hits, scrollable };
  });
}

for (const { w, h, layout } of VIEWPORTS) {
  test(`photo-upload label does not overlap any pass card at max scroll (${layout}) at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(90_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // -- Drive the real send path (lazy project creation, issue #192) ----
    // On a fresh mount the first-run screen is the input surface (the
    // chat composer is hidden while versions.length === 0 &&
    // messages.length === 0), so the first Enter press targets
    // first-run-input, not chat-input.
    const projectResp = page.waitForResponse(
      (r) => r.url().endsWith("/api/projects") && r.request().method() === "POST",
    );
    const firstRunInput = page.getByTestId("first-run-input");
    await expect(firstRunInput).toBeVisible();
    await firstRunInput.fill("a small box");
    await firstRunInput.press("Enter");
    const { id: projectId } = (await (await projectResp).json()) as { id: number };

    // -- Intercept the SSE transport (issue #182 mode (b)) ----------------
    // Fulfilled per pass: a version-created frame carrying the real STL
    // fixture plus a non-empty views map, then the terminal done frame
    // (which releases the design loop so the next send is possible).
    const stlBytes = readFileSync(STL_FIXTURE_PATH);
    const stlDataUri = `data:model/stl;base64,${stlBytes.toString("base64")}`;
    let passCount = 0;
    await page.route(
      (url) => url.pathname === `/api/stream/${projectId}`,
      async (route) => {
        passCount += 1;
        await route.fulfill({
          status: 200,
          headers: { "Content-Type": "text/event-stream" },
          body: versionCreatedFrames(passCount, stlDataUri),
        });
      },
    );

    // The version-created frame refetches the timeline → a version
    // exists, the first-run screen unmounts, and the conversation pane
    // (with its composer) is the active input surface.
    await expect(page.getByTestId("version-filmstrip")).toBeVisible();
    await expect(page.getByTestId("first-run")).toHaveCount(0);

    // -- Precondition: a real PassCard exists and is visible (the frame
    //    carried a non-empty views map, so the pass card has thumbnails).
    await expect(page.getByTestId("pass-card")).toBeVisible({ timeout: 30_000 });
    await expect(page.getByTestId("pass-card-views")).toBeVisible();

    // -- A second pass: enough content to make the transcript genuinely
    //    scrollable at every enumerated viewport. The done frame has
    //    released the design loop, so the composer accepts the send.
    await page.getByTestId("chat-input").fill("make it taller");
    const secondDone: Promise<unknown> = page
      .getByTestId("pass-card[data-version='2']")
      .waitFor({ state: "visible", timeout: 60_000 });
    await page.getByTestId("chat-send-btn").click();
    await secondDone;
    await expect(page.getByTestId("pass-card[data-version='1']")).toBeVisible();

    // -- Precondition: the transcript really is the bounded scroller.
    //    A failure here is a BOUNDED-HEIGHT failure (the label's block is
    //    not pinned below a scrolling transcript), not an overlap
    //    failure — a distinct message per the gap-gate precondition
    //    pattern.
    const before = await measureOverlap(page);
    expect(
      before.scrollable,
      `PRECONDITION (issue #220, ${w}x${h}): the transcript (div.chat-messages) must genuinely overflow — scrollHeight must exceed clientHeight — for the label to be pinned below a real scroller; a non-overflowing transcript means the pane column is unbounded and the pre-fix geometry is back`,
    ).toBe(true);

    // -- Scroll the transcript (the sole scroll container) to its
    //    maximum scrollTop and wait for the auto-scroll to settle (the
    //    new message's smooth scrollTo must not race the measurement).
    await page
      .evaluate(() => {
        const el = document.querySelector<HTMLElement>(
          '[data-testid="chat-panel"] .chat-messages',
        );
        if (el) el.scrollTop = el.scrollHeight;
      });
    await page.waitForFunction(
      () => {
        const el = document.querySelector<HTMLElement>(
          '[data-testid="chat-panel"] .chat-messages',
        );
        if (!el) return false;
        return Math.abs(el.scrollHeight - el.clientHeight - el.scrollTop) <= 1;
      },
      undefined,
      { timeout: 10_000 },
    );

    // -- The acceptance criterion: zero horizontal AND vertical
    //    intersection between the photo-attach label and every pass
    //    card's box, measured at the max scroll position.
    const after = await measureOverlap(page);
    expect(
      after.hits,
      `at ${w}x${h} the photo-attach label must have zero intersection with every pass-card box — found: ${after.hits
        .map((x) => `pass-card ${x.version}: ${x.overlap}`)
        .join("; ")}`,
    ).toHaveLength(0);
  });
}
