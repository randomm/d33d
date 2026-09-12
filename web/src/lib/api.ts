/**
 * Typed backend API client for the d33d SPA (issue #6, workstream task-b).
 *
 * Thin, dependency-free fetch client over the FastAPI contract that exists on
 * main (PR #24 / commit 4783f99):
 *
 *   POST   /api/projects                      — create project
 *   GET    /api/projects                      — list projects
 *   GET    /api/projects/{id}                 — get project
 *   PATCH  /api/projects/{id}                 — update name / tags / notes
 *   DELETE /api/projects/{id}                 — delete project (204)
 *   POST   /api/projects/{id}/photos          — multipart photo upload
 *   GET    /api/stream/{id}                   — SSE: progress | token | done | error
 *   GET    /api/config/models                 — model catalogue (keys redacted)
 *   PUT    /api/config/models                 — full-YAML catalogue replacement
 *   GET    /api/settings/credentials          — names-only credential list
 *   POST   /api/settings/credentials          — store a provider key (Fernet)
 *   POST   /api/projects/{id}/region-edits    — region-scoped edit request (issue #7, task-c)
 *
 * Security invariants (inherited from the backend, asserted here):
 *   - API keys never surface in any value this module parses or returns.
 *     The model catalogue redacts keys ("***redacted***"); the credential
 *     list endpoint returns provider/model names only.
 *   - The client holds no secrets; all LLM/credential calls are server-side.
 */

// ---------------------------------------------------------------------------
// Errors
// ---------------------------------------------------------------------------

/** A non-2xx response from the backend. Carries the status and the
 *  server-provided error detail (parsed where the shape allows). */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;

  constructor(status: number, detail: unknown) {
    super(`API ${status}: ${stringifyDetail(detail)}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
  }
}

function stringifyDetail(detail: unknown): string {
  if (detail instanceof Error) return detail.message;
  if (typeof detail === "string") return detail;
  try {
    return JSON.stringify(detail) ?? String(detail);
  } catch {
    return String(detail);
  }
}

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface Project {
  id: number;
  name: string;
  git_repo_path: string;
  tags: string[];
  notes: string;
  source_photo_path: string | null;
  created_at: string;
  updated_at?: string;
}

export interface CreateProjectInput {
  name: string;
  tags?: string[];
  notes?: string;
}

export interface UpdateProjectInput {
  name?: string;
  tags?: string[];
  notes?: string;
}

export interface PhotoUploadResult {
  id: number;
  source_photo_path: string;
  size: number;
}

/** The four SSE event kinds the backend emits (d33d/streaming.py).
 *  Payloads are open-ended dicts keyed by event kind. */
export type StreamEventKind = "progress" | "token" | "done" | "error";

export interface StreamEvent<T extends StreamEventKind = StreamEventKind> {
  event: T;
  data: Record<string, unknown> & (T extends "token"
    ? { text?: string }
    : T extends "progress"
      ? { step?: string; message?: string }
      : { message?: string });
}

export interface ModelCatalogue {
  source: string;
  providers: Record<
    string,
    {
      name: string;
      base: string;
      /** Always the literal string `"***redacted***"` from the server. */
      key: string;
      defaults: Record<string, unknown>;
    }
  >;
  models: Array<{
    id: string;
    provider: string;
    model: string;
    context_window: number;
    params: Record<string, unknown>;
    fallbacks: string[];
    retries: Record<string, unknown>;
  }>;
  roles: Record<string, string>;
}

export interface Credential {
  provider_id: string;
  model_alias: string;
}

/** Max number of ranked module identifiers `RegionEditRequest.module_ids`
 *  accepts (matches `MAX_REGION_EDIT_MODULE_IDS` in `d33d/app.py`, enforced
 *  server-side via `Field(max_length=...)`). Callers must slice the
 *  ranked list to this length client-side or the request 422s. */
export const MAX_REGION_EDIT_MODULE_IDS = 10;

/** Which of the six render-worker views a lasso selection was drawn on
 *  (matches `ViewId` in `DimensionCanvas.tsx`). */
export type RegionEditViewId = "front" | "back" | "left" | "right" | "top" | "iso";

/** One vertex of the lasso polygon. Coordinate space is caller-dependent:
 *  `DimensionCanvas`'s photo-overlay lasso emits photo-pixel coordinates
 *  (`PhotoPoint`), while the region-selection lasso wired in `App.tsx`
 *  (issue #29) emits viewport-pixel coordinates (`ScreenPoint`, the same
 *  space `ModelViewer.resolveLassoSelection` raycasts through) — the
 *  server does not interpret this field's coordinate space today, since
 *  scoped-edit regeneration is deferred. */
export interface RegionEditPolygonPoint {
  x: number;
  y: number;
}

/**
 * Body of `POST /api/projects/{id}/region-edits` (issue #7, task-c).
 *
 * Carries the ranked module-identifier list `ModelViewer.resolveLassoSelection`
 * resolved (top-most/primary first — never pixel coordinates), the
 * composited red-marked PNG (base64, no data-URL prefix), the raw lasso
 * polygon, the view id it was drawn on, and the user's free-text edit
 * instruction.
 */
export interface RegionEditRequest {
  /** Ranked module identifiers, top-most/primary first. 1–10 entries
   *  (Set-of-Mark cap: small open models confuse longer ID lists). */
  module_ids: string[];
  view_id: RegionEditViewId;
  /** The composited red-marked PNG, base64-encoded (no `data:` prefix). */
  marked_png_base64: string;
  /** Closed polygon vertices, in the coordinate space the caller drew in
   *  (see `RegionEditPolygonPoint`). At least 3 points. */
  polygon: RegionEditPolygonPoint[];
  /** The user's free-text edit instruction for the selected region. */
  instruction: string;
}

/**
 * Response shape for the accept-and-defer stub. `status` is always
 * `"deferred"` — this route never regenerates OpenSCAD source (see
 * `createRegionEdit`'s doc comment).
 */
export interface RegionEditResult {
  project_id: number;
  status: "deferred";
  detail: string;
  module_ids: string[];
  view_id: string;
}

// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export interface ApiClientOptions {
  /** Base URL, e.g. `""` (same-origin) or `"http://localhost:8080"`. */
  baseUrl?: string;
  /** Injectable fetch (test seam). Defaults to globalThis.fetch. */
  fetch?: typeof fetch;
  /** Injectable AbortSignal source — callers can still pass per-call signals. */
}

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;

  constructor(options: ApiClientOptions = {}) {
    const base = options.baseUrl ?? "";
    this.baseUrl = base.endsWith("/") ? base.slice(0, -1) : base;
    this.fetchImpl = options.fetch ?? fetch.bind(globalThis);
  }

  // -- project CRUD ---------------------------------------------------------

  async createProject(input: CreateProjectInput): Promise<Project> {
    return this.request<Project>("POST", "/api/projects", input, 201);
  }

  async listProjects(): Promise<Project[]> {
    return this.request<Project[]>("GET", "/api/projects");
  }

  async getProject(id: number): Promise<Project> {
    return this.request<Project>("GET", `/api/projects/${id}`);
  }

  async updateProject(id: number, input: UpdateProjectInput): Promise<Project> {
    return this.request<Project>("PATCH", `/api/projects/${id}`, input);
  }

  async deleteProject(id: number): Promise<void> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}`,
      { method: "DELETE" },
    );
    if (!res.ok) await throwFor(res);
  }

  // -- photo upload -----------------------------------------------------------

  /**
   * Multipart upload of a reference photo (png/jpeg, ≤ 20 MB server-side).
   * Client-side type/size pre-checks live in the UI layer (task-a) — this
   * method only enforces that the File has an image type, matching the
   * backend's allowed content types so a rejected upload fails fast.
   */
  async uploadPhoto(
    id: number,
    file: File,
    signal?: AbortSignal,
  ): Promise<PhotoUploadResult> {
    if (!file.type.startsWith("image/")) {
      throw new ApiError(
        0,
        `file type ${file.type} must be image/*`,
      );
    }
    const form = new FormData();
    form.append("file", file);
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}/photos`,
      { method: "POST", body: form, signal },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as PhotoUploadResult;
  }

  // -- region-scoped edit request (issue #7, task-c) -------------------------
  //
  // NOTE: This is an HONEST STUB client method, matching the backend's own
  // accept-and-defer contract (see `d33d/app.py`'s `RegionEditRequest`
  // docstring). The route validates the payload and returns 202 Accepted
  // with `status: "deferred"` — it does NOT regenerate any OpenSCAD source.
  // Wiring "regenerate only these named modules" into the design loop
  // (`d33d/design_loop.py`, issue #5) is a future ticket's scope, same as
  // `downloadModel3MF` below documents for the 3MF HTTP route.

  /**
   * Submit a region-scoped edit request: the ranked module-identifier list
   * a lasso selection resolved to, the composited marked PNG, the lasso
   * polygon, the view id, and the user's instruction.
   *
   * Returns 202 Accepted with `status: "deferred"` — the caller must not
   * treat this as "the edit happened"; scoped-edit regeneration is not
   * yet implemented on the backend.
   */
  async createRegionEdit(
    projectId: number,
    input: RegionEditRequest,
  ): Promise<RegionEditResult> {
    return this.request<RegionEditResult>(
      "POST",
      `/api/projects/${projectId}/region-edits`,
      input,
      202,
    );
  }

  // -- model config -----------------------------------------------------------

  async getModelConfig(): Promise<ModelCatalogue> {
    return this.request<ModelCatalogue>("GET", "/api/config/models");
  }

  /**
   * Full-YAML replacement of the model catalogue. The catalogue YAML file is
   * the source of truth; pass the raw YAML string, not a parsed object.
   * Provider keys inside the YAML must be `${ENV_VAR}` references (the
   * backend rejects literals).
   */
  async putModelConfig(yaml: string): Promise<ModelCatalogue> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/config/models`,
      {
        method: "PUT",
        headers: { "Content-Type": "text/yaml" },
        body: yaml,
      },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as ModelCatalogue;
  }

  // -- credentials (names-only list; key storage) ------------------------------

  async listCredentials(): Promise<Credential[]> {
    return this.request<Credential[]>("GET", "/api/settings/credentials");
  }

  /** Store a provider key. The secret is Fernet-encrypted server-side
   *  under MASTER_KEY and never echoed back — the response carries only
   *  the provider/model names. */
  async storeCredential(input: {
    provider: string;
    model_alias: string;
    secret: string;
  }): Promise<Credential> {
    return this.request<Credential>(
      "POST",
      "/api/settings/credentials",
      input,
      201,
    );
  }

  // -- 3MF export (follow-up wiring) ---------------------------------------------
  //
  // NOTE: The backend 3MF-generation endpoint does NOT exist yet. The actual
  // 3MF pipeline lives in d33d/print_validation.py (Python, ticket #3) and is
  // not exposed via HTTP in this ticket's scope. This method is the client-side
  // plumbing that the Export3MF component uses; it will return a 404 until the
  // backend route is added. The component's download-trigger UI and API-call
  // plumbing are wired and tested; actual 3MF generation is a follow-up.

  /**
   * Download a 3MF file for the project.
   *
   * Follow-up wiring: the backend route ``GET /api/projects/{id}/model.3mf``
   * does not yet exist (3MF is produced by ``d33d/print_validation.py`` but
   * not exposed via HTTP). This method is the client-side plumbing — the
   * Export3MF component calls it to fetch the 3MF blob and trigger a browser
   * download. It will return a 404 until the backend endpoint is added.
   */
  async downloadModel3MF(projectId: number): Promise<Blob> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${projectId}/model.3mf`,
      { method: "GET" },
    );
    if (!res.ok) await throwFor(res);
    return res.blob();
  }

  // -- SSE streaming ------------------------------------------------------------

  /**
   * Open the project's event stream and demultiplex frames.
   *
   * The single stream carries two concurrent kinds of payload — LLM token
   * deltas and render-progress events (the 27B model takes 10–20 s to first
   * render, so text streams while renders land). The client demuxes by the
   * `event:` line: `token` → onToken, `progress` → onProgress, `done` →
   * resolves (after onDone), `error` → rejects (after onError).
   *
   * Contract (matches d33d/streaming.py):
   *   - Frames are separated by a blank line; `event:` gives the kind.
   *   - `data:` may span multiple lines (JSON is split by the server) —
   *     rejoin with "\n" and JSON.parse.
   *   - The stream terminates on a terminal event (done | error).
   *   - No active stream: the server emits an immediate `done` frame.
   *
   * The reader is byte-offset safe across chunk boundaries (partial frames
   * are buffered until the terminating blank line).
   */
  async streamEvents(
    id: number,
    handlers: {
      onToken: (text: string, data: Record<string, unknown>) => void;
      onProgress: (step: string | undefined, data: Record<string, unknown>) => void;
      onDone?: (data: Record<string, unknown>) => void;
      onError?: (data: Record<string, unknown>) => void;
    },
    signal?: AbortSignal,
  ): Promise<void> {
    const res = await this.fetchImpl(`${this.baseUrl}/api/stream/${id}`, {
      headers: { Accept: "text/event-stream" },
      signal,
    });
    if (!res.ok) await throwFor(res);
    if (!res.body) {
      throw new ApiError(0, "response has no readable body");
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buf = "";
    // Unflushed current frame lines (event + data lines).
    let eventKind: string | null = null;
    let dataLines: string[] = [];

    const flush = () => {
      if (eventKind === null) {
        eventKind = null;
        dataLines = [];
        return;
      }
      const raw = dataLines.join("\n");
      let payload: Record<string, unknown>;
      try {
        payload = raw ? (JSON.parse(raw) as Record<string, unknown>) : {};
      } catch {
        // Malformed frame: treat as an error event so the turn is not
        // silently lost.
        payload = { message: "malformed SSE data" };
        eventKind = "error";
      }
      dispatch(eventKind, payload, handlers);
      eventKind = null;
      dataLines = [];
    };

    try {
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += decoder.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n")) >= 0) {
          const line = buf.slice(0, idx).replace(/\r$/, "");
          buf = buf.slice(idx + 1);
          if (line === "") {
            flush();
          } else if (line.startsWith("event:")) {
            if (eventKind !== null) dataLines = []; // frame corruption: drop
            eventKind = line.slice("event:".length).trim();
          } else if (line.startsWith("data:")) {
            dataLines.push(line.slice("data:".length).trimStart());
          }
          // `id:` / `retry:` / comments: ignored per contract.
        }
      }
    } catch (e) {
      // Mid-stream network failure (e.g. connection dropped after the
      // initial 200 response): reader.read() rejects. Surface this to
      // callers via the typed onError handler instead of letting an
      // untyped rejection propagate.
      const message = e instanceof Error ? e.message : String(e);
      handlers.onError?.({ message: `stream interrupted: ${message}` });
      throw e;
    }
    // Terminal flush: if the stream closed without a blank line, dispatch
    // whatever is pending — but only if it is a terminal event, otherwise
    // the frame is incomplete (mid-stream drop) and must not render a
    // partial message as final.
    if (eventKind === "done" || eventKind === "error") {
      flush();
    }
  }

  // -- internals ------------------------------------------------------------------

  private async request<T>(
    method: "GET" | "POST" | "PATCH" | "PUT",
    path: string,
    body?: unknown,
    expectedStatus = 200,
  ): Promise<T> {
    const init: RequestInit = { method };
    if (body !== undefined) {
      init.headers = { "Content-Type": "application/json" };
      init.body = JSON.stringify(body);
    }
    const res = await this.fetchImpl(`${this.baseUrl}${path}`, init);
    if (res.status === expectedStatus && res.status >= 200 && res.status < 300) {
      if (res.status === 204) return undefined as T;
      return (await res.json()) as T;
    }
    await throwFor(res);
    throw new ApiError(res.status, "unreachable");
  }
}

/** Dispatch one parsed SSE frame to the matching handler. */
function dispatch(
  kind: string,
  payload: Record<string, unknown>,
  handlers: {
    onToken: (text: string, data: Record<string, unknown>) => void;
    onProgress: (step: string | undefined, data: Record<string, unknown>) => void;
    onDone?: (data: Record<string, unknown>) => void;
    onError?: (data: Record<string, unknown>) => void;
  },
): void {
  switch (kind) {
    case "token":
      handlers.onToken(typeof payload.text === "string" ? payload.text : "", payload);
      return;
    case "progress":
      handlers.onProgress(
        typeof payload.step === "string" ? payload.step : undefined,
        payload,
      );
      return;
    case "done":
      handlers.onDone?.(payload);
      return;
    case "error":
      handlers.onError?.(payload);
      return;
    default:
      // Unknown event kind: ignore (forward-compat with new server events)
      // rather than failing the turn.
  }
}

async function throwFor(res: Response): Promise<never> {
  let detail: unknown;
  const text = await res.text().catch(() => "");
  if (text) {
    try {
      detail = JSON.parse(text);
    } catch {
      detail = text;
    }
  }
  // FastAPI error shape: {"detail": "..."} or {"error": "..."} or a string
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (typeof d.detail === "string") detail = d.detail;
    else if (typeof d.error === "string") detail = d.error;
  }
  throw new ApiError(res.status, detail ?? `HTTP ${res.status}`);
}
