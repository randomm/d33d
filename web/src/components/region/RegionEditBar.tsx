/**
 * Region edit bar — the inline instruction bar that follows a point pick
 * (issue #129, extracted from App.tsx by issue #195, the #116/W8 lineage).
 *
 * Extracted verbatim from the stage host's inline IIFE: the bar is anchored
 * to the pin, not to the viewport's bottom edge — placed in the quadrant
 * OPPOSITE the pin relative to the viewport centre, clamped to the viewport
 * (and the leader line reverses when the clamp kicks in). The leader line
 * is a thin 1px line in the marker colour, the only place the marker
 * legitimately belongs in the bar. The bar dims while the pin is orbiting
 * or cleared.
 *
 * Behaviour-neutral: the quadrant-flip, clamping and leader-flip branches
 * moved here as-is, driven by the same props the IIFE closed over.
 */
import { MARKER_COLOR } from "../../lib/marker";
import type { RegionEditViewId } from "../../lib/api";
import { Z_INDEX } from "../../App";
import copy from "../../copy";

export interface RegionEditBarSelection {
  thumbnail: string;
  viewId: RegionEditViewId;
  moduleIds: string[];
  point: { x: number; y: number };
}

interface RegionEditBarProps {
  /** The picked point in the viewport (the bar's anchor). */
  selection: RegionEditBarSelection;
  /** The bar's sizing source — the stage element's measured box. */
  viewportSize: { width: number; height: number };
  /** The free-text instruction typed into the bar (App's local state). */
  text: string;
  onTextChange: (text: string) => void;
  /** Submit (the form's onSubmit AND the Apply button). */
  onSubmit: () => void;
  /** Cancel (the button AND the input's Escape key). */
  onCancel: () => void;
  /** The pin is orbiting (orbit gesture in flight). */
  orbitingPin: boolean;
  /** The pin is cleared (pose crossed the threshold). */
  orbitClearedPin: boolean;
}

export function RegionEditBar({
  selection,
  viewportSize,
  text,
  onTextChange,
  onSubmit,
  onCancel,
  orbitingPin,
  orbitClearedPin,
}: RegionEditBarProps) {
  const pin = selection.point;
  const { width: vw, height: vh } = viewportSize;
  // The bar's width is fixed at 320px (the spec's gap-gate answer: a
  // fixed width, clamped against the viewport). Height is derived from
  // the bar's content (thumbnail 32 + input + buttons + module chip +
  // hint); the tests pin the width, so the height is a measurement —
  // the clamp uses the viewport's available space, not a magic height.
  const BAR_WIDTH = 320;
  const BAR_GAP = 12; // gap between pin and bar edge
  const cx = vw / 2;
  const cy = vh / 2;
  // Quadrant: opposite the pin relative to the viewport centre.
  // pin above-centre-left → bar bottom-right, etc.
  const pinLeft = pin.x < cx;
  const pinUp = pin.y < cy;
  // Default position: the quadrant opposite the pin.
  // If pin is upper-left, bar goes lower-right, and vice versa.
  let barLeft: number;
  let barTop: number;
  // The bar's box: width BAR_WIDTH, height estimated at 120px
  // (32px thumbnail + 8px gap + 24px input row + 4px gap + 16px module
  // chip + 4px gap + 20px hint ≈ 90px content + 16px padding + border).
  const BAR_HEIGHT = 120;
  if (pinUp) {
    barTop = cy + BAR_GAP; // bar in lower half
  } else {
    barTop = cy - BAR_GAP - BAR_HEIGHT; // bar in upper half
  }
  if (pinLeft) {
    barLeft = cx + BAR_GAP; // bar in right half
  } else {
    barLeft = cx - BAR_GAP - BAR_WIDTH; // bar in left half
  }
  // Clamp: the bar's box must stay within the viewport.
  const clampedLeft = Math.max(4, Math.min(barLeft, vw - BAR_WIDTH - 4));
  const clampedTop = Math.max(4, Math.min(barTop, vh - BAR_HEIGHT - 4));
  const clamped = clampedLeft !== barLeft || clampedTop !== barTop;
  // The leader line goes from the pin to the bar's nearest edge.
  // When clamped, the leader reverses: it points FROM the bar TO the pin,
  // not from the pin to the bar's default (uncropped) position.
  const leaderStartX = pin.x;
  const leaderStartY = pin.y;
  const leaderEndX = clamped ? pin.x : (pinLeft ? clampedLeft : clampedLeft + BAR_WIDTH);
  const leaderEndY = clamped ? pin.y : (pinUp ? clampedTop + BAR_HEIGHT : clampedTop);
  const dimmed = orbitingPin || orbitClearedPin;
  const moduleChip = selection.moduleIds.length > 0
    ? selection.moduleIds[0]
    : null;
  return (
    <>
      {/* The leader line — a thin 1px line in the marker colour.
          The bar is at z-index 30; the leader is part of the bar's
          visual unit and sits at the same z-index. The line goes from
          the pin to the bar's nearest edge (default) or from the bar
          to the pin (flipped — the pin is the "source" the line points
          back to). */}
      <svg
        data-testid="region-edit-leader"
        style={{
          position: "absolute",
          left: 0,
          top: 0,
          width: vw,
          height: vh,
          pointerEvents: "none",
          zIndex: Z_INDEX.pinAndBar,
        }}
        aria-hidden="true"
      >
        <line
          x1={leaderStartX}
          y1={leaderStartY}
          x2={leaderEndX}
          y2={leaderEndY}
          stroke={MARKER_COLOR}
          strokeWidth={1}
          strokeOpacity={0.6}
        />
      </svg>
      <form
        className="region-edit-bar"
        data-testid="region-edit-bar"
        role="group"
        data-flipped={clamped ? "true" : "false"}
        style={{
          position: "absolute",
          left: clampedLeft,
          top: clampedTop,
          width: BAR_WIDTH,
          display: "flex",
          flexDirection: "column",
          gap: 6,
          padding: "8px 12px",
          backgroundColor: "rgba(0, 0, 0, 0.8)",
          borderRadius: 8,
          boxSizing: "border-box",
          zIndex: Z_INDEX.pinAndBar,
          opacity: dimmed ? 0.5 : 1,
          transition: "opacity 120ms ease",
        }}
        onSubmit={(e) => {
          e.preventDefault();
          onSubmit();
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
          <img
            src={selection.thumbnail}
            alt={`pending selection on ${selection.viewId}`}
            className="pending-selection-thumbnail"
            data-testid="pending-selection-thumbnail"
            style={{
              width: 32,
              height: 32,
              maxWidth: 200,
              objectFit: "cover",
              flex: "0 0 auto",
            }}
          />
          <input
            type="text"
            className="region-edit-input"
            data-testid="region-edit-input"
            placeholder={copy.region.placeholder}
            value={text}
            onChange={(e) => onTextChange(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Escape") onCancel();
            }}
            autoFocus
            style={{
              flex: "1 1 auto",
              minWidth: 0,
              padding: "4px 8px",
              border: "none",
              borderRadius: 4,
              backgroundColor: "rgba(255, 255, 255, 0.95)",
              color: "#1f2328",
            }}
          />
        </div>
        <div style={{ display: "flex", gap: 8 }}>
          <button
            type="submit"
            className="region-edit-apply-btn"
            data-testid="region-edit-apply-btn"
            aria-label={copy.region.apply}
            disabled={text.trim().length === 0}
            style={{
              flex: "0 0 auto",
              padding: "4px 12px",
              border: "none",
              borderRadius: 4,
              backgroundColor: "#0969da",
              color: "#ffffff",
              cursor: text.trim().length === 0 ? "not-allowed" : "pointer",
              opacity: text.trim().length === 0 ? 0.5 : 1,
            }}
          >
            {copy.region.apply}
          </button>
          <button
            type="button"
            className="region-edit-cancel-btn"
            data-testid="pending-selection-cancel-btn"
            aria-label={copy.region.cancel}
            onClick={onCancel}
            style={{
              flex: "0 0 auto",
              padding: "4px 12px",
              border: "none",
              borderRadius: 4,
              backgroundColor: "rgba(255, 255, 255, 0.2)",
              color: "#ffffff",
              cursor: "pointer",
            }}
          >
            {copy.region.cancel}
          </button>
        </div>
        {/* The resolved module chip + the pose hint. The module name
            is in mono (the raw detail); the sentence is in the UI face. */}
        <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          {moduleChip !== null && (
            <span
              data-testid="region-edit-module-chip"
              style={{
                fontFamily: "var(--font-mono)",
                fontSize: 11,
                color: "var(--color-fg-2)",
                padding: "2px 6px",
                background: "rgba(255,255,255,0.08)",
                borderRadius: 4,
                display: "inline-block",
                width: "fit-content",
              }}
            >
              {moduleChip}
              {" "}
              {copy.region.resolvedTo}
            </span>
          )}
          <span
            data-testid="region-edit-pose-hint"
            style={{ fontSize: 11, color: "var(--color-muted)" }}
          >
            {copy.region.poseHint}
          </span>
          {orbitClearedPin && (
            <span
              data-testid="region-edit-cleared-hint"
              style={{
                fontSize: 11,
                color: "var(--color-muted)",
                marginTop: 2,
              }}
            >
              {copy.region.clearedHint}
            </span>
          )}
        </div>
      </form>
    </>
  );
}
