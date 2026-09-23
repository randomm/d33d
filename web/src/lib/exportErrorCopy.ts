/**
 * 3MF export error mapping (issue #233).
 *
 * Maps a `downloadModel3MF` rejection to the failure surface's primary
 * sentence + the raw backend detail (kept collapsed, mirroring the
 * design-loop failure turn's raw disclosure).
 *
 * The raw backend text (e.g. "validation failed — no 3MF produced: Slice
 * dry run failed: …") is NEVER the primary copy. The `ApiError.errorClass`
 * — the backend's closed `d33d/print_validation.py` enum, preserved by the
 * API client (issue #233) — selects the sentence:
 *
 *   - a class with a sentence in `copy.failure.reasons` → that sentence
 *     (load_error, watertight, winding, dimension, volume, slice,
 *     envelope, export_error — the validation-gate 502 classes);
 *   - `"conflict"` (the 409 body: a design pass is still in flight) →
 *     `copy.export3mf.conflict`;
 *   - everything else — `"unknown"` (the backend's fallback), any
 *     unlisted/future class, no `error_class` at all (the 404 bodies),
 *     or a non-ApiError rejection (a network failure carries no status or
 *     class) → the generic `copy.export3mf.failed`.
 *
 * `detail` is the raw backend message (the string `ApiError.detail`) for
 * collapse display; `undefined` when the rejection carries no usable
 * detail — the generic sentence then stands alone, and no `<details>`
 * renders.
 */

import { ApiError } from "./api";
import { copy } from "../copy";

/** The mapped export failure: the primary sentence plus (when established)
 *  the raw backend detail for the collapsed disclosure. */
export interface ExportErrorDisplay {
  /** The primary plain-language sentence (never raw backend text). */
  message: string;
  /** The raw backend detail, for the collapsed <details> (part 4). */
  detail?: string;
}

function rawDetail(e: unknown): string | undefined {
  return e instanceof ApiError && typeof e.detail === "string"
    ? e.detail
    : undefined;
}

/** Map a download rejection to its failure-turn display (see module docs
 *  for the mapping rule). */
export function displayExportError(e: unknown): ExportErrorDisplay {
  if (e instanceof ApiError) {
    const mapped = e.errorClass
      ? (copy.failure.reasons as Record<string, string>)[e.errorClass]
      : undefined;
    const message =
      mapped ??
      (e.errorClass === "conflict"
        ? copy.export3mf.conflict
        : copy.export3mf.failed);
    const detail = rawDetail(e);
    return { message, ...(detail !== undefined ? { detail } : {}) };
  }
  const detail = rawDetail(e);
  return {
    message: copy.export3mf.failed,
    ...(detail !== undefined ? { detail } : {}),
  };
}
