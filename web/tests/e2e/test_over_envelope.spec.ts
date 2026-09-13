/**
 * E2E: over-envelope gate failure (issue #49, spec 2 of 5).
 *
 * The SSE stream (GET /api/stream/{project_id}) is intercepted via
 * page.route and fed a gate-7 failure event: `event: error` with
 * `data { error_class: "envelope", message: "gate7/envelope: …" }` —
 * the same shape d33d/print_validation.py emits for a mesh whose
 * per-axis extent exceeds the QIDI Plus 5 build envelope.
 *
 * Assertion targets (per the issue's gate resolution):
 *  1. `data-testid="app-error"` — the SPA's only consumer of an SSE
 *     `error` event (App.tsx onError → setStreamError) — shows the
 *     gate-failure message containing the `gate7/envelope:` prefix.
 *  2. `data-testid="validation-status"` — still reads its static
 *     "Waiting for render…" placeholder: the design-loop-to-SSE wiring
 *     is a separate ticket, so no interceptor payload can drive it.
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

test("gate7/envelope failure surfaces in app-error; validation-status stays static", async ({ page }) => {
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

  // Gate 7 over-envelope failure — the prefix is the exact constant
  // d33d/print_validation.py emits (_GATE7_ENVELOPE_PREFIX).
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
              "gate7/envelope: dimension 0 (250.00mm) exceeds envelope 220.00mm on axis X — bounding box does not fit the QIDI Plus 5 build plate",
          },
        ],
      ]),
    });
  });

  // Opening the stream happens in handleSendMessage.
  await page.getByTestId("chat-input").fill("Render this part please");
  await page.getByTestId("chat-send-btn").click();

  // 1. The gate-failure message carries the gate7/envelope: prefix and
  //    appears in the left-pane alert (App.tsx onError → app-error).
  const appError = page.getByTestId("app-error");
  await expect(appError).toBeVisible();
  await expect(appError).toContainText("gate7/envelope:");
  await expect(appError).toContainText("exceeds envelope");

  // 2. The right-pane validation status stays on its static placeholder —
  //    no design-loop-to-SSE wiring exists yet, so the interceptor
  //    cannot (and must not be expected to) drive it.
  await expect(page.getByTestId("validation-status")).toHaveText("Waiting for render…");
});
