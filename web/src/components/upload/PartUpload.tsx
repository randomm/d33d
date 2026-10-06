/**
 * PartUpload — the part (STL/3MF) upload surface (issue #334, sub-issue D5).
 *
 * A self-contained drop target + file picker for an STL/3MF part. A part
 * chosen before any project exists routes through the SAME single-flight
 * `ensureProject` latch as the first send and the photo path (issue #192 /
 * #282) — a part and a message in quick succession share ONE in-flight
 * createProject call, and no /part POST fires before the project exists.
 *
 * The upload BEHAVIOUR (the extension guard, the ensure-project latch
 * routing, the POST, and the verbatim-detail error reduction) lives in
 * `usePartUpload` — this component and the Screen 1 file card (App's
 * `handlePartFile`) share that one path; the component owns only the
 * drop-target UI state and the prop-to-callback adaptation (a missing
 * latch, or a latch reject, surfaces the project-creation failure copy).
 *
 * Error contract (the spec): the 400 / 413 / 422 / 409 `detail` bodies are
 * surfaced VERBATIM in the blocked style (`var(--color-blocked)`, #D2A63C) —
 * never a status code, never paraphrased (the #299 way). A success reports
 * the project id up (`onUploaded`) — Screen 2 (the import report) is the
 * caller's job, driven by its design-state refetch.
 *
 * All strings come from copy — nothing is inlined.
 */

import { useState, useRef, useCallback, useEffect, type ChangeEvent, type DragEvent } from "react";
import { ApiClient, ApiError } from "../../lib/api";
import copy from "../../copy";

interface PartUploadProps {
  /** The project id to upload to. When absent, the component routes
   *  through `onEnsureProject` (the single-flight lazy-creation latch)
   *  before any POST. */
  projectId?: number;
  /** The single-flight lazy-creation latch (App's `ensureProject`). */
  onEnsureProject?: () => Promise<number>;
  /** Called once the part is stored server-side (with the project id). */
  onUploaded?: (projectId: number) => void;
  /** Surfaces a project-creation failure or an upload failure. */
  onError?: (message: string, detail?: string) => void;
  /** A client-side ApiClient (test seam). Defaults to a same-origin client. */
  client?: ApiClient;
}

const ACCEPT = ".stl,.3mf";

/** Reduce an upload/creation failure to the verbatim `detail` the blocked
 *  state renders. A FastAPI body carries `{detail}` — a string, or the
 *  structured `{code, message}` object the 409s carry. Exported: the
 *  Screen 1 file-card path (App's `handlePartFile`) shares this reduction
 *  so one place answers "what does this upload error say". */
export function detailText(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail;
    if (typeof d === "string") return d;
    if (d && typeof d === "object") {
      const obj = d as Record<string, unknown>;
      if (typeof obj.message === "string") return obj.message;
    }
    return e.message;
  }
  return e instanceof Error ? e.message : String(e);
}

/**
 * usePartUpload — the shared part-file (STL/3MF) upload path (issue #334).
 *
 * ONE place answers "is this a part file, and how do we upload it": the
 * extension guard, the ensure-project latch routing, the upload POST, and
 * the verbatim-detail error reduction. The Screen 1 file card (App's
 * `handlePartFile`) and the chat-pane drop area (PartUpload) both call
 * this — three inlined copies of the same predicate used to exist.
 *
 * The 400/422/413/409 `detail` bodies are surfaced VERBATIM through
 * `detailText` (the #299 way) — never a status code, never paraphrased.
 */
export type PartFileResult = "unsupported" | "created" | "failed";

interface UsePartUploadOptions {
  /** The project id to upload to. When absent, the upload routes through
   *  `ensureProject` (the single-flight lazy-creation latch) first. */
  projectId: number | null;
  /** The single-flight lazy-creation latch (App's `ensureProject`). */
  ensureProject: () => Promise<number>;
  /** The client used for the POST (App's stable same-origin client). */
  client: ApiClient;
  /** Called once the part is stored server-side (with the project id). */
  onSuccess: (projectId: number) => void;
  /** Called with the verbatim `detail` on an upload/creation failure. */
  onError: (message: string, detail?: string) => void;
  /** Called with the verbatim copy on a project-creation failure. */
  onProjectCreationFailure?: (e: unknown) => void;
  /** Called when the upload POST starts and when it settles (issue #395
   *  elapsed-seconds state: the label needs a live start timestamp while
   *  the request is in flight). */
  onUploadStateChange?: (inFlight: boolean) => void;
}

/** The shared guard + upload + error reduction (see the module doc). */
export function usePartUpload({
  projectId,
  ensureProject,
  client,
  onSuccess,
  onError,
  onProjectCreationFailure,
  onUploadStateChange,
}: UsePartUploadOptions): (file: File) => Promise<PartFileResult> {
  return useCallback(
    async (file: File) => {
      // Client-side extension guard (belt-and-braces over `accept`): never
      // POST a non-STL/3MF — the server 400s it, but a fast local refusal
      // keeps the 400 detail for a genuinely unsupported *type*.
      const name = file.name.toLowerCase();
      if (!name.endsWith(".stl") && !name.endsWith(".3mf")) {
        onError(copy.partUpload.unsupported, copy.partUpload.unsupported);
        return "unsupported";
      }
      const doUpload = async (pid: number) => {
        onUploadStateChange?.(true);
        try {
          await client.uploadPart(pid, file);
          onSuccess(pid);
          return "created" as const;
        } catch (e) {
          // The verbatim-detail reduction (the module's exported
          // `detailText`) — one place answers "what does this upload say".
          const detail = detailText(e);
          onError(detail, detail);
          return "failed" as const;
        } finally {
          onUploadStateChange?.(false);
        }
      };
      if (projectId !== null) {
        return doUpload(projectId);
      }
      try {
        const id = await ensureProject();
        return await doUpload(id);
      } catch (e) {
        onProjectCreationFailure?.(e);
        return "failed";
      }
    },
    [projectId, ensureProject, client, onSuccess, onError, onProjectCreationFailure, onUploadStateChange],
  );
}

type UploadState = "idle" | "uploading" | "success" | "error";

export function PartUpload({
  projectId,
  onEnsureProject,
  onUploaded,
  onError,
  client,
}: PartUploadProps) {
  const [state, setState] = useState<UploadState>("idle");
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);
  // Issue #395: the upload/parse in-flight window — a big mesh can take a
  // long time to read and repair, so the label ticks "Reading your file…
  // Ns" with the client-side elapsed seconds (spec: client-side elapsed
  // time is acceptable, no server streaming). `uploadStart` is the timestamp
  // the count measures from; `uploadElapsed` is the rendered second count.
  const [uploadStart, setUploadStart] = useState<number | null>(null);
  const [uploadElapsed, setUploadElapsed] = useState(0);

  useEffect(() => {
    // The interval runs ONLY while a read is in flight (the state is
    // "uploading" AND the start timestamp is set — on settle, the shared
    // path's `onUploadStateChange(false)` nulls `uploadStart`, so the
    // cleanup fires and no further state updates happen after the POST
    // settles; the label swaps to the result state's UI in the same render
    // batch as the settle).
    if (state !== "uploading" || uploadStart === null) return;
    const tick = () =>
      setUploadElapsed(Math.max(0, Math.floor((Date.now() - uploadStart) / 1000)));
    tick();
    const id = window.setInterval(tick, 1000);
    return () => window.clearInterval(id);
  }, [state, uploadStart]);
  const clientRef = useRef<ApiClient | null>(client ?? null);
  clientRef.current = client ?? null;

  // A missing caller latch is adapted to a rejecting latch: the shared
  // `usePartUpload` path requires one, and the reject makes the shared
  // project-creation-failure branch surface `copy.shell.noProject` (the
  // component's own error state), never a stray upload failure.
  const ensureLatch = useRef<() => Promise<number>>(
    onEnsureProject ?? (() => Promise.reject(new Error(copy.shell.noProject))),
  );
  ensureLatch.current =
    onEnsureProject ?? (() => Promise.reject(new Error(copy.shell.noProject)));

  const upload = usePartUpload({
    projectId: projectId ?? null,
    ensureProject: ensureLatch.current,
    client: clientRef.current ?? new ApiClient(),
    onSuccess: (pid) => {
      setState("success");
      onUploaded?.(pid);
    },
    onError: (message, detail) => {
      setState("error");
      setErrorDetail(detail ?? message);
      onError?.(message, detail);
    },
    onProjectCreationFailure: (e) => {
      setState("error");
      const msg = copy.shell.projectCreationFailed(
        e instanceof Error ? e.message : "unknown error",
      );
      setErrorDetail(msg);
      onError?.(msg, msg);
    },
    onUploadStateChange: (inFlight) => {
      // The shared path marks the in-flight window — the component owns
      // the timer state (it knows the label to render for it). On settle
      // the result (success/error) was already applied by the shared
      // path's own callbacks, which run before this notice; the interval
      // cleanup fires from the effect when the state leaves "uploading".
      if (inFlight) {
        setState("uploading");
        setUploadStart(Date.now());
        setUploadElapsed(0);
      } else {
        setUploadStart(null);
      }
    },
  });

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void upload(file);
    // Reset the input so re-picking the SAME file fires change again.
    e.target.value = "";
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) void upload(file);
  };

  return (
    <div
      className={`part-upload ${dragOver ? "drag-over" : ""}`}
      data-testid="part-upload"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      <input
        type="file"
        accept={ACCEPT}
        data-testid="part-file-input"
        onChange={handleFileChange}
        aria-label="Import an STL or 3MF file (chat pane)"
        style={{ display: "none" }}
        id="part-file-input"
      />
      <label
        htmlFor="part-file-input"
        className="part-upload-label"
        data-testid="part-upload-label"
      >
        {state === "uploading" ? (
          <span data-testid="part-upload-status">
            {copy.partUpload.reading(uploadElapsed)}
          </span>
        ) : (
          <span>{copy.partUpload.dropLine}</span>
        )}
      </label>
      {state === "error" && errorDetail && (
        <span
          className="part-upload-error"
          data-testid="part-upload-error"
          style={{ color: "var(--color-blocked)" }}
        >
          {errorDetail}
        </span>
      )}
    </div>
  );
}
