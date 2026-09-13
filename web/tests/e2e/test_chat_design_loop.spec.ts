/**
 * E2E integration spec (issue #54, local-only, NOT in CI):
 *
 *   seed a project with a known-passing fixture (photo + stated_dims)
 *     → send a real chat message via POST /api/projects/{id}/chat
 *     → wait for SSE completion (the design loop runs in a background
 *       task and streams progress/token/done frames)
 *     → assert a version was created (the loop passed)
 *     → assert the assistant bubble received the response (token frame)
 *
 * Determinism notes:
 *   - No LLM calls, no real Docker render worker. The design loop is
 *     mocked at the `app.state.run_design_loop` seam — the production
 *     app wires the real loop, but this spec uses a test fixture that
 *     returns a pass result immediately.
 *   - The SSE stream is intercepted via `page.route` (the `e2eSseInterceptor`
 *     fixture) and fulfilled with a controlled, finite SSE byte string
 *     that matches the backend's frame contract (progress → token → done).
 *   - The chat POST is sent to the REAL (unintercepted) backend so the
 *     route's 202 response and the event-source registration are
 *     exercised end-to-end.
 */

import { test, expect } from "./fixtures";
import { readFileSync } from "node:fs";
import path from "node:path";

const FIXTURE_PHOTO_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "test-photo.png",
);

test("chat design loop: send message → SSE completion → version created", async ({
  page,
  e2eApi,
  e2eSseInterceptor,
  e2eSseDocument,
}) => {
  // --- Seed: create a project via the real backend API -------------------
  const project = await e2eApi.createProject(`e2e-chat-${Date.now()}`);
  const projectId = project.id;

  // Upload a reference photo (the known-passing fixture).
  const photoBuffer = readFileSync(FIXTURE_PHOTO_PATH);
  const formData = new FormData();
  formData.append("file", new File([photoBuffer], "test-photo.png", {
    type: "image/png",
  }));
  const uploadRes = await fetch(`${process.env.E2E_BASE_URL ?? "http://localhost:8080"}/api/projects/${projectId}/photos`, {
    method: "POST",
    body: formData,
  });
  expect(uploadRes.status).toBe(201);

  // --- Interceptor: the SSE stream (finite, well-formed) ------------------
  // The backend's event source is registered synchronously before the 202
  // response, so the SSE stream will find it. The interceptor fulfils the
  // stream with a controlled SSE document that matches the backend's frame
  // contract: progress → token → done.
  const sseBody = e2eSseDocument([
    { event: "progress", data: { step: "design-loop-start" } },
    { event: "progress", data: { step: "design-loop-pass" } },
    { event: "progress", data: { step: "version-created", version_id: 1 } },
    { event: "token", data: { text: "W = 20; H = 25; D = 30; cube([W, H, D]);" } },
    { event: "done", data: { message: "Design loop passed validation" } },
  ]);
  await e2eSseInterceptor(projectId, [
    { event: "progress", data: { step: "design-loop-start" } },
    { event: "progress", data: { step: "design-loop-pass" } },
    { event: "progress", data: { step: "version-created", version_id: 1 } },
    { event: "token", data: { text: "W = 20; H = 25; D = 30; cube([W, H, D]);" } },
    { event: "done", data: { message: "Design loop passed validation" } },
  ]);

  // --- SPA boot: the app auto-creates a project on mount ------------------
  await page.goto("/");
  await expect(page.getByTestId("app-shell")).toBeVisible();

  // The SPA auto-creates a project on mount. We need to use the SAME
  // project id that the SPA created, not the one we created via the API
  // (the SPA doesn't know about the API-created project). Instead, we
  // observe the SPA's auto-created project from the network stream.
  const spaProjectId = await page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes("/api/projects"),
    { timeout: 15_000 },
  );
  expect(spaProjectId.status()).toBe(201);
  const spaProject = (await spaProjectId.json()) as { id: number };
  const spaPid = spaProject.id;

  // Re-intercept the SSE stream for the SPA's project (the one the chat
  // message will target).
  await e2eSseInterceptor(spaPid, [
    { event: "progress", data: { step: "design-loop-start" } },
    { event: "progress", data: { step: "design-loop-pass" } },
    { event: "progress", data: { step: "version-created", version_id: 1 } },
    { event: "token", data: { text: "W = 20; H = 25; D = 30; cube([W, H, D]);" } },
    { event: "done", data: { message: "Design loop passed validation" } },
  ]);

  // Upload a photo to the SPA's project (the chat message needs a photo).
  const spaUploadRes = await fetch(`${process.env.E2E_BASE_URL ?? "http://localhost:8080"}/api/projects/${spaPid}/photos`, {
    method: "POST",
    body: (() => {
      const fd = new FormData();
      fd.append("file", new File([photoBuffer], "test-photo.png", { type: "image/png" }));
      return fd;
    })(),
  });
  expect(spaUploadRes.status).toBe(201);

  // --- Send a chat message -------------------------------------------------
  // The SPA's handleSendMessage calls POST /chat BEFORE opening the SSE
  // stream. The send button is disabled while in flight (client-side
  // guard).
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
  // token frame text (the SCAD source).
  // Wait for the assistant bubble to receive the token text.
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
  // flag released).
  await expect
    .poll(async () => {
      const btn = page.getByTestId("chat-send-btn");
      return await btn.isEnabled();
    }, { timeout: 10_000 })
    .toBe(true);

  // --- Assert a version was created ---------------------------------------
  // The design loop passed → a version was created (the version-created
  // progress frame was emitted). Check via the API.
  const versions = await e2eApi.listVersions(spaPid);
  expect(versions.length).toBeGreaterThanOrEqual(1);
  const latest = versions[versions.length - 1] as {
    name: string;
    params: Record<string, unknown>;
  };
  // The version name is "design" (set by the chat wiring).
  expect(latest.name).toBe("design");
});
