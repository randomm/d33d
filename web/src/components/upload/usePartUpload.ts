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
import { useCallback } from "react";
import type { ApiClient } from "../../lib/api";
import copy from "../../copy";
import { detailText } from "./PartUpload";

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
}

/** The shared guard + upload + error reduction (see the module doc). */
export function usePartUpload({
  projectId,
  ensureProject,
  client,
  onSuccess,
  onError,
  onProjectCreationFailure,
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
        try {
          await client.uploadPart(pid, file);
          onSuccess(pid);
          return "created" as const;
        } catch (e) {
          // The verbatim-detail reduction (PartUpload's exported
          // `detailText`) — one place answers "what does this upload say".
          const detail = detailText(e);
          onError(detail, detail);
          return "failed" as const;
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
    [projectId, ensureProject, client, onSuccess, onError, onProjectCreationFailure],
  );
}
