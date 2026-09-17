/**
 * FailureCard — the app-level error card in the conversation (issue #82).
 *
 * Presentational: the parent (App) owns the error state, the error kind
 * (the Retry control renders only for "stream" errors) and the in-flight
 * flag that disables Retry. Extracted verbatim from App.tsx's inline
 * `app-error` block (issue #116 — surface extraction).
 */

import type { DisplayError } from "../../lib/errorMapping";

interface FailureCardProps {
  /** The structured error to display. */
  error: DisplayError;
  /** The kind of error ("stream" = design-loop/chat stream, "other" = e.g.
   *  a region edit or photo upload failure) — Retry only renders for
   *  "stream" errors (issue #82). */
  kind: "stream" | "other";
  /** True while a design loop is in flight — disables the Retry button. */
  inFlight: boolean;
  /** Invoked when Retry is clicked. */
  onRetry: () => void;
}

export function FailureCard({ error, kind, inFlight, onRetry }: FailureCardProps) {
  return (
    <div className="app-error" data-testid="app-error" role="alert">
      {error.message}
      {error.detail && (
        <details data-testid="app-error-detail" className="app-error-detail">
          <summary>Details</summary>
          {error.detail}
        </details>
      )}
      {error.retryable && kind === "stream" && (
        <button
          type="button"
          className="app-error-retry-btn"
          data-testid="app-error-retry"
          onClick={onRetry}
          disabled={inFlight}
        >
          Retry
        </button>
      )}
    </div>
  );
}
