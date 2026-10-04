/**
 * E2E: first-run control hit-test (issue #347).
 * MANUAL-ONLY — NOT run by CI. Run with: `cd web && npx playwright test test_first_run_hit.spec.ts`
 *
 * The ticket's acceptance criterion: while the first-run card (.first-run,
 * z-index 10) is shown, every first-run control — Start, Add a photo,
 * each starter chip, the file card, and the file drop target — must be
 * the topmost element at its own centre point per document.elementFromPoint
 * (or an unforced Playwright click), NOT the conversation pane
 * (.app-left-pane, z-index 20) or any of its descendants. At 1440×900 the
 * pre-fix pane floated top-left over the card's controls; at 1280×800 the
 * docked bar covered the card's lower region. Both layouts must be
 * exercised — the 900px dock threshold (CONVERSATION_DOCK_MAX_HEIGHT_PX,
 * strict less-than) splits them.
 *
 * Why elementFromPoint (not a fill+Enter submit): the existing e2e specs
 * all send the first message through first-run-input.press("Enter"), which
 * bypasses the pointer entirely — a text-fill+Enter path can never catch
 * a click-blocking overlay. The hit-test IS the regression guard.
 *
 * The pane must ALSO be invisible (visibility:hidden) while first-run is
 * up — the fix keeps the pane mounted (FirstRun's photo button routes to
 * the pane's label[htmlFor="photo-file-input"].click(), so unmounting is
 * not allowed) but hides it. A bare pointer-events:none fix would still
 * show the 📦/📎 hints through the semi-transparent card, so the
 * visibility assertion is part of the criterion too.
 *
 * Setup is a bare page.goto("/"): the SPA auto-creates a fresh project on
 * load (first-run screen present) and the live backend always serves
 * GET /api/config/envelope, so the first-run card and the plate are up
 * with no interception. The e2e-guard (issue #207) in playwright.config
 * already refuses to run this suite against the operator's live server
 * without D33D_DATA_DIR set.
 */

import { expect, test } from "@playwright/test";

const VIEWPORTS: ReadonlyArray<{ w: number; h: number }> = [
  { w: 1024, h: 640 },
  { w: 1280, h: 800 },
  { w: 1440, h: 900 },
  { w: 1920, h: 1080 },
];

/** The first-run controls that must each be the topmost element at their
 *  own centre point while .first-run is shown (issue #347 acceptance).
 *  file-card passes if elementFromPoint returns the card OR any of its
 *  descendants (the card's centre can land on its caption prose); only
 *  file-drop requires the drop target itself (gap-gate decision 4). */
const CONTROLS = [
  { testid: "first-run-start-btn", label: "Start button" },
  { testid: "first-run-photo-btn", label: "Add a photo button" },
  { testid: "first-run-file-drop", label: "file drop target" },
] as const;

for (const { w, h } of VIEWPORTS) {
  test(`first-run controls are topmost and the pane is hidden at ${w}x${h}`, async ({
    page,
  }) => {
    test.setTimeout(60_000);

    await page.setViewportSize({ width: w, height: h });
    await page.goto("/");

    // Precondition: the first-run card is up (no leftover version from a
    // prior run that unmounted it) and the conversation pane is mounted
    // (the fix keeps it in the DOM — hidden, never unmounted).
    const card = page.getByTestId("first-run");
    await expect
      .soft(
        card,
        `PRECONDITION (issue #347, ${w}x${h}): first-run card must be present — a leftover version from a prior run unmounts it`,
      )
      .toBeVisible();

    const pane = page.getByTestId("app-left-pane");
    await expect(pane).toBeAttached();

    // The pane must be invisible while first-run is up — pointer-events:
    // none alone is NOT sufficient (the hints would still show through
    // the semi-transparent card).
    const paneVisibility = await pane.evaluate((el) =>
      window.getComputedStyle(el).visibility,
    );
    expect(
      paneVisibility,
      `at ${w}x${h} the conversation pane must be visibility:hidden while first-run is up (its 📦/📎 hints must not show through the card)`,
    ).toBe("hidden");

    // Each control: elementFromPoint at its centre must resolve to the
    // control itself (or, for the file card, any descendant). The pane
    // (or a descendant of it) must NEVER be the hit target.
    for (const { testid, label } of [
      ...CONTROLS,
      { testid: "first-run-file-card", label: "file card" } as const,
    ]) {
      // Gap-gate decision 4 (issue #347): for the file card, the hit may
      // be the card or ANY of its descendants (its centre can land on the
      // caption prose); for every other control, the hit must BE the
      // control or a descendant of it. One evaluate, containment check.
      const hit = await page.evaluate((tid: string) => {
        const el = document.querySelector<HTMLElement>(`[data-testid="${tid}"]`);
        if (!el) return { found: false, isControlOrDescendant: false, inPane: false };
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const target = document.elementFromPoint(cx, cy);
        return {
          found: true,
          isControlOrDescendant: !!target && el.contains(target),
          inPane: !!target?.closest('[data-testid="app-left-pane"]'),
        };
      }, testid);

      expect(hit.found, `at ${w}x${h} the ${label} must be present`).toBe(true);
      expect(
        hit.inPane,
        `at ${w}x${h} the ${label} (${testid}) centre must not resolve to the conversation pane`,
      ).toBe(false);
      expect(
        hit.isControlOrDescendant,
        `at ${w}x${h} the ${label} (${testid}) centre must resolve to the ${testid === "first-run-file-card" ? "card or a descendant of it" : "control or a descendant of it"}`,
      ).toBe(true);
    }

    // The four starter chips (all share testid first-run-starter).
    const starterHits = await page.evaluate(() => {
      const els = Array.from(
        document.querySelectorAll<HTMLElement>('[data-testid="first-run-starter"]'),
      );
      return els.map((el) => {
        const rect = el.getBoundingClientRect();
        const cx = rect.left + rect.width / 2;
        const cy = rect.top + rect.height / 2;
        const target = document.elementFromPoint(cx, cy);
        const inPane = !!target?.closest('[data-testid="app-left-pane"]');
        const isSelf = target === el || !!el.contains(target);
        return { inPane, isSelf };
      });
    });
    expect(starterHits.length, "four starter chips must be present").toBe(4);
    starterHits.forEach((s, i) => {
      expect(
        s.inPane,
        `starter chip ${i} centre must not resolve to the conversation pane`,
      ).toBe(false);
      expect(
        s.isSelf,
        `starter chip ${i} centre must resolve to the chip itself`,
      ).toBe(true);
    });
  });
}
