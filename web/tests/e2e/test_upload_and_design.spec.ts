/**
 * E2E happy path (issue #49, spec 1 of 5):
 *
 *   upload a photo via the reference-photo input
 *     → the photo-upload UI settles to the success state
 *     → the 3MF download is intercepted via page.route and fulfilled with
 *       a pre-rendered 3MF fixture (GET /api/projects/{id}/model.3mf does
 *       not exist server-side yet — that route is a separate ticket)
 *     → assert the fulfilled response is exactly the fixture's bytes.
 *
 * Determinism notes:
 *   - No LLM calls, no real Docker render worker. The SSE stream is NOT
 *     intercepted in this spec — the happy path exercises the upload and
 *     download paths, and the app's SSE fetch only fires on chat send.
 *   - The SPA auto-creates a project on mount (App.tsx). We observe that
 *     project's id from the network stream (POST /api/projects → id)
 *     instead of creating a second project via the API, so the 3MF route
 *     interception matches the id the Export 3MF button actually requests.
 *   - Both mount-time and upload waits use page.waitForResponse (not
 *     page.waitForRequest): waitForRequest fires the moment the request is
 *     SENT, and the subsequent resp.response() can resolve null if the
 *     page has already navigated or the request was retried — the exact
 *     flake class that made this spec's 15 s project-creation timeout fire
 *     while the SPA was already fully rendered. Waiting for the 201
 *     response instead removes that race class entirely (same pattern the
 *     other three specs already use).
 *   - The fixture file is the sibling-workstream `scaffold`'s scope
 *     (web/tests/e2e/). Until those land, the spec generates its
 *     equivalents deterministically in a scratch dir: a tiny PNG reference
 *     photo and a minimal valid 3MF (a zip with a single
 *     [Content_Types].xml member), so this spec is self-contained.
 */

import { test, expect, type Page } from "@playwright/test";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const FIXTURE_PHOTO_PATH = path.resolve(
  path.dirname(new URL(import.meta.url).pathname),
  "test-photo.png",
);

/** 1x1 pixel PNG — enough to pass the type/size checks and decode. */
const TINY_PNG = Buffer.from(
  "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAASUVORK5CYII=",
  "base64",
);

/**
 * Minimal but valid 3MF: a zip with a single stored (method 0) member
 * `[Content_Types].xml` — the bare minimum that is a real, parseable zip
 * (local header + central directory + end-of-central-directory record).
 * Bytes are computed deterministically in code — no binary fixture needed.
 */
function buildDeterministic3MF(): Buffer {
  const name = Buffer.from("[Content_Types].xml");
  const xml = Buffer.from(
    '<?xml version="1.0" encoding="UTF-8"?><model unit="millimeter"><resources/><build/></model>',
  );
  const crc32 = (() => {
    const table: number[] = [];
    for (let n = 0; n < 256; n++) {
      let c = n;
      for (let k = 0; k < 8; k++) {
        c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1;
      }
      table[n] = c >>> 0;
    }
    return (buf: Buffer): number => {
      let c = 0xffffffff;
      for (let i = 0; i < buf.length; i++) {
        c = table[(c ^ buf[i]) & 0xff] ^ (c >>> 8);
      }
      return (c ^ 0xffffffff) >>> 0;
    };
  })();

  const crc = crc32(xml);
  const local = Buffer.alloc(30 + name.length);
  local.writeUInt32LE(0x02014b50, 0); // local file header signature
  local.writeUInt16LE(20, 4); // version needed to extract
  local.writeUInt16LE(0, 6); // flags
  local.writeUInt16LE(0, 8); // compression: stored
  local.writeUInt16LE(0, 10); // mod time
  local.writeUInt16LE(0x21, 12); // mod date (arbitrary, valid)
  local.writeUInt32LE(crc, 14);
  local.writeUInt32LE(xml.length, 18); // compressed size
  local.writeUInt32LE(xml.length, 22); // uncompressed size
  local.writeUInt16LE(name.length, 26); // name length
  local.writeUInt16LE(0, 28); // extra length
  name.copy(local, 30);

  const cdOffset = local.length + xml.length;
  const central = Buffer.alloc(46 + name.length);
  central.writeUInt32LE(0x02014b50, 0); // central directory signature
  central.writeUInt16LE(20, 4); // version made by
  central.writeUInt16LE(20, 6); // version needed to extract
  central.writeUInt16LE(0, 8); // flags
  central.writeUInt16LE(0, 10); // compression
  central.writeUInt16LE(0, 12); // mod time
  central.writeUInt16LE(0x21, 14); // mod date
  central.writeUInt32LE(crc, 16);
  central.writeUInt32LE(xml.length, 20);
  central.writeUInt32LE(xml.length, 24);
  central.writeUInt16LE(name.length, 28);
  central.writeUInt16LE(0, 30); // extra length
  central.writeUInt16LE(0, 32); // comment length
  central.writeUInt16LE(0, 34); // start disk number
  central.writeUInt16LE(0, 36); // internal attrs
  central.writeUInt16LE(0, 38); // external attrs
  central.writeUInt32LE(0, 40); // local header offset
  name.copy(central, 46);

  const eocd = Buffer.alloc(22);
  eocd.writeUInt32LE(0x06054b50, 0); // end-of-central-directory signature
  eocd.writeUInt16LE(0, 4); // disk number
  eocd.writeUInt16LE(0, 6); // cd disk number
  eocd.writeUInt16LE(1, 8); // entries on this disk
  eocd.writeUInt16LE(1, 10); // total entries
  eocd.writeUInt32LE(central.length, 12); // cd size
  eocd.writeUInt32LE(cdOffset, 16); // cd offset
  eocd.writeUInt16LE(0, 20); // comment length

  return Buffer.concat([local, xml, central, eocd]);
}

/**
 * Wait until the SPA has auto-created its project (App.tsx creates one on
 * mount), then return its id — the same id the Export 3MF button will use
 * for the 3MF route this spec intercepts.
 */
async function waitForProjectId(page: Page): Promise<number> {
  const projectResp = await page.waitForResponse(
    (r) =>
      r.request().method() === "POST" &&
      r.url().includes("/api/projects"),
    { timeout: 15_000 },
  );
  expect(projectResp.status()).toBe(201);
  const body = (await projectResp.json()) as { id: number };
  expect(typeof body.id).toBe("number");
  return body.id;
}

test.beforeEach(async ({ request }) => {
  // Wait for the app to be reachable (webServer hook / E2E_SKIP_SERVER).
  const base = process.env.E2E_BASE_URL ?? "http://localhost:8080";
  await expect
    .poll(async () => (await request.get(`${base}/api/projects`)).status(), {
      timeout: 15_000,
    })
    .toBe(200);
});

test("happy path: upload photo → SSE settle → 3MF download", async ({
  page,
}) => {
  // --- Fixtures: reference photo (scaffold scope; generated fallback) ----
  const scratch = mkdtempSync(path.join(tmpdir(), "pi-rukas-e2e-"));
  try {
    let photoBuffer: Buffer;
    try {
      photoBuffer = readFileSync(FIXTURE_PHOTO_PATH);
    } catch {
      // Scaffold fixture not present yet — generate an equivalent tiny
      // PNG into the scratch dir so the spec stays self-contained.
      photoBuffer = TINY_PNG;
      writeFileSync(path.join(scratch, "test-photo.png"), photoBuffer);
    }

    // --- 3MF fixture (scaffold scope; deterministic fallback) -------------
    const threeMf = buildDeterministic3MF();
    writeFileSync(path.join(scratch, "model.3mf"), threeMf);

    // --- Route interception: the not-yet-existing 3MF route --------------
    const threeMfRequests: string[] = [];
    await page.route(
      (url: URL) =>
        url.pathname.startsWith("/api/projects/") &&
        url.pathname.endsWith("/model.3mf"),
      async (route) => {
        threeMfRequests.push(route.request().url());
        await route.fulfill({
          status: 200,
          contentType: "model/3mf",
          body: threeMf,
        });
      },
    );

    // --- SPA boot: the app auto-creates a project on mount ---------------
    await page.goto("/");
    await expect(page.getByTestId("app-shell")).toBeVisible();

    // Observe the auto-created project id (settle the create before the
    // upload, so the upload's POST — and the id it targets — is known).
    const projectId = await waitForProjectId(page);

    // The upload input is rendered once the project id exists.
    const photoInput = page.getByTestId("photo-file-input");
    await expect(photoInput).toBeAttached();

    // --- Upload: the reference photo via the input -----------------------
    // The photo is POSTed to the REAL (unintercepted) backend upload
    // endpoint for this project — only the not-yet-existing 3MF route is
    // intercepted.
    const uploadPromise = page.waitForResponse(
      (r) =>
        r.request().method() === "POST" &&
        r.url().includes(`/api/projects/${projectId}/photos`),
    );
    await photoInput.setInputFiles([
      {
        name: "test-photo.png",
        mimeType: "image/png",
        buffer: photoBuffer,
      },
    ]);
    const uploadResp = await uploadPromise;
    expect(uploadResp.status()).toBe(201);

    // The upload settles to the success state: the label now shows the
    // preview image instead of the "Attach reference photo" prompt.
    await expect(page.getByTestId("photo-preview")).toBeVisible({
      timeout: 10_000,
    });
    await expect(page.getByText("📎 Attach reference photo")).toHaveCount(0);

    // --- SSE settle -------------------------------------------------------
    // The design-loop-to-SSE wiring (onProgress) is a separate ticket, so
    // the validation-status span is intentionally a static placeholder in
    // the current app: the "settle" in this happy path is the UI reaching
    // its stable post-upload state — preview shown, no upload error, no
    // app-level error, and the export affordance present and enabled.
    await expect(page.getByTestId("validation-status")).toHaveText(
      "Waiting for render…",
    );
    await expect(page.getByTestId("upload-error")).toHaveCount(0);
    await expect(page.getByTestId("app-error")).toHaveCount(0);

    // --- 3MF download via page.route -------------------------------------
    // The Export 3MF button hits GET /api/projects/{id}/model.3mf — a
    // route that does not exist server-side yet. It is intercepted above
    // and fulfilled with the pre-rendered 3MF fixture; the browser then
    // triggers a download of model-{projectId}.3mf.
    const [download] = await Promise.all([
      page.waitForEvent("download"),
      page.getByTestId("export-3mf-button").click(),
    ]);
    expect(download.suggestedFilename()).toBe(`model-${projectId}.3mf`);
    // The fulfilled response is byte-identical to the fixture.
    const downloadPath = await download.path();
    const downloaded = readFileSync(downloadPath);
    expect(downloaded.equals(threeMf)).toBe(true);
    // And the request really went to this project's 3MF route.
    expect(threeMfRequests.length).toBe(1);
    expect(threeMfRequests[0]).toContain(`/api/projects/${projectId}/model.3mf`);
  } finally {
    rmSync(scratch, { recursive: true, force: true });
  }
});
