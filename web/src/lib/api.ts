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
 *  server-provided error detail (parsed where the shape allows).
 *
 *  `errorClass` preserves the response's `error_class` (the closed
 *  `d33d/print_validation.py` enum, sent by the 3MF download route's
 *  502/409 bodies) so callers can map it to plain-language copy
 *  (issue #233). `undefined` whenever the body did not carry one —
 *  the `API ${status}: …` message shape is unchanged for every caller. */
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  readonly errorClass?: string;

  constructor(status: number, detail: unknown, errorClass?: string) {
    super(`API ${status}: ${stringifyDetail(detail)}`);
    this.name = "ApiError";
    this.status = status;
    this.detail = detail;
    this.errorClass = errorClass;
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

/**
 * The version-timeline entry (GET /api/projects/{id}/versions).
 * `diff_count` is the diff badge — the number of params that changed vs
 * the parent (0 for the first version).
 */
export interface VersionTimelineEntry {
  id: number;
  name: string;
  params: Record<string, number | string | boolean>;
  created_by_message: string;
  parent: number | null;
  restored_from: number | null;
  forked_from: [number, number] | null;
  pinned: boolean;
  archived: boolean;
  thumbnail: string | null;
  created_at: string;
  /** Diff badge: params changed vs the parent (0 for the first version).
   *  Present on the timeline (GET /versions) but NOT on the single-version
   *  GET /versions/{id} — the diff badge is computed in the timeline route
   *  from the parent pointer. */
  diff_count: number;
  /** The last 3MF export of this version, ISO-8601 UTC (issue #126) —
   *  server-side state, so the filmstrip mark survives a page reload.
   *  `null` when the version was never exported. */
  exported_at: string | null;
}

/**
 * A gallery card (GET /api/projects/{id}/gallery) — a pinned variant.
 * `actions` is the closed action set the gallery card renders.
 */
export interface GalleryCard extends VersionTimelineEntry {
  actions: Array<"set-as-main" | "branch-from" | "archive">;
}

/**
 * The closed provenance set (issue #120) — a literal, never a bare `string`
 * (the backend's `error_class` enum is the precedent): the user said this
 * value (`stated`), a render produced it (`measured`), the model picked it
 * with no user evidence (`assumed` — issue #246: model-emitted parameters
 * are assumed, never stated), there is no value (`unknown` — `value` is
 * `null`), or a measured value differs from the stated one (`disagrees` —
 * both numbers are carried; the displayed one is the MEASURED, because
 * that is what will print).
 */
export type DesignStateProvenance =
  | "stated"
  | "measured"
  | "assumed"
  | "unknown"
  | "disagrees";

/**
 * One design-state entry (GET /api/projects/{id}/design-state) — the row
 * the Brief renders. `value` is nullable: `provenance: "unknown"` carries
 * `null` (never `0`). `unit` is `"mm"` for numeric params, `null` for
 * non-numeric ones. `stated_value` is present ONLY when `provenance` is
 * `"disagrees"`.
 *
 * `kind` discriminates the row's origin: `"param"` (a row built from the
 * version's params snapshot — the model emitted the value) vs `"axis"`
 * (a row built from the persisted per-axis stated set — the dimension
 * protocol's W/D/H axes). `name` is NOT unique within a block: a param
 * row and an axis row can both be named `W` — `kind`+`name` is the row
 * identity, and nothing may dedupe, drop, or match rows by name.
 */
export interface DesignStateEntry {
  name: string;
  kind: "param" | "axis";
  label: string;
  value: number | string | boolean | null;
  unit: string | null;
  provenance: DesignStateProvenance;
  stated_value?: number | string | boolean | null;
  /** Issue #248: true when the label is the raw SCAD identifier (no
   *  model label) — the UI renders it in the mono face (mono = machine
   *  value). Absent/legacy entries are treated as identifiers. */
  label_is_identifier?: boolean;
  /** Issue #248: the model's declared axis ("W" | "D" | "H") — the only
   *  promotion evidence; present on param rows only. The UI renders the
   *  row via `kind`, not this field, but the contract test surface
   *  pins the full payload shape. */
  axis?: "W" | "D" | "H";
  /** Issue #248: the model's stated reason for a value the user did not
   *  give — the Brief's expanded assumed row renders it. */
  reason?: string;
}

/**
 * The compare response (GET .../versions/compare?a=&b=) — the prioritized
 * surface. Both full param sets, the computed diff table (added/removed/
 * changed), and the shared-rotation contract (identical units/axis
 * convention → the two client viewports share one camera/rotation state).
 */
export interface VersionCompare {
  project_id: number;
  a: VersionTimelineEntry;
  b: VersionTimelineEntry;
  diff: {
    added: string[];
    removed: string[];
    changed: string[];
    count: number;
  };
  /** The shared-rotation contract for the two-viewport compare surface. */
  shared_rotation: {
    units: "mm";
    axis_convention: "z-up";
    identical_convention: boolean;
  };
}

/**
 * A project-library card (GET /api/library). Search over name/tags/notes
 * is client-side over these rows.
 */
export interface LibraryCard {
  id: number;
  name: string;
  tags: string[];
  notes: string;
  current_version: number | null;
  last_activity: { ts: string | null; version_id: number | null; name: string | null } | null;
  thumbnail: string | null;
}

/**
 * The project (GET /api/projects/{id}) — with the version fields
 * (issue #8): `current_version` (the resume pointer) and
 * `last_activity` (the library-card activity).
 */
export interface Project {
  id: number;
  name: string;
  git_repo_path: string;
  tags: string[];
  notes: string;
  source_photo_path: string | null;
  created_at: string;
  updated_at?: string;
  /** The latest (main) version — the project resumes here. */
  current_version?: number | null;
  /** Last-activity record (the latest version's ts/name/id). */
  last_activity?: { ts: string | null; version_id: number | null; name: string | null } | null;
}

/** A design-loop FINALIZE input (issue #8). */
export interface FinalizeInput {
  /** The complete parameter set (full snapshot). */
  params?: Record<string, number | string | boolean>;
  name?: string;
  message?: string;
}

/** A manual version-create input (issue #8). */
export interface CreateVersionInput {
  params: Record<string, number | string | boolean>;
  name?: string;
  message?: string;
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
      : T extends "error"
        // Issue #82: the terminal error frame carries a STRUCTURED failure
        // reason on the design-loop exhausted path (one of the four
        // GATE_REASON_BITS or a render ErrorClass value). Absent on
        // infra-failure frames (no DesignResult to read it from) and on
        // legacy frames — a missing `reason` means "not a mapped design-loop
        // gate failure", never a gate reason.
        ? { message?: string; reason?: string }
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

/**
 * The build envelope (GET /api/config/envelope) — x/y/z in millimetres plus
 * a `verified` flag (true once the values have been confirmed against the
 * machine; false until then). A third reader of the backend's named
 * constants: the SPA prints these numbers and never a literal of its own.
 * The keep-out notch is deliberately NOT in this payload.
 */
export interface Envelope {
  x: number;
  y: number;
  z: number;
  unit: "mm";
  verified: boolean;
}

/** Max number of ranked module identifiers `RegionEditRequest.module_ids`
 *  accepts (matches `MAX_REGION_EDIT_MODULE_IDS` in `d33d/app.py`, enforced
 *  server-side via `Field(max_length=...)`). Callers must slice the
 *  ranked list to this length client-side or the request 422s. */
/**
 * Client-side TOTAL wall-clock deadline for the SSE stream, in milliseconds
 * (issue #221, last-resort catch-all).
 *
 * Measured from the start of `streamEvents` (the fetch call) — NOT an
 * idle/per-frame timer. Must EXCEED the server-side `DESIGN_LOOP_TIMEOUT_SECONDS`
 * (d33d/design_loop_events.py, 180 s) with real margin so the server's clean,
 * structured "design_loop_timed_out" error frame normally arrives first;
 * this deadline fires only if the server deadline never reached the client
 * (e.g. a half-open connection or the server process died).
 *
 * Overridable via `ApiClientOptions.streamTotalTimeoutMs` for tests
 * (the 240 s production value is untestable as-is).
 */
export const STREAM_TOTAL_TIMEOUT_MS = 240_000;

export const MAX_REGION_EDIT_MODULE_IDS = 10;

/** Which of the six render-worker views a region pick was drawn on
 *  (matches `ViewId` in `DimensionCanvas.tsx`). */
export type RegionEditViewId = "front" | "back" | "left" | "right" | "top" | "iso";

/** Runtime-checkable form of `RegionEditViewId`, matching
 *  `REGION_EDIT_VIEW_IDS` in `d33d/app.py` (the server 422s any `view_id`
 *  outside this set via `RegionEditRequest`'s field validator). The type
 *  alone only guards compile-time call sites — tests asserting the actual
 *  request body a mock captured need a runtime set to check against. */
export const REGION_EDIT_VIEW_IDS: readonly RegionEditViewId[] = [
  "front",
  "back",
  "left",
  "right",
  "top",
  "iso",
];

/** Hard cap on the base64-decoded `marked_png_base64` body, matching
 *  `MAX_REGION_EDIT_IMAGE_BYTES` in `d33d/app.py` (the route 413s above
 *  this). */
export const MAX_REGION_EDIT_IMAGE_BYTES = 5 * 1024 * 1024;

/** The single picked point, in the view's CSS-pixel coordinate space
 *  (the same space the pick layer records via `getBoundingClientRect()`
 *  and `ModelViewer.resolvePointPick` raycasts through). The server does
 *  not interpret the coordinate space — the marked PNG (the red dot
 *  composited at exactly this location) is the authoritative grounding;
 *  this field is kept for audit/debugging and the containment gate. */
export interface RegionEditPoint {
  x: number;
  y: number;
}

/**
 * Body of `POST /api/projects/{id}/region-edits` (issue #7, task-c;
 * wired to the design loop by issue #68; re-based for #98's point pick).
 *
 * Carries the OPTIONAL module-identifier list the point pick resolved
 * via `ModelViewer.resolvePointPick` (supplementary context — a streamed
 * unnamed STL legitimately yields an EMPTY list; the marked PNG is the
 * grounding, never the ids), the composited red-marked PNG (base64, no
 * data-URL prefix) the vision model sees as the marked-up render of the
 * current model, the single picked point (audit/debugging + the
 * containment gate), the view id it was drawn on, and the user's
 * free-text edit instruction.
 */
export interface RegionEditRequest {
  /** Optional module identifiers the pick resolved (0–10 entries; Set-of
   *  of-Mark cap: small open models confuse longer ID lists). Empty when
   *  the pick landed on unnamed geometry (a streamed STL). */
  module_ids: string[];
  view_id: RegionEditViewId;
  /** The composited red-marked PNG, base64-encoded (no `data:` prefix). */
  marked_png_base64: string;
  /** The single picked point in the view's CSS-pixel space (see
   *  `RegionEditPoint`). */
  point: RegionEditPoint;
  /** The user's free-text edit instruction for the selected region. */
  instruction: string;
}

/**
 * 202 response for `POST /api/projects/{id}/region-edits` (issue #68).
 * Mirrors `POST /{id}/chat` exactly — `{project_id, status: "accepted"}`.
 * The accepted status means the payload was validated and the design
 * loop was queued in the background; it does NOT mean any regeneration
 * happened. The new version (if the loop passes) arrives only via the
 * SSE stream's `version-created` progress frame, never in this body.
 */
export interface RegionEditResult {
  project_id: number;
  status: "accepted";
}

// ---------------------------------------------------------------------------
// ---------------------------------------------------------------------------
// Client
// ---------------------------------------------------------------------------

export interface ApiClientOptions {
  /** Base URL, e.g. `""` (same-origin) or `"http://localhost:8080"`. */
  baseUrl?: string;
  /** Injectable fetch (test seam). Defaults to globalThis.fetch. */
  fetch?: typeof fetch;
  /** Injectable AbortSignal source — callers can still pass per-call signals. */
  /** Override `STREAM_TOTAL_TIMEOUT_MS` (test seam; production uses the
   *  module constant). Must exceed the server-side deadline with margin. */
  streamTotalTimeoutMs?: number;
}

// ---------------------------------------------------------------------------
// Version-management client methods (issue #8)
// ---------------------------------------------------------------------------

export class ApiClient {
  private readonly baseUrl: string;
  private readonly fetchImpl: typeof fetch;
  private readonly streamTotalTimeoutMs: number;

  constructor(options: ApiClientOptions = {}) {
    const base = options.baseUrl ?? "";
    this.baseUrl = base.endsWith("/") ? base.slice(0, -1) : base;
    this.fetchImpl = options.fetch ?? fetch.bind(globalThis);
    this.streamTotalTimeoutMs =
      options.streamTotalTimeoutMs ?? STREAM_TOTAL_TIMEOUT_MS;
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

  // -- version management (issue #8) ------------------------------------

  /**
   * Get the version timeline (oldest first, with diff badges).
   */
  async listVersions(id: number): Promise<VersionTimelineEntry[]> {
    return this.request<VersionTimelineEntry[]>(
      "GET",
      `/api/projects/${id}/versions`,
    );
  }

  /**
   * Create a version (the manual create path; the design-loop FINALIZE
   * boundary uses `finalize` instead — a version is created exactly when
   * the loop passes validation).
   */
  async createVersion(
    id: number,
    input: CreateVersionInput,
  ): Promise<VersionTimelineEntry> {
    return this.request<VersionTimelineEntry>(
      "POST",
      `/api/projects/${id}/versions`,
      input,
      201,
    );
  }

  /**
   * Get a single version.
   */
  async getVersion(
    id: number,
    versionId: number,
  ): Promise<VersionTimelineEntry> {
    return this.request<VersionTimelineEntry>(
      "GET",
      `/api/projects/${id}/versions/${versionId}`,
    );
  }

  /**
   * Patch a version (rename / pin / archive / thumbnail — any subset).
   */
  async updateVersion(
    id: number,
    versionId: number,
    input: {
      name?: string;
      pinned?: boolean;
      archived?: boolean;
      thumbnail?: string;
    },
  ): Promise<VersionTimelineEntry> {
    return this.request<VersionTimelineEntry>(
      "PATCH",
      `/api/projects/${id}/versions/${versionId}`,
      input,
    );
  }

  /**
   * Record that the 3MF of a SPECIFIC version was exported (issue #126).
   * The mark belongs to the version actually downloaded — not necessarily
   * the latest — and is server-side state (it survives a page reload).
   * The SPA calls this only AFTER a successful download, so no failed or
   * cancelled export can set a mark.
   *
   * NOTE (follow-up wiring): the DOWNLOAD route
   * `GET /api/projects/{id}/model.3mf` does not exist server-side yet (the
   * 3MF is produced by d33d/print_validation.py but not exposed over HTTP —
   * the same follow-up as `downloadModel3MF`). The mark route is real and
   * lands now, so the reload-survival contract is in place before the
   * download wires up.
   */
  async recordExport(
    id: number,
    versionId: number,
  ): Promise<VersionTimelineEntry> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}/versions/${versionId}/export`,
      { method: "POST" },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as VersionTimelineEntry;
  }

  /**
   * Non-destructive restore: a NEW forward version with the target's full
   * snapshot (parent = current latest). Restoring the current latest is a
   * 409 (no-op — dedupe, no spurious entry).
   */
  async restoreVersion(
    id: number,
    versionId: number,
  ): Promise<VersionTimelineEntry> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}/versions/${versionId}/restore`,
      { method: "POST" },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as VersionTimelineEntry;
  }

  /**
   * Set as main: re-point `current_version` in place AND write a no-change
   * marker commit (the git history records the switch without rewriting).
   */
  async setVersionAsMain(
    id: number,
    versionId: number,
  ): Promise<Project> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}/versions/${versionId}/set-as-main`,
      { method: "POST" },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as Project;
  }

  /**
   * Branch from: creates a NEW PROJECT (own git repo) whose first version
   * is seeded from the source version's full snapshot. The new repo's
   * history contains no source-project commit (forks are variant cards,
   * not a git graph).
   */
  async branchFromVersion(
    id: number,
    versionId: number,
  ): Promise<{ project: Project; version: VersionTimelineEntry }> {
    const res = await this.fetchImpl(
      `${this.baseUrl}/api/projects/${id}/versions/${versionId}/branch-from`,
      { method: "POST" },
    );
    if (!res.ok) await throwFor(res);
    return (await res.json()) as {
      project: Project;
      version: VersionTimelineEntry;
    };
  }

  /**
   * Compare two versions (the prioritized surface): both full param sets,
   * the computed diff table, and the shared-rotation contract (the two
   * client viewports share one camera/rotation state).
   */
  async compareVersions(
    id: number,
    a: number,
    b: number,
  ): Promise<VersionCompare> {
    return this.request<VersionCompare>(
      "GET",
      `/api/projects/${id}/versions/compare?a=${a}&b=${b}`,
    );
  }

  /**
   * Get the pinned variant gallery (archived variants hidden by default;
   * `archived` reveals them).
   */
  async getGallery(id: number, archived = false): Promise<GalleryCard[]> {
    const q = archived ? "?archived=1" : "";
    return this.request<GalleryCard[]>("GET", `/api/projects/${id}/gallery${q}`);
  }

  /**
   * Get the project library grid (name, last activity, thumbnail per
   * project; search is client-side).
   */
  async getLibrary(): Promise<LibraryCard[]> {
    return this.request<LibraryCard[]>("GET", "/api/library");
  }

  /**
   * Get the design-state block (issue #120, consumer 2): the entries the
   * Brief renders, with provenance per value. `value` is nullable —
   * `provenance: "unknown"` serialises as `null` and MUST survive JSON.parse
   * as `null` (never defaulted to 0 — issue #91 shipped exactly that
   * defect on the backend). `stated_value` rides alongside ONLY on
   * `disagrees` entries.
   */
  async getDesignState(id: number): Promise<DesignStateEntry[]> {
    return this.request<DesignStateEntry[]>(
      "GET",
      `/api/projects/${id}/design-state`,
    );
  }

  /**
   * Get the project's current OpenSCAD design source (the conversation's
   * resumed design state). `source` is null when no design exists yet.
   */
  async getDesignSource(
    id: number,
  ): Promise<{ source: string | null }> {
    return this.request<{ source: string | null }>(
      "GET",
      `/api/projects/${id}/design-source`,
    );
  }

  /**
   * Upload the project's OpenSCAD design source (persisted to the git
   * repo — versioned content).
   */
  async putDesignSource(id: number, source: string): Promise<{ stored: string; length: number }> {
    return this.request<{ stored: string; length: number }>(
      "POST",
      `/api/projects/${id}/design-source`,
      { source },
    );
  }

  /**
   * FINALIZE: run the injected design loop; a `pass` result versions the
   * best candidate's parameters; any other status is a 422 (never a
   * spurious version).
   */
  async finalize(id: number, input?: FinalizeInput): Promise<VersionTimelineEntry> {
    return this.request<VersionTimelineEntry>(
      "POST",
      `/api/projects/${id}/finalize`,
      input ?? {},
      201,
    );
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

  // -- region-scoped edit request (issue #7, task-c; design-loop wiring
  // by issue #68) — mirrors `postChat` (see below).

  /**
   * Submit a region-scoped edit request: the ranked module-identifier list
   * a point pick resolved to, the composited marked PNG, the picked
   * point, the view id, and the user's instruction.
   *
   * Returns 202 Accepted with `status: "accepted"` (mirroring `POST
   * /{id}/chat`) — the design loop runs in the background and the new
   * version (if the loop passes) arrives only via the SSE stream's
   * `version-created` progress frame, never in this response. A 409 means
   * a design loop is already in flight for this project.
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

  // -- chat / design loop (issue #54) ---------------------------------------

  /**
   * Send a chat message to the design loop.
   *
   * Returns 202 Accepted immediately — the design loop runs in a
   * background task and streams progress/token frames via the SSE endpoint
   * (`GET /api/stream/{projectId}`). The caller must open the SSE stream
   * AFTER this call resolves (the event source is registered synchronously
   * before the 202 response, so the stream will find it).
   *
   * `statedDims` is an optional (W, D, H) triple in mm. The SPA never
   * sends it (see the call site in App.tsx); when absent the SERVER
   * resolves dimensions itself — first the dimensions stated in the
   * message text, then the latest version's W/D/H — and when neither
   * yields a triple the design loop's bbox gate abstains rather than
   * fabricating a (0, 0, 0) target (issue #91). Never a 422.
   *
   * `chatHistory` is the list of prior user messages (the SPA sends the
   * last 10). Absent → empty tuple.
   */
  async postChat(
    projectId: number,
    input: {
      message: string;
      stated_dims?: [number, number, number];
      chat_history?: string[];
    },
  ): Promise<{ status: string }> {
    return this.request<{ status: string }>(
      "POST",
      `/api/projects/${projectId}/chat`,
      input,
      202,
    );
  }

  // -- model config -----------------------------------------------------------

  async getModelConfig(): Promise<ModelCatalogue> {
    return this.request<ModelCatalogue>("GET", "/api/config/models");
  }

  /**
   * The build envelope for the machine (GET /api/config/envelope) — x/y/z in
   * millimetres plus a `verified` flag. A third READER of the backend's named
   * constants (never a copy of the numbers): the first-run plate backdrop and
   * the failure card's envelope copy both print these values, so the SPA must
   * read them from the API rather than literal a confident value it has not
   * established. The keep-out notch is deliberately NOT in this payload.
   */
  async getEnvelope(): Promise<Envelope> {
    return this.request<Envelope>("GET", "/api/config/envelope");
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
    // Internal total-deadline: if no terminal frame arrives within
    // `streamTotalTimeoutMs` (wall-clock from here), the reader is
    // cancelled (tearing down the fetch/stream) and the existing "stream
    // interrupted" onError path fires via the reader.read() rejection.
    const res = await this.fetchImpl(`${this.baseUrl}/api/stream/${id}`, {
      headers: { Accept: "text/event-stream" },
      signal,
    });
    if (!res.ok) await throwFor(res);
    if (!res.body) {
      throw new ApiError(0, "response has no readable body");
    }

    const reader = res.body.getReader();
    // Internal total-deadline: if no terminal frame arrives within
    // `streamTotalTimeoutMs` (wall-clock from here), the reader is
    // cancelled (tearing down the fetch/stream) and the existing "stream
    // interrupted" onError path fires via the loop exit detection.
    let deadlineFired = false;
    const deadlineTimer: ReturnType<typeof setTimeout> = setTimeout(() => {
      deadlineFired = true;
      // Cancel the reader — this causes the next read() to resolve with
      // done=true (the stream closes cleanly) and tears down the
      // underlying fetch connection.
      void reader.cancel();
    }, this.streamTotalTimeoutMs);
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
      if (eventKind === "done" || eventKind === "error") {
        clearTimeout(deadlineTimer);
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

    // Deadline fired: the stream was cancelled (reader.read() resolved
    // with done=true via reader.cancel()). Route through the same
    // "stream interrupted" onError path.
    if (deadlineFired) {
      const err = new Error("stream total deadline exceeded");
      handlers.onError?.({ message: `stream interrupted: ${err.message}` });
      throw err;
    }
    clearTimeout(deadlineTimer);
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
      if (res.status === 204) {
        // 204 No Content — the caller must use `Promise<void>` (not a typed
        // body) so T is never cast to undefined for a body-shaped return.
        return undefined as unknown as T;
      }
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
  // FastAPI error shape: {"detail": "..."} or {"error": "..."} or a string.
  // The 3MF download route (issue #233) adds "error_class" to these bodies
  // (d33d/print_validation.py's closed enum) — preserve it on the ApiError
  // before the detail is collapsed to a string; `undefined` for every body
  // shape without the key, so other callers see no change.
  let errorClass: string | undefined;
  if (detail && typeof detail === "object") {
    const d = detail as Record<string, unknown>;
    if (typeof d.error_class === "string") errorClass = d.error_class;
    if (typeof d.detail === "string") detail = d.detail;
    else if (typeof d.error === "string") detail = d.error;
  }
  throw new ApiError(res.status, detail ?? `HTTP ${res.status}`, errorClass);
}
