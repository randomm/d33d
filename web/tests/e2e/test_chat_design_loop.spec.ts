/**
 * E2E integration spec (issue #54, local-only, NOT in CI):
 *
 *   seed the SPA's auto-created project with a reference photo (the
 *     known-passing fixture)
 *     → send a real chat message via POST /api/projects/{id}/chat
 *     → wait for SSE completion (the SSE stream is intercepted via
 *       page.route and fulfilled with a finite, well-formed document)
 *     → assert the assistant bubble received the token frame
 *     → assert the viewer pane displays geometry after the stream
 *       completes (the version-created frame's stl_data_uri drives
 *       the STL swap)
 *     → assert the send button is re-enabled (in-flight flag released)
 *
 * Determinism notes:
 *   - The SPA auto-creates a project on mount (App.tsx — the single
 *     default project; there is no deep-link route yet). This spec does
 *     NOT create a project via the API first — it observes the SPA's
 *     auto-created project from the network stream and uses that id for
 *     everything else (the earlier draft created a dead API project and
 *     then discovered the SPA's different one; that dead setup is gone).
 *   - The SSE stream is intercepted via `page.route` (the
 *     `e2eSseInterceptor` fixture) and fulfilled with a controlled,
 *     finite SSE byte string that matches the backend's frame contract
 *     (progress → token → done). The SPA's `ApiClient.streamEvents`
 *     consumes this intercepted stream — NOT the real backend's event
 *     source — so the version-creation assertion below checks the
 *     INTERCEPTED frame the SPA consumed, not a real version row in the
 *     backend. (The backend's real design loop is not mocked in this
 *     spec — there is no `app.state.run_design_loop` override; the
 *     interception happens at the HTTP/SSE layer, between the SPA and
 *     the backend. The earlier draft's docstring claimed a mock that
 *     does not exist.)
 *   - The chat POST is sent to the REAL (unintercepted) backend so the
 *     route's 202 response and the event-source registration are
 *     exercised end-to-end. The POST's 202 + the route's in-flight flag
 *     lifecycle are the real backend contract this spec pins.
 *   - The `version-created` progress frame is asserted in the INTERCEPTED
 *     SSE document (the SPA consumed it); the backend's real
 *     `versions` table is NOT asserted on, because the real design loop
 *     is not running in this spec (the SSE stream is intercepted before
 *     the real loop's frames would reach the SPA).
 *   - The version-timeline side rail is NOT asserted on for the same
 *     reason — the SPA's `listVersions` call would see an empty timeline
 *     (no real version was created by a real loop).
 */

import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
import path from "node:path";

const FIXTURE_PHOTO_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "test-photo.png",
);

/** Build one well-formed SSE frame, matching d33d/streaming.py's
 *  format_sse: an `event:` line, a `data:` line, then the blank line that
 *  terminates the frame. The SPA's streamEvents reader resolves on the
 *  terminal frame, so a finite body is a complete stream. */
function sseBody(frames: Array<{ event: string; data: Record<string, unknown> }>): string {
  return frames
    .map((f) => `event: ${f.event}\ndata: ${JSON.stringify(f.data)}\n\n`)
    .join("");
}

/** The viewer's uniform clear colour (0x1a1a2e). A pixel with all three
 *  RGB channels equal to this is background — geometry is present iff at
 *  least one probed pixel differs. */
function isBackgroundPixel(r: number, g: number, b: number): boolean {
  return r === 26 && g === 26 && b === 46;
}

test("chat design loop: send message → SSE completion → assistant bubble receives token", async ({
  page,
}) => {
  // --- SPA boot: the app auto-creates a project on mount ------------------
  // Arm the response listener BEFORE navigation — Playwright only records
  // responses observed from the moment the promise is created, and the
  // mount-time `POST /api/projects` fires during `goto` (the SPA's
  // App.tsx `createProject` effect runs on the first render, before the
  // app-shell is painted). Waiting for the shell first would miss it.
  const spaProjectPromise = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes("/api/projects"),
    { timeout: 30_000 },
  );
  await page.goto("/");
  await expect(page.getByTestId("app-shell")).toBeVisible();

  // Observe the SPA's auto-created project from the network stream. The
  // SPA does not deep-link to an existing project (single default project
  // per mount), so this is the only project id available.
  const spaProjectResp = await spaProjectPromise;
  expect(spaProjectResp.status()).toBe(201);
  const spaProject = (await spaProjectResp.json()) as { id: number };
  const spaPid = spaProject.id;

  // --- Seed: upload a reference photo to the SPA's project ----------------
  // The chat route reads the project's `source_photo_path` and emits it
  // as a data URI; a missing photo falls back to the 1x1 transparent-PNG
  // constant (never None). The spec uploads the real fixture so the data
  // URI path is exercised, not the fallback.
  const photoBuffer = readFileSync(FIXTURE_PHOTO_PATH);
  const base = process.env.E2E_BASE_URL ?? "http://localhost:8080";
  const uploadRes = await fetch(`${base}/api/projects/${spaPid}/photos`, {
    method: "POST",
    body: (() => {
      const fd = new FormData();
      fd.append("file", new File([photoBuffer], "test-photo.png", { type: "image/png" }));
      return fd;
    })(),
  });
  expect(uploadRes.status).toBe(201);

  // --- Interceptor: the SSE stream (finite, well-formed) ------------------
  // The interceptor fulfils the SPA's `GET /api/stream/{spaPid}` with a
  // controlled SSE document matching the backend's frame contract:
  // progress → progress → progress → token → done. The `version-created`
  // progress frame is part of this document — it is what the SPA
  // consumed (asserted below), NOT a real version row in the backend.
  // The version-created frame carries the pass's render payload (issue
  // #69): `stl_data_uri`, a real base64 data URI of the on-disk minimal
  // STL fixture (web/tests/fixtures/viewer/mini-box.stl, the same bytes
  // the viewer's own unit tests load), plus a `views` map. The SPA
  // decodes stl_data_uri and swaps the GLB fixture for the streamed STL —
  // the viewer-geometry assertion below pins that end-to-end.
  const stlBytes = readFileSync(
    path.resolve(
      path.dirname(new URL(import.meta.url).pathname),
      "../fixtures/viewer/mini-box.stl",
    ),
  );
  const stlDataUri = `data:application/octet-stream;base64,${stlBytes.toString("base64")}`;
  const sseFrames = [
    { event: "progress", data: { step: "design-loop-start" } },
    { event: "progress", data: { step: "design-loop-pass" } },
    {
      event: "progress",
      data: { step: "version-created", version_id: 1, stl_data_uri: stlDataUri, views: {} },
    },
    { event: "token", data: { text: "W = 20; H = 25; D = 30; cube([W, H, D]);" } },
    { event: "done", data: { message: "Design loop passed validation" } },
  ];
  await page.route(`**/api/stream/${spaPid}`, (route) =>
    route.fulfill({
      status: 200,
      contentType: "text/event-stream",
      body: sseBody(sseFrames),
    }),
  );

  // --- Send a chat message -------------------------------------------------
  // The SPA's handleSendMessage calls POST /chat BEFORE opening the SSE
  // stream. The send button is disabled while in flight (client-side
  // guard — `inFlight` prop on ChatPanel). The button is ALSO disabled
  // while the input is empty (disabled={!input.trim() || inFlight}), so
  // it must be asserted enabled AFTER the fill, not before.
  const chatInput = page.getByTestId("chat-input");
  await expect(chatInput).toBeAttached();

  // Type a message; the button becomes enabled once the input is non-empty.
  await chatInput.fill("Make a 20mm wide, 25mm high, 30mm deep box");
  await expect(page.getByTestId("chat-send-btn")).toBeEnabled();

  // Send it.
  const sendPromise = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes(`/api/projects/${spaPid}/chat`),
  );
  await page.getByTestId("chat-send-btn").click();

  // The POST /chat returns 202 Accepted.
  const chatResp = await sendPromise;
  expect(chatResp.status()).toBe(202);
  const chatBody = (await chatResp.json()) as { status: string };
  expect(chatBody.status).toBe("accepted");

  // The user bubble shows the message.
  await expect(page.getByTestId("chat-msg-user")).toContainText(
    "Make a 20mm wide, 25mm high, 30mm deep box",
  );

  // The SSE stream delivers the frames. The assistant bubble receives the
  // token frame text (the SCAD source). The SPA's onToken appends to the
  // streaming assistant bubble, so the single token frame fills it.
  await expect
    .poll(
      () => {
        const els = page.locator('[data-testid="chat-msg-assistant"]');
        return els.count();
      },
      { timeout: 10_000 },
    )
    .toBeGreaterThan(0);

  await expect(page.locator('[data-testid="chat-msg-assistant"]').last()).toContainText(
    "W = 20; H = 25; D = 30; cube([W, H, D]);",
    { timeout: 10_000 },
  );

  // The viewer pane displays geometry after the stream completes: the
  // version-created frame's stl_data_uri (real mini-box.stl bytes as a
  // base64 data URI) is decoded by the SPA and swapped in for the GLB
  // fixture — the three.js canvas then renders the streamed STL mesh.
  // Read the WebGL canvas back via drawImage → getImageData and assert the
  // frame is no longer uniform background (0x1a1a2e = rgb(26,26,46)) —
  // geometry occupies part of the viewport, so some probed pixel differs.
  const probePixels = () =>
    page.locator('[data-testid="viewer-pane"] canvas').evaluate((el) => {
      const canvas = el as HTMLCanvasElement;
      const probe = document.createElement("canvas");
      probe.width = 8;
      probe.height = 8;
      const ctx = probe.getContext("2d");
      if (!ctx) return "";
      try {
        ctx.drawImage(canvas, 0, 0, 8, 8);
        return ctx.getImageData(0, 0, 8, 8).data.join(",");
      } catch {
        return "";
      }
    });
  await expect
    .poll(async () => {
      const px = (await probePixels()).split(",").map(Number);
      // Geometry is present iff at least one probed pixel is not the
      // uniform viewer clear colour.
      for (let i = 0; i < px.length; i += 4) {
        if (!isBackgroundPixel(px[i], px[i + 1], px[i + 2])) return true;
      }
      return false;
    }, { timeout: 10_000, message: "viewer canvas never rendered the streamed STL" })
    .toBe(true);

  // The send button stays DISABLED until the stream completes: the input
  // was cleared on send (handleSubmit does setInput("")), and the button
  // is disabled when the input is empty OR in-flight — both true here.
  // Once the intercepted stream's `done` frame resolves, the SPA's
  // `.finally` releases the in-flight flag; the button remains disabled
  // only until the operator types again (empty input). We assert the
  // in-flight flag was released by typing a probe character and
  // re-asserting enabled.
  await page.getByTestId("chat-send-btn").waitFor({ state: "attached" });
  // Type a probe character to give the button a non-empty input; if the
  // in-flight flag is still set, the button stays disabled.
  await page.getByTestId("chat-input").fill("x");
  await expect
    .poll(async () => {
      const btn = page.getByTestId("chat-send-btn");
      return await btn.isEnabled();
    }, { timeout: 10_000 })
    .toBe(true);
  // Clear the probe — don't leave the input dirty.
  await page.getByTestId("chat-input").fill("");
});
