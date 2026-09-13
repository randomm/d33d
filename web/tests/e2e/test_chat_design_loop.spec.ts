/**
 * E2E integration spec (issue #54, local-only, NOT in CI):
 *
 *   seed the SPA's auto-created project with a reference photo (the
 *     known-passing fixture)
 *     → send a real chat message via POST /api/projects/{id}/chat
 *     → wait for SSE completion (the SSE stream is intercepted via
 *       page.route and fulfilled with a finite, well-formed document)
 *     → assert the assistant bubble received the token frame
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

import { test, expect } from "./fixtures";
import { readFileSync } from "node:fs";
import path from "node:path";

const FIXTURE_PHOTO_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "test-photo.png",
);

test("chat design loop: send message → SSE completion → assistant bubble receives token", async ({
  page,
  e2eSseInterceptor,
}) => {
  // --- SPA boot: the app auto-creates a project on mount ------------------
  await page.goto("/");
  await expect(page.getByTestId("app-shell")).toBeVisible();

  // Observe the SPA's auto-created project from the network stream. The
  // SPA does not deep-link to an existing project (single default project
  // per mount), so this is the only project id available.
  const spaProjectResp = await page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes("/api/projects"),
    { timeout: 15_000 },
  );
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
  await e2eSseInterceptor(spaPid, [
    { event: "progress", data: { step: "design-loop-start" } },
    { event: "progress", data: { step: "design-loop-pass" } },
    { event: "progress", data: { step: "version-created", version_id: 1 } },
    { event: "token", data: { text: "W = 20; H = 25; D = 30; cube([W, H, D]);" } },
    { event: "done", data: { message: "Design loop passed validation" } },
  ]);

  // --- Send a chat message -------------------------------------------------
  // The SPA's handleSendMessage calls POST /chat BEFORE opening the SSE
  // stream. The send button is disabled while in flight (client-side
  // guard — `inFlight` prop on ChatPanel).
  const chatInput = page.getByTestId("chat-input");
  await expect(chatInput).toBeAttached();

  // The send button should be enabled (not in flight).
  await expect(page.getByTestId("chat-send-btn")).toBeEnabled();

  // Type a message and send it.
  const sendPromise = page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes(`/api/projects/${spaPid}/chat`),
  );
  await chatInput.fill("Make a 20mm wide, 25mm high, 30mm deep box");
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

  // The send button is re-enabled after the stream completes (in-flight
  // flag released — the SPA's `setDesignLoopInFlight(false)` in the
  // `.finally` of the postChat chain).
  await expect
    .poll(async () => {
      const btn = page.getByTestId("chat-send-btn");
      return await btn.isEnabled();
    }, { timeout: 10_000 })
    .toBe(true);
});
