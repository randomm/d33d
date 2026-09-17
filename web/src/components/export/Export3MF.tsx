/**
 * Export3MF — the 3MF download (validation-status panel).
 *
 * The 3MF is a DOWNLOAD ARTIFACT, never rendered inline (the ModelViewer
 * "3MF exclusion" contract) — this component triggers the browser download
 * and nothing more. Slicing stays in Orca; nothing here implies it slices.
 *
 * The designed ending (issue #126): on a SUCCESSFUL download the parent is
 * told via `onExported(versionId)` exactly once. The parent (App) is the
 * only thing that appends the completion turn (copy.shell.exportDone) and
 * records the filmstrip mark (POST …/versions/{id}/export) — both after the
 * download's bytes are in the browser. A failed or cancelled download calls
 * neither: no completion turn, no mark.
 *
 * The filename comes from `copy.shell.exportFilename(project, version)` —
 * the slug contract the design contract pins ("Curtain rod bracket" + "v4"
 * → "curtain-rod-bracket-v4.3mf"), never an invented `model-{id}.3mf`.
 */

import { useState } from "react";
import { ApiClient } from "../../lib/api";
import copy from "../../copy";

interface Export3MFProps {
  projectId: number;
  /** The project's display name (the filename slug's source). */
  projectName?: string;
  /** The id of the version being exported — the latest is the default.
   *  The exported mark belongs to THIS id, not necessarily the latest. */
  versionId?: number;
  /** The version's display name (the filename's suffix, `vN`). */
  versionName?: string;
  /** Injectable API client (test seam). Defaults to a same-origin ApiClient. */
  client?: ApiClient;
  /** Fires exactly once, AFTER a successful download. Failed/cancelled
   *  downloads never call it. */
  onExported?: (versionId: number) => void;
}

type ExportState = "idle" | "downloading" | "error";

export function Export3MF({
  projectId,
  projectName = "",
  versionId,
  versionName,
  client,
  onExported,
}: Export3MFProps) {
  const [state, setState] = useState<ExportState>("idle");
  const [error, setError] = useState<string | null>(null);

  const apiClient = client ?? new ApiClient();
  // The filename is the deck's slug contract. `vN` — the version's ordinal,
  // which is its name (`v${id}`) by the timeline's own labelling.
  const downloadName = copy.shell.exportFilename(
    projectName,
    versionName ?? (versionId !== undefined ? `v${versionId}` : "current"),
  );

  const handleExport = async () => {
    setState("downloading");
    setError(null);
    try {
      const blob = await apiClient.downloadModel3MF(projectId);
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = downloadName;
      anchor.click();
      URL.revokeObjectURL(url);
      setState("idle");
      // The download's bytes are in the browser — the completion moment is
      // real. The parent records the mark; if that fails, the export itself
      // has already happened and is never un-done. `undefined` (no version
      // targeted — a fresh project with no version) is a no-op: there is
      // no version to mark and no completion turn to name.
      if (versionId !== undefined) onExported?.(versionId);
    } catch (e) {
      const message = e instanceof Error ? e.message : "3MF export failed";
      setState("error");
      setError(message);
      // A failed export appends no completion turn and sets no mark —
      // onExported is never called on this path.
    }
  };

  return (
    <div className="export-3mf" data-testid="export-3mf">
      <button
        type="button"
        data-testid="export-3mf-button"
        onClick={() => void handleExport()}
        disabled={state === "downloading"}
        aria-label={copy.shell.export}
      >
        {state === "downloading" ? "Exporting…" : copy.shell.export}
      </button>
      {state === "error" && error && (
        <span className="export-3mf-error" data-testid="export-3mf-error">
          {error}
        </span>
      )}
    </div>
  );
}
