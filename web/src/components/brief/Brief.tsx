/**
 * Brief — the "what are we building" overlay (top-left).
 *
 * Presentational: the parent (App) owns the responsive chip/full-mode
 * decision. Extracted verbatim from App.tsx's inline `brief-panel`
 * (issue #116 — surface extraction); today it renders only the eyebrow
 * line — the parameter rows arrive with the Brief content ticket (W9),
 * which will delete PinnedParamStrip.
 */

import copy from "../../copy";

interface BriefProps {
  /** True when the window is below the chip threshold — the Brief
   *  renders compact, not as a full panel. */
  isChip: boolean;
  /** The top/left inset from the stage edge (px). */
  inset: number;
}

export function Brief({ isChip, inset }: BriefProps) {
  return (
    <div
      className="brief-panel"
      data-testid="brief-panel"
      data-mode={isChip ? "chip" : "full"}
      style={{
        position: "absolute",
        top: inset,
        left: inset,
        zIndex: 10,
        padding: isChip ? "8px 12px" : "16px",
        borderRadius: 8,
        background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
        color: "var(--color-fg)",
        maxWidth: isChip ? "none" : 420,
      }}
    >
      {copy.brief.eyebrow}
    </div>
  );
}
