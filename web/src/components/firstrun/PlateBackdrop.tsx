/**
 * PlateBackdrop — the build plate drawn to scale behind the first-run screen
 * (issue #128, W14).
 *
 * The constraint arrives as a room, not a warning: a low-contrast outline of
 * the build volume the API reports, captioned with its real numbers. The
 * drawing is derived from the envelope dimensions (the SVG viewBox IS the
 * plate in millimetres), so if the API's numbers change the drawing changes
 * with them — a plate at a fixed aspect with a separately-fetched caption
 * would be the same defect wearing a nicer coat.
 *
 * The keep-out notch is deliberately NOT drawn: the envelope route
 * (GET /api/config/envelope) does not expose it (issue #122 keeps the vendor
 * constant private), and this surface must not hardcode or infer a number it
 * has not established.
 *
 * The outline uses the hairline colour at very low opacity — a backdrop, not
 * a model. The marker colour never appears here: it is the region marker and
 * nothing else.
 */

import copy from "../../copy";

interface PlateBackdropProps {
  /** Build envelope from GET /api/config/envelope — millimetres. */
  x: number;
  y: number;
  z: number;
  /** True once the values have been confirmed against the machine; false
   *  until then — the caption must carry that uncertainty, never hide it. */
  verified: boolean;
}

export function PlateBackdrop({ x, y, z, verified }: PlateBackdropProps) {
  // The plate's top view: x wide, z deep (the print head's travel plane).
  // The viewBox is the envelope itself — the drawing scales with the API.
  return (
    <div
      className="plate-backdrop-wrap"
      data-testid="plate-backdrop"
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "center",
          gap: 4,
          maxHeight: "100%",
        }}
      >
        {/* The plate outline — top view (x × z), very low contrast. */}
        <svg
          className="plate-backdrop"
          data-testid="plate-backdrop-svg"
          viewBox={`0 0 ${x} ${z}`}
          style={{
            width: "min(44vh, 46vw)",
            height: "auto",
            display: "block",
          }}
          aria-hidden
        >
          <rect
            className="plate-outline"
            x={2}
            y={2}
            width={x - 4}
            height={z - 4}
            fill="none"
            stroke="var(--color-hairline)"
            strokeWidth={1.5}
            opacity={0.35}
          />
        </svg>
        {/* The caption: the API's numbers, formatted once in the deck. */}
        <span
          className="plate-caption"
          data-testid="plate-caption"
          style={{
            fontFamily: "var(--font-mono)",
            fontSize: "var(--font-size-sm)",
            color: "var(--color-faint)",
          }}
        >
          {copy.firstRun.plateCaption(x, y, z)}
          {!verified && copy.firstRun.plateCaptionUnverified}
        </span>
        <span
          className="plate-note"
          data-testid="plate-note"
          style={{
            fontFamily: "var(--font-ui)",
            fontSize: "var(--font-size-xs)",
            color: "var(--color-faint)",
            maxWidth: 316,
            textAlign: "center",
          }}
        >
          {copy.firstRun.plateNote}
        </span>
      </div>
    </div>
  );
}
