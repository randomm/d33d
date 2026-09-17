/**
 * PassProgress — the design-loop stage indicator (issue #82).
 *
 * Presentational: the parent (App) owns the in-flight flag, the current
 * step label and the elapsed-seconds counter. Extracted verbatim from
 * App.tsx's inline `design-loop-progress` block (issue #116 — surface
 * extraction). The step-label mapping stays exactly as it was inline:
 * unknown/empty values render the generic label, never the raw token.
 */

interface PassProgressProps {
  /** The raw step token from the design-loop progress frame (may be null
   *  or an unknown value — both render the generic label). */
  step: string | null;
  /** Elapsed seconds since the pass started. */
  elapsed: number;
}

export function PassProgress({ step, elapsed }: PassProgressProps) {
  return (
    <div className="design-loop-progress" data-testid="design-loop-progress" role="status">
      <span data-testid="design-loop-stage">
        {step === "design-loop-start"
          ? "Generating design…"
          : step === "design-loop-pass"
            ? "Rendering and checking…"
            : step === "version-created"
              ? "Saving version…"
              : "Working on your design…"}
      </span>
      <div
        className="design-loop-progress-bar"
        data-testid="design-loop-progress-bar"
        aria-hidden="true"
      >
        <span className="design-loop-progress-indicator" />
      </div>
      <span data-testid="design-loop-elapsed">{elapsed}s</span>
    </div>
  );
}
