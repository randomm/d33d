/**
 * Unit tests for the typed backend API client (issue #6, task-b).
 *
 * Pure fetch-level: a fake `fetch` records the outgoing RequestInit and
 * plays back scripted Response objects. No network, no server — the backend
 * contract shapes are copied from d33d/projects.py, d33d/app.py and
 * d33d/streaming.py (PR #24 / 4783f99).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ApiClient,
  ApiError,
  STREAM_TOTAL_TIMEOUT_MS,
  type Credential,
  type ModelCatalogue,
  type PhotoUploadResult,
  type Project,
  type RegionEditRequest,
  type RegionEditResult,
  type StreamEvent,
} from "../api.js";

// ---------------------------------------------------------------------------
// Fake fetch harness
// ---------------------------------------------------------------------------

type CallRecord = { url: string; init: RequestInit };

class FakeFetch {
  calls: CallRecord[] = [];
  private queue: Array<Response | (() => Response)> = [];
  private _impl: (url: string, init: RequestInit) => Promise<Response>;

  constructor() {
    this._impl = (url: string, init: RequestInit): Promise<Response> => {
      this.calls.push({ url, init });
      const next = this.queue.shift();
      if (next === undefined) {
        throw new Error(`FakeFetch: no response queued for ${url}`);
      }
      const res = typeof next === "function" ? next() : next;
      return Promise.resolve(res);
    };
  }

  /** Replace the fetch behaviour (e.g. to simulate abort rejection). */
  replace(impl: (url: string, init: RequestInit) => Promise<Response>): this {
    this._impl = impl;
    return this;
  }

  enqueue(res: Response | (() => Response)): this {
    this.queue.push(res);
    return this;
  }

  handler = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const url = typeof input === "string" ? input : input.toString();
    return this._impl(url, init ?? {});
  };
}

function json(status: number, body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

const CATALOGUE: ModelCatalogue = {
  source: "/data/models.yaml",
  providers: {
    main: {
      name: "main",
      base: "https://llm.trailopeners.com/v1",
      key: "***redacted***",
      defaults: {},
    },
  },
  models: [
    {
      id: "qwen27",
      provider: "main",
      model: "RedHatAI/Qwen3.8-27B-INT4",
      context_window: 262144,
      params: {},
      fallbacks: [],
      retries: { max: 3 },
    },
  ],
  roles: { design: "qwen27" },
};

const PROJECT: Project = {
  id: 1,
  name: "filigree earring",
  git_repo_path: "/data/projects/1",
  tags: ["earring"],
  notes: "from photo",
  source_photo_path: null,
  created_at: "2026-01-01T00:00:00Z",
};

let fake: FakeFetch;
let client: ApiClient;

beforeEach(() => {
  fake = new FakeFetch();
  client = new ApiClient({ baseUrl: "http://api.test", fetch: fake.handler });
});

afterEach(() => {
  vi.restoreAllMocks();
});

function lastCall(): CallRecord {
  const c = fake.calls.at(-1);
  expect(c).toBeDefined();
  return c!;
}

// ---------------------------------------------------------------------------
// Project CRUD
// ---------------------------------------------------------------------------

describe("project CRUD", () => {
  it("createProject POSTs JSON to /api/projects", async () => {
    fake.enqueue(json(201, PROJECT));
    const p = await client.createProject({ name: "filigree earring", tags: ["earring"] });
    expect(p.id).toBe(1);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual({
      name: "filigree earring",
      tags: ["earring"],
    });
  });

  it("listProjects GETs /api/projects", async () => {
    fake.enqueue(json(200, [PROJECT]));
    const list = await client.listProjects();
    expect(list).toHaveLength(1);
    expect(lastCall().url).toBe("http://api.test/api/projects");
    expect(lastCall().init.method).toBe("GET");
  });

  it("getProject GETs /api/projects/{id}", async () => {
    fake.enqueue(json(200, PROJECT));
    await client.getProject(1);
    expect(lastCall().url).toBe("http://api.test/api/projects/1");
  });

  it("updateProject PATCHes with partial body", async () => {
    fake.enqueue(json(200, { ...PROJECT, name: "renamed" }));
    const p = await client.updateProject(1, { name: "renamed" });
    expect(p.name).toBe("renamed");
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1");
    expect(init.method).toBe("PATCH");
    expect(JSON.parse(init.body as string)).toEqual({ name: "renamed" });
  });

  it("deleteProject DELETEs and resolves on 204", async () => {
    fake.enqueue(new Response(null, { status: 204 }));
    await expect(client.deleteProject(1)).resolves.toBeUndefined();
    expect(lastCall().init.method).toBe("DELETE");
  });

  it("getProject surfaces FastAPI 404 detail", async () => {
    fake.enqueue(json(404, { detail: "project not found" }));
    await expect(client.getProject(99)).rejects.toMatchObject({
      status: 404,
      detail: "project not found",
    });
  });

  it("upload failures carry the server error shape", async () => {
    fake.enqueue(json(413, { detail: "file exceeds 20971520 byte limit" }));
    const f = new File([new ArrayBuffer(1)], "big.png", { type: "image/png" });
    await expect(client.uploadPhoto(1, f)).rejects.toMatchObject({
      status: 413,
      detail: "file exceeds 20971520 byte limit",
    });
  });

  it("non-JSON error bodies fall back to raw text", async () => {
    fake.enqueue(new Response("Internal Server Error", { status: 500 }));
    await expect(client.getProject(1)).rejects.toMatchObject({
      status: 500,
      detail: "Internal Server Error",
    });
  });
});

// ---------------------------------------------------------------------------
// Photo upload
// ---------------------------------------------------------------------------

describe("photo upload", () => {
  it("POSTs multipart to /api/projects/{id}/photos with a file field", async () => {
    fake.enqueue(
      json(201, {
        id: 1,
        source_photo_path: "/data/projects/1/photos/photo.png",
        size: 42,
      } satisfies PhotoUploadResult),
    );
    const f = new File([new Uint8Array(42)], "photo.png", { type: "image/png" });
    const r = await client.uploadPhoto(1, f);
    expect(r.size).toBe(42);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/photos");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const field = (init.body as FormData).get("file");
    expect(field).toBeInstanceOf(File);
    expect((field as File).name).toBe("photo.png");
  });

  it("passes through an AbortSignal for cancellation", async () => {
    const signal = new AbortController().signal;
    fake.enqueue(json(201, { id: 1, source_photo_path: "p", size: 1 }));
    const f = new File([new Uint8Array(1)], "a.jpg", { type: "image/jpeg" });
    await client.uploadPhoto(1, f, signal);
    expect(lastCall().init.signal).toBe(signal);
  });

  it("rejects a non-image File before any request is made", async () => {
    const f = new File([new Uint8Array(1)], "a.txt", { type: "text/plain" });
    await expect(client.uploadPhoto(1, f)).rejects.toBeInstanceOf(ApiError);
    expect(fake.calls).toHaveLength(0);
  });
});

// ---------------------------------------------------------------------------
// Part upload + unit settlement + part.stl (issue #334)
// ---------------------------------------------------------------------------

describe("part upload (issue #334)", () => {
  const PART = {
    filename: "box.stl",
    format: "stl",
    unit: "mm",
    unit_status: "assumed",
    scale: 1,
    report: {
      triangles: 12,
      bodies: 1,
      watertight: true,
      gaps_closed: 0,
      bbox_file_units: [20, 20, 20],
    },
    options: null,
  };

  it("uploadPart POSTs multipart to /api/projects/{id}/part with a file field", async () => {
    fake.enqueue(json(201, { id: 1, version_id: 1, part: PART }));
    const f = new File([new Uint8Array(42)], "box.stl", { type: "model/stl" });
    const r = await client.uploadPart(1, f);
    expect(r.version_id).toBe(1);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/part");
    expect(init.method).toBe("POST");
    expect(init.body).toBeInstanceOf(FormData);
    const field = (init.body as FormData).get("file");
    expect(field).toBeInstanceOf(File);
    expect((field as File).name).toBe("box.stl");
  });

  it("uploadPart rejects a non-.stl/.3mf file before any request is made", async () => {
    const f = new File([new Uint8Array(1)], "photo.png", { type: "image/png" });
    await expect(client.uploadPart(1, f)).rejects.toBeInstanceOf(ApiError);
    expect(fake.calls).toHaveLength(0);
  });

  it("uploadPart surfaces the 409 part_exists detail", async () => {
    fake.enqueue(json(409, { detail: "A part already exists: 'box.stl'" }));
    const f = new File([new Uint8Array(1)], "dup.stl", { type: "model/stl" });
    await expect(client.uploadPart(1, f)).rejects.toMatchObject({
      status: 409,
      detail: "A part already exists: 'box.stl'",
    });
  });

  it("setPartUnit POSTs {unit} to /api/projects/{id}/part/units", async () => {
    fake.enqueue(json(200, { id: 1, part: PART, bbox_mm: [20, 20, 20] }));
    await client.setPartUnit(1, "inch");
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/part/units");
    expect(JSON.parse(init.body as string)).toEqual({ unit: "inch" });
  });

  it("setPartAxisMeasurement POSTs {axis, mm} to /api/projects/{id}/part/units", async () => {
    fake.enqueue(json(200, { id: 1, part: PART, bbox_mm: [10, 20, 20] }));
    await client.setPartAxisMeasurement(1, "D", 42);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/part/units");
    expect(JSON.parse(init.body as string)).toEqual({ axis: "D", mm: 42 });
  });

  it("fetchPartStl GETs /api/projects/{id}/part.stl and resolves the ArrayBuffer", async () => {
    const bytes = new Uint8Array([0, 1, 2, 3]);
    fake.enqueue(new Response(bytes, { status: 200, headers: { "Content-Type": "model/stl" } }));
    const buf = await client.fetchPartStl(1);
    expect(buf).toBeInstanceOf(ArrayBuffer);
    expect(new Uint8Array(buf)).toEqual(bytes);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/part.stl");
    expect(init.method).toBe("GET");
  });

  it("fetchPartStl surfaces a 409 (the source-missing shape)", async () => {
    fake.enqueue(json(409, { detail: { code: "source_missing", message: "the part file is not on disk" } }));
    await expect(client.fetchPartStl(1)).rejects.toMatchObject({ status: 409 });
  });
});

// ---------------------------------------------------------------------------
// Region-scoped edit request (issue #7, task-c; design-loop wiring by
// issue #68)
// ---------------------------------------------------------------------------

const REGION_EDIT_REQUEST: RegionEditRequest = {
  module_ids: ["curl_3", "curl_4"],
  view_id: "front",
  marked_png_base64: "aGVsbG8=",
  point: { x: 300, y: 200 },
  instruction: "open up this spiral, it's too tight to print",
};

const REGION_EDIT_RESULT: RegionEditResult = {
  project_id: 1,
  status: "accepted",
};

describe("region-scoped edit request", () => {
  it("createRegionEdit POSTs JSON to /api/projects/{id}/region-edits", async () => {
    fake.enqueue(json(202, REGION_EDIT_RESULT));
    const r = await client.createRegionEdit(1, REGION_EDIT_REQUEST);
    expect(r.status).toBe("accepted");
    // The 202 body is exactly {project_id, status} — no detail/module_ids/
    // view_id echo (the version arrives only via the SSE stream).
    expect(r).toEqual({ project_id: 1, status: "accepted" });

    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/region-edits");
    expect(init.method).toBe("POST");
    expect(init.headers).toEqual({ "Content-Type": "application/json" });
    expect(JSON.parse(init.body as string)).toEqual(REGION_EDIT_REQUEST);
  });

  it("resolves only on 202 (the accepted status, mirroring /chat)", async () => {
    fake.enqueue(json(202, REGION_EDIT_RESULT));
    await expect(
      client.createRegionEdit(1, REGION_EDIT_REQUEST),
    ).resolves.toMatchObject({ status: "accepted" });
  });

  it("surfaces a 404 for an unknown project", async () => {
    fake.enqueue(json(404, { detail: "project not found" }));
    await expect(
      client.createRegionEdit(999, REGION_EDIT_REQUEST),
    ).rejects.toMatchObject({ status: 404, detail: "project not found" });
  });

  it("surfaces a 422 validation error (e.g. an unknown view_id)", async () => {
    fake.enqueue(
      json(422, { detail: [{ msg: "view_id must be one of the render-worker views" }] }),
    );
    await expect(
      client.createRegionEdit(1, { ...REGION_EDIT_REQUEST, view_id: "bottom-left" as never }),
    ).rejects.toBeInstanceOf(ApiError);
  });

  it("never fabricates a version/result in the 202 body", async () => {
    fake.enqueue(json(202, REGION_EDIT_RESULT));
    const r = await client.createRegionEdit(1, REGION_EDIT_REQUEST);
    // The version arrives only via the SSE stream's version-created frame —
    // the 202 body has no field for it.
    expect(r).not.toHaveProperty("scad");
    expect(r).not.toHaveProperty("result");
    expect(r).not.toHaveProperty("version_id");
  });
});

// ---------------------------------------------------------------------------
// Model config + credentials
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// Design state (issue #237, task-b)
// ---------------------------------------------------------------------------

describe("getDesignState", () => {
  it("GETs /api/projects/{id}/design-state and resolves the envelope", async () => {
    const envelope = {
      entries: [
        {
          name: "outer_diameter",
          label: "Outer diameter",
          value: 20,
          unit: "mm",
          provenance: "stated",
        },
        {
          name: "wire_gauge",
          label: "Wire gauge",
          value: null,
          unit: null,
          provenance: "unknown",
        },
      ],
      history_missing: false,
    };
    fake.enqueue(json(200, envelope));
    const result = await client.getDesignState(1);
    expect(result.entries).toHaveLength(2);
    expect(result.history_missing).toBe(false);
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/projects/1/design-state");
    expect(init.method).toBe("GET");
  });

  it("preserves a null value on unknown entries — never 0 (issue #91 contract)", async () => {
    fake.enqueue(
      json(200, {
        entries: [
          {
            name: "hole_diameter",
            label: "Hole diameter",
            value: null,
            unit: "mm",
            provenance: "unknown",
          },
        ],
        history_missing: false,
      }),
    );
    const [row] = (await client.getDesignState(1)).entries;
    expect(row?.value).toBeNull();
    expect(row?.value).not.toBe(0);
  });

  it("carries stated_value only on disagrees entries", async () => {
    fake.enqueue(
      json(200, {
        entries: [
          {
            name: "width",
            label: "Width",
            value: 18,
            unit: "mm",
            provenance: "disagrees",
            stated_value: 20,
          },
        ],
        history_missing: false,
      }),
    );
    const [row] = (await client.getDesignState(1)).entries;
    expect(row).toMatchObject({ value: 18, stated_value: 20 });
  });

  it("resolves an empty entries array when the project has no version yet", async () => {
    fake.enqueue(json(200, { entries: [], history_missing: false }));
    const result = await client.getDesignState(1);
    expect(result.entries).toEqual([]);
    expect(result.history_missing).toBe(false);
  });

  it("carries history_missing=true when the repo directory is absent (issue #316)", async () => {
    fake.enqueue(json(200, { entries: [], history_missing: true }));
    const result = await client.getDesignState(1);
    expect(result.history_missing).toBe(true);
    expect(result.entries).toEqual([]);
  });

  it("surfaces a 404 for an unknown project", async () => {
    fake.enqueue(json(404, { detail: "project not found" }));
    await expect(client.getDesignState(99)).rejects.toMatchObject({
      status: 404,
      detail: "project not found",
    });
  });

  it("surfaces a 500 with the non-JSON raw-text fallback", async () => {
    fake.enqueue(new Response("Internal Server Error", { status: 500 }));
    await expect(client.getDesignState(1)).rejects.toMatchObject({
      status: 500,
      detail: "Internal Server Error",
    });
  });

  it("the envelope's `part` deserialises as null when no part (issue #334)", async () => {
    fake.enqueue(json(200, { entries: [], history_missing: false, part: null }));
    const result = await client.getDesignState(1);
    expect(result.part).toBeNull();
  });

  it("the envelope's `part` carries unit_status/report/options when present (issue #334)", async () => {
    fake.enqueue(
      json(200, {
        entries: [],
        history_missing: false,
        part: {
          filename: "box.stl",
          format: "stl",
          unit: null,
          unit_status: "unsettled",
          scale: null,
          report: {
            triangles: 12,
            bodies: 2,
            watertight: false,
            gaps_closed: 1,
            bbox_file_units: [100, 100, 100],
          },
          options: [
            { unit: "mm", scale: 1, extents_mm: [100, 100, 100], fits_envelope: true, at_least_5mm: true },
          ],
        },
      }),
    );
    const result = await client.getDesignState(1);
    expect(result.part).not.toBeNull();
    expect(result.part?.unit_status).toBe("unsettled");
    expect(result.part?.report?.triangles).toBe(12);
    expect(result.part?.options).toHaveLength(1);
  });
});

describe("model config", () => {
  it("getModelConfig GETs /api/config/models", async () => {
    fake.enqueue(json(200, CATALOGUE));
    const c = await client.getModelConfig();
    expect(c.models[0]?.model).toBe("RedHatAI/Qwen3.8-27B-INT4");
    expect(lastCall().url).toBe("http://api.test/api/config/models");
  });

  it("putModelConfig PUTs raw YAML with text/yaml content type", async () => {
    fake.enqueue(json(200, CATALOGUE));
    const yaml = "providers:\n  main:\n    base: https://llm.trailopeners.com/v1\n";
    const c = await client.putModelConfig(yaml);
    expect(c.providers.main?.key).toBe("***redacted***");
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/config/models");
    expect(init.method).toBe("PUT");
    expect(init.headers).toEqual({ "Content-Type": "text/yaml" });
    expect(init.body).toBe(yaml);
  });

  it("surfaces the backend's literal-key rejection (security invariant)", async () => {
    fake.enqueue(
      json(
        400,
        {
          error:
            'provider key(s) for ["main"] must be an environment variable reference (e.g. "${SOME_VAR}")',
        },
      ),
    );
    await expect(
      client.putModelConfig("providers:\n  main:\n    key: sk-live-123\n"),
    ).rejects.toMatchObject({ status: 400 });
  });
});

describe("credentials", () => {
  it("listCredentials returns names only — no key material in the type", async () => {
    const creds: Credential[] = [
      { provider_id: "main", model_alias: "qwen27" },
    ];
    fake.enqueue(json(200, creds));
    const list = await client.listCredentials();
    expect(list).toEqual(creds);
    // Structural: the Credential type has no field for a secret/ciphertext.
    const keys = Object.keys(list[0]!);
    expect(keys.sort()).toEqual(["model_alias", "provider_id"]);
  });

  it("storeCredential POSTs the secret and resolves only with names (201)", async () => {
    fake.enqueue(
      json(201, { provider_id: "main", model_alias: "qwen27" }),
    );
    const r = await client.storeCredential({
      provider: "main",
      model_alias: "qwen27",
      secret: "sk-test-secret",
    });
    expect(r).toEqual({ provider_id: "main", model_alias: "qwen27" });
    const { url, init } = lastCall();
    expect(url).toBe("http://api.test/api/settings/credentials");
    expect(JSON.parse(init.body as string)).toEqual({
      provider: "main",
      model_alias: "qwen27",
      secret: "sk-test-secret",
    });
    // The secret must never appear in the response: the typed return value
    // cannot carry it (only provider_id / model_alias exist).
    expect(Object.keys(r)).not.toContain("secret");
    expect(JSON.stringify(r)).not.toContain("sk-test-secret");
  });
});

// ---------------------------------------------------------------------------
// SSE demux
// ---------------------------------------------------------------------------

function sseFrame(kind: string, data: Record<string, unknown>): string {
  return `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
}

function makeSseResponse(body: string): Response {
  // A ReadableStream over the full SSE payload, split on awkward boundaries
  // (mid-JSON, mid-line) to prove the reader is byte-offset safe.
  const encoder = new TextEncoder();
  const bytes = encoder.encode(body);
  // Split into chunks of 7 bytes to guarantee mid-frame chunking.
  const chunks: Uint8Array[] = [];
  for (let i = 0; i < bytes.length; i += 7) {
    chunks.push(bytes.subarray(i, i + 7));
  }
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const c of chunks) controller.enqueue(c);
      controller.close();
    },
  });
  return new Response(stream, {
    status: 200,
    headers: { "Content-Type": "text/event-stream" },
  });
}

describe("SSE stream demux", () => {
  it("demultiplexes token + progress frames in one stream, in order", async () => {
    const payload = [
      sseFrame("token", { text: "Designing " }),
      sseFrame("progress", { step: "render.view_00_front" }),
      sseFrame("token", { text: "an earring." }),
      sseFrame("done", { message: "complete" }),
    ].join("");
    fake.enqueue(makeSseResponse(payload));

    const events: Array<{ kind: string; value: string }> = [];
    await client.streamEvents(1, {
      onToken: (text) => events.push({ kind: "token", value: text }),
      onProgress: (step) => events.push({ kind: "progress", value: step ?? "" }),
      onDone: () => events.push({ kind: "done", value: "" }),
    });

    expect(events).toEqual([
      { kind: "token", value: "Designing " },
      { kind: "progress", value: "render.view_00_front" },
      { kind: "token", value: "an earring." },
      { kind: "done", value: "" },
    ]);
    expect(lastCall().url).toBe("http://api.test/api/stream/1");
  });

  it("answer path: a stream with only a done frame carrying kind:'answer' demuxes to onDone with the full payload (issue #249)", async () => {
    // The answer path emits NO token frames and NO progress frames — just
    // a single terminal done frame with kind:"answer" and the answer text
    // in `message`. The demux must deliver the full done payload (including
    // the `kind` field) to onDone so the App layer can distinguish answer
    // from design-loop frames.
    const payload = sseFrame("done", { message: "It is 12 mm tall — you said that.", kind: "answer" });
    fake.enqueue(makeSseResponse(payload));

    let tokenText = "";
    const progressSteps: string[] = [];
    let doneData: Record<string, unknown> | null = null;
    await client.streamEvents(1, {
      onToken: (t) => { tokenText += t; },
      onProgress: (step) => { progressSteps.push(step ?? ""); },
      onDone: (d) => { doneData = d; },
    });

    // No token or progress frames were emitted on the answer path.
    expect(tokenText).toBe("");
    expect(progressSteps).toEqual([]);
    // The done frame carried the full payload including kind.
    expect(doneData).toEqual({ message: "It is 12 mm tall — you said that.", kind: "answer" });
  });

  it("design-loop path: a done frame without kind demuxes to onDone with the message only (existing behaviour, issue #249)", async () => {
    // The standard design-loop done frame has no `kind` field (absent =
    // design). The demux must deliver the message so the App layer applies
    // the passCard summary (not the verbatim message).
    const payload = sseFrame("done", { message: "Design loop passed validation" });
    fake.enqueue(makeSseResponse(payload));

    let doneData: Record<string, unknown> | null = null;
    await client.streamEvents(1, {
      onToken: () => {},
      onProgress: () => {},
      onDone: (d) => { doneData = d; },
    });

    expect(doneData).toEqual({ message: "Design loop passed validation" });
    const d = doneData as Record<string, unknown> | null;
    expect(d?.kind).toBeUndefined();
  });

  it("handles multi-line SSE data (server splits JSON across data: lines)", async () => {
    const data = { message: "line\nbreak", step: "a" };
    const raw = JSON.stringify(data);
    const lines = raw.split("\n").map((l) => `data: ${l}`).join("\n");
    const payload = `event: progress\n${lines}\n\n`;
    fake.enqueue(makeSseResponse(payload));

    const got: Record<string, unknown>[] = [];
    await client.streamEvents(1, {
      onToken: () => {},
      onProgress: (_step, d) => got.push(d),
    });
    expect(got).toEqual([data]);
  });

  it("treats 'error' as a terminal event kind and calls onError", async () => {
    const payload = sseFrame("token", { text: "partial " }) + sseFrame("error", { message: "llm failed" });
    fake.enqueue(makeSseResponse(payload));

    let tokenText = "";
    let errMsg: string | undefined;
    await client.streamEvents(1, {
      onToken: (t) => { tokenText += t; },
      onProgress: () => {},
      onError: (d) => { errMsg = d.message as string; },
    });
    expect(tokenText).toBe("partial ");
    expect(errMsg).toBe("llm failed");
  });

  it("a mid-stream close without a terminal frame does NOT deliver partials as final", async () => {
    // Two token frames land, then the connection closes with no blank-line
    // terminator on a third, partial token frame — it must be dropped.
    const complete =
      sseFrame("token", { text: "first " }) + sseFrame("token", { text: "second " });
    const partialFrame = 'event: token\ndata: {"text": "trunca';
    fake.enqueue(makeSseResponse(complete + partialFrame));

    let tokenText = "";
    let done = false;
    await client.streamEvents(1, {
      onToken: (t) => { tokenText += t; },
      onProgress: () => {},
      onDone: () => { done = true; },
    });
    // Only complete frames were delivered; the truncated frame was not.
    expect(tokenText).toBe("first second ");
    expect(done).toBe(false);
  });

  it("the empty-stream contract: server's immediate 'done' resolves cleanly", async () => {
    fake.enqueue(makeSseResponse(sseFrame("done", { message: "no active stream" })));
    const got: string[] = [];
    await client.streamEvents(1, {
      onToken: (t) => got.push(t),
      onProgress: () => {},
      onDone: () => got.push("done"),
    });
    expect(got).toEqual(["done"]);
  });

  it("unknown event kinds are ignored (forward compat), stream continues", async () => {
    const payload =
      sseFrame("experimental", { foo: 1 }) +
      sseFrame("token", { text: "ok" }) +
      sseFrame("done", {});
    fake.enqueue(makeSseResponse(payload));
    const got: string[] = [];
    await client.streamEvents(1, {
      onToken: (t) => got.push(t),
      onProgress: () => {},
      onDone: () => got.push("done"),
    });
    expect(got).toEqual(["ok", "done"]);
  });

  it("the notice frame demuxes to onNotice, in order with other frames (issue #295)", async () => {
    // Issue #295: the server emits a `notice` frame (a non-terminal frame
    // carrying a fixed copy.ts sentence, e.g. the lost-photo notice)
    // BEFORE the terminal done/error frame. The demux must deliver the
    // full notice payload to onNotice (so the App can render it as a
    // plain assistant message in the transcript), in the correct order
    // relative to other frames.
    const noticeMessage =
      "Your reference photo for this project is missing, so I'm designing " +
      "from your words alone";
    const payload = [
      sseFrame("progress", { step: "render.view_00_front" }),
      sseFrame("notice", { message: noticeMessage }),
      sseFrame("done", { message: "Design loop passed validation" }),
    ].join("");
    fake.enqueue(makeSseResponse(payload));

    const events: Array<{ kind: string; value: string }> = [];
    await client.streamEvents(1, {
      onToken: () => {},
      onProgress: (step) => events.push({ kind: "progress", value: step ?? "" }),
      onNotice: (d) =>
        events.push({ kind: "notice", value: typeof d.message === "string" ? d.message : "" }),
      onDone: () => events.push({ kind: "done", value: "" }),
    });

    // The notice frame arrives AFTER the progress frame and BEFORE the
    // terminal done frame — the server defers the notice until just
    // before the terminal frame on every exit path, but the demux must
    // preserve the order.
    expect(events).toEqual([
      { kind: "progress", value: "render.view_00_front" },
      { kind: "notice", value: noticeMessage },
      { kind: "done", value: "" },
    ]);
  });

  it("the notice frame's payload is the full message string (issue #295)", async () => {
    // The notice frame carries the full fixed copy.ts string as the
    // `message` field. The demux must deliver the full payload (including
    // the `message` field) to onNotice so the App can render it verbatim
    // (the same pattern as the done frame's `confirm_sentence` and the
    // answer frame's `message`).
    const noticeMessage =
      "Your reference photo for this project is missing, so I'm designing " +
      "from your words alone";
    const payload = sseFrame("notice", { message: noticeMessage }) + sseFrame("done", {});
    fake.enqueue(makeSseResponse(payload));

    let noticeData: Record<string, unknown> | null = null;
    await client.streamEvents(1, {
      onToken: () => {},
      onProgress: () => {},
      onNotice: (d) => { noticeData = d; },
      onDone: () => {},
    });

    // The notice frame carried the full payload including the message.
    expect(noticeData).toEqual({ message: noticeMessage });
    // The message is a string (the fixed copy.ts sentence).
    const noticePayload = noticeData as { message?: string } | null;
    expect(noticePayload?.message).toBe(noticeMessage);
    if (noticePayload) {
      expect(typeof noticePayload.message).toBe("string");
    }
  });

  it("malformed JSON in a data frame is dispatched as an error, not dropped", async () => {
    const payload = "event: token\ndata: {not json\n\n" + sseFrame("done", {});
    fake.enqueue(makeSseResponse(payload));
    let errMsg: string | undefined;
    let done = false;
    await client.streamEvents(1, {
      onToken: () => {},
      onProgress: () => {},
      onError: (d) => { errMsg = d.message as string; },
      onDone: () => { done = true; },
    });
    expect(errMsg).toBe("malformed SSE data");
    expect(done).toBe(true);
  });

  it("rejects a non-200 response from the stream endpoint", async () => {
    fake.enqueue(json(500, { detail: "boom" }));
    await expect(
      client.streamEvents(1, { onToken: () => {}, onProgress: () => {} }),
    ).rejects.toMatchObject({ status: 500 });
  });

  it("propagates AbortSignal rejection from fetch", async () => {
    // The fake fetch mirrors the real one: an aborted signal rejects with
    // an AbortError. This proves the client plumbs the signal into the
    // request and lets the rejection propagate untouched (cancellation is
    // end-to-end, not swallowed or reclassified).
    const ac = new AbortController();
    ac.abort();
    fake.replace((url, init) => {
      fake.calls.push({ url, init });
      if (init.signal?.aborted) {
        const e = new DOMException("The operation was aborted.", "AbortError");
        return Promise.reject(e);
      }
      throw new Error("no abort path expected");
    });
    await expect(
      client.streamEvents(1, { onToken: () => {}, onProgress: () => {} }, ac.signal),
    ).rejects.toMatchObject({ name: "AbortError" });
    expect(fake.calls).toHaveLength(1); // the request WAS attempted with the signal
    expect((fake.calls[0]?.init.signal as AbortSignal | undefined)?.aborted).toBe(true);
  });

  it("invokes onError and rethrows on a mid-stream reader.read() rejection", async () => {
    // Simulate a network drop after the initial 200 response: the first
    // read() yields a valid token frame, the second read() rejects (e.g.
    // the connection dropped). The reader loop must not let this become an
    // untyped/unhandled rejection — it must call handlers.onError first.
    const encoder = new TextEncoder();
    const firstChunk = encoder.encode(sseFrame("token", { text: "partial " }));
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(firstChunk);
      },
      pull() {
        return Promise.reject(new Error("network drop"));
      },
    });
    const res = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    fake.enqueue(res);

    let tokenText = "";
    const onErrorSpy = vi.fn();
    await expect(
      client.streamEvents(1, {
        onToken: (t) => {
          tokenText += t;
        },
        onProgress: () => {},
        onError: onErrorSpy,
      }),
    ).rejects.toThrow("network drop");

    expect(tokenText).toBe("partial ");
    expect(onErrorSpy).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining("stream interrupted") }),
    );
  });

  it("client total deadline: a stream that hangs after one token frame aborts and calls onError with 'stream interrupted'", async () => {
    // Monkeypatch the deadline to a small value (100 ms) so the test is fast.
    const shortClient = new ApiClient({
      baseUrl: "http://api.test",
      fetch: fake.handler,
      streamTotalTimeoutMs: 100,
    });

    // A stream that emits one token frame, then never yields again
    // (pull returns a never-resolving promise — simulates a hung SSE stream).
    const encoder = new TextEncoder();
    const firstChunk = encoder.encode(sseFrame("token", { text: "partial " }));
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(firstChunk);
      },
      pull() {
        return new Promise<never>(() => {}); // never resolves
      },
    });
    const res = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    fake.enqueue(res);

    let tokenText = "";
    const onErrorSpy = vi.fn();
    const p = shortClient.streamEvents(1, {
      onToken: (t) => { tokenText += t; },
      onProgress: () => {},
      onError: onErrorSpy,
    });

    // The deadline fires at ~100 ms and the stream is aborted.
    await expect(p).rejects.toThrow();
    expect(tokenText).toBe("partial ");
    expect(onErrorSpy).toHaveBeenCalledWith(
      expect.objectContaining({ message: expect.stringContaining("stream interrupted") }),
    );
  });

  it("client total deadline: a stream that completes before the deadline is NOT aborted", async () => {
    // Deadline is 200 ms; the stream delivers a done frame after ~50 ms.
    const shortClient = new ApiClient({
      baseUrl: "http://api.test",
      fetch: fake.handler,
      streamTotalTimeoutMs: 200,
    });

    const encoder = new TextEncoder();
    const doneChunk = encoder.encode(sseFrame("done", { message: "ok" }));
    const stream = new ReadableStream<Uint8Array>({
      start(controller) {
        // Enqueue the done frame after a short delay (simulates a slow but
        // legitimate server response).
        setTimeout(() => {
          controller.enqueue(doneChunk);
          controller.close();
        }, 50);
      },
    });
    const res = new Response(stream, {
      status: 200,
      headers: { "Content-Type": "text/event-stream" },
    });
    fake.enqueue(res);

    const onDoneSpy = vi.fn();
    const onErrorSpy = vi.fn();
    await shortClient.streamEvents(1, {
      onToken: () => {},
      onProgress: () => {},
      onDone: onDoneSpy,
      onError: onErrorSpy,
    });

    expect(onDoneSpy).toHaveBeenCalled();
    expect(onErrorSpy).not.toHaveBeenCalled();
  });
});

describe("STREAM_TOTAL_TIMEOUT_MS constant", () => {
  it("is strictly greater than the server-side 360 s derived total with margin", () => {
    // Server deadline: 120 s per-attempt × 3 attempts = 360 s (issue #417).
    // Client must exceed with real margin.
    expect(STREAM_TOTAL_TIMEOUT_MS).toBeGreaterThan(360_000);
    // Must have at least 60 s of margin (not just 1 ms more).
    expect(STREAM_TOTAL_TIMEOUT_MS).toBeGreaterThanOrEqual(960_000);
  });
});

// ---------------------------------------------------------------------------
// Type-level invariants (compile-time)
// ---------------------------------------------------------------------------

describe("type-level invariants", () => {
  it("StreamEvent is the typed shape of demuxed frames", () => {
    const ev: StreamEvent<"token"> = { event: "token", data: { text: "x" } };
    expect(ev.event).toBe("token");
  });

  it("ApiError carries status and detail", () => {
    const e = new ApiError(418, { detail: "teapot" });
    expect(e.status).toBe(418);
    expect(e.detail).toEqual({ detail: "teapot" });
  });
});

// ---------------------------------------------------------------------------
// Error-class preservation (issue #233)
// ---------------------------------------------------------------------------

describe("ApiError error_class preservation", () => {
  it("constructor preserves errorClass without changing the message shape", () => {
    const e = new ApiError(
      502,
      "validation failed — no 3MF produced: Slice dry run failed",
      "slice",
    );
    expect(e.status).toBe(502);
    expect(e.errorClass).toBe("slice");
    // The message format is unchanged for every existing consumer.
    expect(e.message).toBe(
      "API 502: validation failed — no 3MF produced: Slice dry run failed",
    );
  });

  it("constructor leaves errorClass undefined when not supplied", () => {
    const e = new ApiError(404, "project not found");
    expect(e.errorClass).toBeUndefined();
    expect(e.message).toBe("API 404: project not found");
  });

  it("throwFor on a 502 body with error_class preserves it (the 3MF download shape)", async () => {
    fake.enqueue(
      json(502, {
        error: "validation failed — no 3MF produced: Slice dry run failed: can not find setting file: qidi",
        error_class: "slice",
      }),
    );
    await expect(client.getProject(1)).rejects.toMatchObject({
      status: 502,
      errorClass: "slice",
      detail: "validation failed — no 3MF produced: Slice dry run failed: can not find setting file: qidi",
    });
  });

  it("throwFor on a 409 body with error_class 'conflict' preserves it", async () => {
    fake.enqueue(json(409, { error: "validation in progress", error_class: "conflict" }));
    await expect(client.getProject(1)).rejects.toMatchObject({
      status: 409,
      errorClass: "conflict",
    });
  });

  it("a body without error_class (FastAPI {detail}, 404) yields undefined — no message change", async () => {
    fake.enqueue(json(404, { detail: "project not found" }));
    await expect(client.getProject(99)).rejects.toMatchObject({
      status: 404,
      errorClass: undefined,
      detail: "project not found",
    });
  });

  it("a non-JSON body yields no errorClass and the raw text as detail", async () => {
    fake.enqueue(new Response("Internal Server Error", { status: 500 }));
    const err = await client.getProject(1).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).errorClass).toBeUndefined();
    expect((err as ApiError).detail).toBe("Internal Server Error");
    expect((err as ApiError).message).toBe("API 500: Internal Server Error");
  });

  it("downloadModel3MF on a 502 carries the error_class", async () => {
    fake.enqueue(
      json(502, {
        error: "validation failed — no 3MF produced: no 3MF in render dir",
        error_class: "export_error",
      }),
    );
    await expect(client.downloadModel3MF(1)).rejects.toMatchObject({
      status: 502,
      errorClass: "export_error",
      detail: "validation failed — no 3MF produced: no 3MF in render dir",
    });
  });

  // -----------------------------------------------------------------------
  // Issue #295: the 409 `source_missing` mapping (real Response →
  // `apiClient.restoreVersion` → `throwFor`). The server emits the 409
  // body `{"detail": {"code": "source_missing", "message": "…"}}`
  // (d33d/versions_routes.py, `_raise_mapped`). The SPA must map the CODE
  // to copy.ts text (never the raw detail message, never the generic
  // `API 409: …` string); every other 409 (the no-op dedupe keeps its
  // legacy string detail) keeps its existing message.
  // -----------------------------------------------------------------------

  it("restoreVersion on a 409 `source_missing` carries the structured detail (issue #295, d5)", async () => {
    // Build a real 409 Response with the exact body the server emits
    // (d33d/versions_routes.py, `_raise_mapped`).
    fake.enqueue(
      json(409, {
        detail: {
          code: "source_missing",
          message: "the saved design source is missing from disk",
        },
      }),
    );
    const err = await client.restoreVersion(1, 5).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(409);
    // Issue #295: `throwFor` unwraps the FastAPI envelope when the detail is
    // a structured `{ code, message }` object — `ApiError.detail` IS the
    // inner object, so the App's `e.detail.code === "source_missing"` read
    // works directly (no `detail.detail` double-unwrap).
    expect(e.detail).toEqual({
      code: "source_missing",
      message: "the saved design source is missing from disk",
    });
    // The inner code is accessible directly on the detail object.
    const inner = e.detail as { code: string; message: string };
    expect(inner.code).toBe("source_missing");
    expect(inner.message).toBe("the saved design source is missing from disk");
    // The generic `API 409: …` message shape is preserved for every
    // caller (the App maps the code, not the message). The message may
    // contain the raw detail (the App reads `detail.code`, not `e.message`),
    // but the user-facing text is the copy.ts sentence.
    expect(e.message).toMatch(/^API 409:/);
  });

  it("restoreVersion on a legacy no-op 409 (string detail) keeps the legacy message (issue #295)", async () => {
    // The no-op 409 (the dedupe: restoring the current latest) keeps the
    // legacy string detail shape `{"detail": "…"}`. The App must map this
    // to its own generic message (never the raw string).
    fake.enqueue(
      json(409, {
        detail: "the version is already the current version",
      }),
    );
    const err = await client.restoreVersion(1, 5).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(409);
    // The legacy string detail is preserved (not collapsed to an object).
    expect(e.detail).toBe("the version is already the current version");
    // The generic `API 409: …` message shape is preserved.
    expect(e.message).toBe(
      "API 409: the version is already the current version",
    );
    // The detail has no `code` field (it's a string, not an object) — the
    // App's mapping reads `detail.code`, which is `undefined` for a
    // string, so it falls through to the generic path.
    const d = e.detail as unknown;
    expect(typeof d).toBe("string");
  });

  it("restoreVersion on a 409 with no detail body falls back to the generic message (issue #295)", async () => {
    // A 409 with an empty body (no detail at all) falls back to the
    // generic `HTTP 409` message — the App maps this to its own generic
    // message (never a raw HTTP status).
    fake.enqueue(new Response("", { status: 409 }));
    const err = await client.restoreVersion(1, 5).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    const e = err as ApiError;
    expect(e.status).toBe(409);
    expect(e.detail).toBe("HTTP 409");
    expect(e.message).toBe("API 409: HTTP 409");
  });
});
