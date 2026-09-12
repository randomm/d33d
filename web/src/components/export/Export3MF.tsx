/**
 * Export3MF — 3MF download-trigger button (validation-status panel).
 *
 * Scope note (issue #6, D6): the backend has NO 3MF-generation HTTP
 * endpoint yet. The 3MF pipeline lives in `d33d/print_validation.py`
 * (ticket #3) and is not exposed over HTTP in this ticket's scope — the
 * FastAPI route that `ApiClient.downloadModel3MF` calls
 * (`GET /api/projects/{id}/model.3mf`) does not exist. This component is
 * the download-trigger UI + API-client plumbing only; until the backend
 * route lands, clicking the button surfaces the resulting 404 as an error
 * state rather than crashing. Wiring the real backend endpoint is a
 * follow-up ticket.
 *
 * 3MF is a download artifact, never rendered inline (see ModelViewer's
 * "3MF exclusion" contract) — this component never touches the viewer.
 */

import { useState } from "react";
import { ApiClient } from "../../lib/api";

interface Export3MFProps {
  projectId: number;
  /** Injectable API client (test seam). Defaults to a same-origin ApiClient. */
  client?: ApiClient;
  /** Injectable filename builder — defaults to `model-{projectId}.3mf`. */
  fileName?: string;
}

type ExportState = "idle" | "downloading" | "error";

export function Export3MF({ projectId, client, fileName }: Export3MFProps) {
  const [state, setState] = useState<ExportState>("idle");
  const [error, setError] = useState<string | null>(null);

  const apiClient = client ?? new ApiClient();
  const downloadName = fileName ?? `model-${projectId}.3mf`;

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
    } catch (e) {
      const message = e instanceof Error ? e.message : "3MF export failed";
      setState("error");
      setError(message);
    }
  };

  return (
    <div className="export-3mf" data-testid="export-3mf">
      <button
        type="button"
        data-testid="export-3mf-button"
        onClick={() => void handleExport()}
        disabled={state === "downloading"}
        aria-label="Export 3MF"
      >
        {state === "downloading" ? "Exporting…" : "Export 3MF"}
      </button>
      {state === "error" && error && (
        <span className="export-3mf-error" data-testid="export-3mf-error">
          {error}
        </span>
      )}
    </div>
  );
}
