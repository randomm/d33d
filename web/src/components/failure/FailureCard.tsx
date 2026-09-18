/**
 * FailureCard — the app-level error card in the conversation (issue #82).
 *
 * Presentational: the parent (App) owns the error state. Renders the
 * message and, when present, a collapsible detail block — every caller
 * passes a non-retryable, plain card (stream failures render as a
 * conversation turn via FailureTurn, issue #124, so the old stream-only
 * Retry branch was removed in issue #175).
 *
 * Extracted verbatim from App.tsx's inline `app-error` block (issue #116
 * — surface extraction).
 */

import type { DisplayError } from "../../lib/errorMapping";

interface FailureCardProps {
  /** The structured error to display. */
  error: DisplayError;
}

export function FailureCard({ error }: FailureCardProps) {
  return (
    <div className="app-error" data-testid="app-error" role="alert">
      {error.message}
      {error.detail && (
        <details data-testid="app-error-detail" className="app-error-detail">
          <summary>Details</summary>
          {error.detail}
        </details>
      )}
    </div>
  );
}
