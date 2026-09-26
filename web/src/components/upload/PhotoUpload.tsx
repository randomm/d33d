/**
 * PhotoUpload — reference photo upload (part of the left pane).
 *
 * The operator selects an image file (png/jpeg, ≤ 20 MB). The component
 * validates type and size client-side before POSTing to the backend
 * upload endpoint. On success, the stored photo path is reported up.
 *
 * Backend contract (from d33d/projects.py):
 *   POST /api/projects/{id}/photos
 *   multipart/form-data with a `file` field
 *   Accepts: image/png, image/jpeg
 *   Max: 20 MB
 */

import { shell as shellCopy } from "../../copy";
import { useState, type ChangeEvent, type DragEvent } from "react";

const MAX_SIZE_BYTES = 20 * 1024 * 1024; // 20 MB
const ALLOWED_TYPES = ["image/png", "image/jpeg"];

interface PhotoUploadProps {
  /** Optional project ID — when absent, the component is in "no project yet" mode. */
  projectId?: number;
  /**
   * Issue #282: resolves the project the photo uploads to. Present in
   * "no project yet" mode (projectId undefined): awaits the single-flight
   * lazy creation (App's ensureProject) so a photo chosen before any
   * message still creates exactly one project. A rejection means creation
   * failed — the upload never fires and the caller's creation-failure
   * copy is surfaced instead.
   */
  onEnsureProject?: () => Promise<number>;
  /**
   * Called once the photo is stored server-side. `width`/`height` are the
   * image's natural pixel dimensions (read client-side via `Image.onload`)
   * so callers — e.g. DimensionCanvas — can map click coordinates back to
   * the actual photo instead of assuming a fixed size.
   */
  onUploaded: (photoPath: string, width: number, height: number) => void;
  onError?: (message: string) => void;
}

/** Read a File's natural pixel dimensions by loading it into an <img>. */
function readImageDimensions(file: File): Promise<{ width: number; height: number }> {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new window.Image();
    img.onload = () => {
      resolve({ width: img.naturalWidth, height: img.naturalHeight });
      URL.revokeObjectURL(url);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error("Failed to read image dimensions"));
    };
    img.src = url;
  });
}

type UploadState = "idle" | "uploading" | "success" | "error";

export function PhotoUpload({
  projectId,
  onEnsureProject,
  onUploaded,
  onError,
}: PhotoUploadProps) {
  const [state, setState] = useState<UploadState>("idle");
  const [previewUrl, setPreviewUrl] = useState<string | null>(null);
  const [dragOver, setDragOver] = useState(false);

  const validateFile = (file: File): string | null => {
    if (!ALLOWED_TYPES.includes(file.type)) {
      return `Unsupported file type "${file.type}". Use PNG or JPEG.`;
    }
    if (file.size > MAX_SIZE_BYTES) {
      return `File exceeds 20 MB limit (got ${(file.size / 1024 / 1024).toFixed(1)} MB).`;
    }
    return null;
  };

  const upload = async (file: File) => {
    let effectiveProjectId = projectId;
    if (effectiveProjectId === undefined) {
      // No project yet — create one through the caller's single-flight
      // latch (issue #282). Two rapid triggers (photo + send) share the
      // same in-flight promise, so exactly one project results. A
      // rejection means creation failed — surface the caller's failure
      // copy and do NOT upload (there is no project to upload to).
      if (onEnsureProject) {
        try {
          effectiveProjectId = await onEnsureProject();
        } catch (e) {
          // Creation failed — surface the caller's CREATION failure copy
          // (App routes it to the shared project-creation card, the same
          // card the send path uses), never an upload failure.
          setState("error");
          onError?.(shellCopy.projectCreationFailed(
            e instanceof Error ? e.message : "unknown error",
          ));
          return;
        }
      } else {
        setState("error");
        onError?.(shellCopy.noProject);
        return;
      }
    }
    const error = validateFile(file);
    if (error) {
      setState("error");
      onError?.(error);
      return;
    }

    setState("uploading");
    try {
      const formData = new FormData();
      formData.append("file", file);

      const resp = await fetch(
        `/api/projects/${effectiveProjectId}/photos`,
        { method: "POST", body: formData },
      );

      if (!resp.ok) {
        const body = await resp.json().catch(() => ({ detail: resp.statusText }));
        throw new Error(body.detail ?? `Upload failed (${resp.status})`);
      }

      const parsed: unknown = await resp.json();
      if (
        typeof parsed !== "object" ||
        parsed === null ||
        typeof (parsed as { source_photo_path?: unknown }).source_photo_path !== "string"
      ) {
        throw new Error("Upload response missing source_photo_path");
      }
      const data = parsed as { source_photo_path: string };

      // The server upload has now succeeded — `source_photo_path` is
      // durably stored. From here on, a failure is never reported as an
      // upload failure: reading the image's natural pixel dimensions is a
      // client-side nicety for DimensionCanvas, not part of the upload
      // contract. If it fails (corrupt/unusual file, stalled decode), fall
      // back to 0x0 and still report success — a retry here would just
      // create an orphaned duplicate on the server for no benefit.
      let width = 0;
      let height = 0;
      try {
        ({ width, height } = await readImageDimensions(file));
      } catch {
        // Dimensions unknown; upload success is unaffected.
      }
      setState("success");
      setPreviewUrl(URL.createObjectURL(file));
      onUploaded(data.source_photo_path, width, height);
    } catch (e) {
      setState("error");
      const msg = e instanceof Error ? e.message : "Upload failed";
      onError?.(msg);
    }
  };

  const handleFileChange = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file) void upload(file);
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file) void upload(file);
  };

  return (
    <div
      className={`photo-upload ${dragOver ? "drag-over" : ""}`}
      data-testid="photo-upload"
      onDragOver={(e) => {
        e.preventDefault();
        setDragOver(true);
      }}
      onDragLeave={() => setDragOver(false)}
      onDrop={handleDrop}
    >
      <input
        type="file"
        accept="image/png,image/jpeg"
        data-testid="photo-file-input"
        onChange={handleFileChange}
        aria-label="Reference photo upload"
        style={{ display: "none" }}
        id="photo-file-input"
      />
      <label
        htmlFor="photo-file-input"
        className="photo-upload-label"
        data-testid="photo-upload-label"
      >
        {state === "uploading" ? (
          <span data-testid="upload-status">Uploading…</span>
        ) : state === "success" && previewUrl ? (
          <img
            src={previewUrl}
            alt="Uploaded reference"
            className="photo-preview"
            data-testid="photo-preview"
          />
        ) : (
          <span>📎 Attach reference photo</span>
        )}
      </label>
      {state === "error" && (
        <span className="upload-error" data-testid="upload-error">
          Upload failed
        </span>
      )}
    </div>
  );
}
