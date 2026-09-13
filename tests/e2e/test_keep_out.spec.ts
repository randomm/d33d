/**
 * E2E: keep-out zone gate failure (issue #49, spec 3 of 5).
 *
 * Same SSE interception as the over-envelope spec, but the gate-7
 * message prefix is `gate7/keep-out:` (d33d/print_validation.py's
 * _GATE7_KEEP_OUT_PREFIX — a mesh whose centred position still
 * overlaps the QIDI Plus 5 bed keep-out zone).
 *
 * Per the issue's gate resolution, `error_class` stays `"envelope"` —
 * keep-out is NOT a distinct member of the closed ErrorClass enum; the
 * distinguishing assertion is on the MESSAGE PREFIX, not on error_class.
 *
 * Assertion targets (same contract as the over-envelope spec):
 *  1. `data-testid="app-error"` shows the message containing
 *     `gate7/keep-out:` (and NOT the `gate7/envelope:` prefix).
 *  2. `data-testid="validation-status"` still reads the static
 *     "Waiting for render…" placeholder.
 */
import { test, expect } from "@playwright/test";

/** Build a WHATWG-SSE body from (event, data) frames, matching
 *  d33d/streaming.py's format_sse: an `event:` line, a `data:` line,
 *  then the blank line that terminates the frame. The SPA's streamEvents
 *  reader resolves on the terminal frame (error here), so a finite body
 *  is a complete stream. */
function sseBody(frames: Array<[string, unknown]>): string {
  return frames
    .map(([event, data]) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`)
    .join("");
}

test("gate7/keep-out failure surfaces in app-error; validation-status stays static", async ({ page }) => {
  // Let the SPA's mount-time project creation land, then read the id
  // from its POST /api/projects response. The stream is only opened
  // when a chat message is sent (well after mount), so installing the
  // route here cannot race the real server.
  const createPromise = page.waitForResponse(
    (res) => res.url().endsWith("/api/projects") && res.request().method() === "POST",
  );
  await page.goto("/");
  const createRes = await createPromise;
  const { id: pid } = (await createRes.json()) as { id: number };

  // Gate 7 keep-out failure: error_class stays "envelope" (keep-out is
  // a sub-branch of the envelope gate, not its own error class); the
  // discriminator is the gate7/keep-out message prefix.
  page.route(`**/api/stream/${pid}`, async (route) => {
    await route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sseBody([
        ["progress", { step: "validate", message: "running 7-gate pipeline" }],
        [
          "error",
          {
            error_class: "envelope",
            message:
              "gate7/keep-out: post-centre position (-12.50, 0.00, 0.00) overlaps the QIDI Plus 5 bed keep-out zone",
          },
        ],
      ]),
    });
  });

  // Opening the stream happens in handleSendMessage.
  await page.getByTestId("chat-input").fill("Render this part please");
  await page.getByTestId("chat-send-btn").click();

  // 1. The gate-failure message carries the gate7/keep-out: prefix and
  //    appears in the left-pane alert (App.tsx onError → app-error).
  const appError = page.getByTestId("app-error");
  await expect(appError).toBeVisible();
  await expect(appError).toContainText("gate7/keep-out:");
  await expect(appError).toContainText("keep-out zone");

  // The discriminator is the prefix, not a distinct error_class — so the
  // over-envelope prefix must NOT appear in this failure.
  await expect(appError).not.toContainText("gate7/envelope:");

  // 2. The right-pane validation status stays on its static placeholder —
  //    no design-loop-to-SSE wiring exists yet, so the interceptor
  //    cannot (and must not be expected to) drive it.
  await expect(page.getByTestId("validation-status")).toHaveText("Waiting for render…");
});
