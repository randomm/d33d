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

import { useState, type ChangeEvent, type DragEvent } from "react";

const MAX_SIZE_BYTES = 20 * 1024 * 1024; // 20 MB
const ALLOWED_TYPES = ["image/png", "image/jpeg"];

interface PhotoUploadProps {
  /** Optional project ID — when absent, the component is in "no project yet" mode. */
  projectId?: number;
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
    if (projectId === undefined) {
      onError?.("No project selected");
      return;
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
        `/api/projects/${projectId}/photos`,
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
      const { width, height } = await readImageDimensions(file);
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
