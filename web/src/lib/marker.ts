/**
 * The region marker colour.
 *
 * This is NOT a design token. It is a functional constant that happens to be a
 * colour: it is composited into an image a vision model then reads, and those
 * models are measurably fragile about marker colour — a red-to-blue swap can flip
 * correctness. Treating it as part of the palette is the mistake that gets it
 * retinted by a well-meaning theming pass.
 *
 * It therefore has exactly one home — this file — and is deliberately ABSENT from
 * styles.css. Anything that needs it in CSS-land takes it from here as an inline
 * style. There is no `--color-marker` token, on purpose.
 *
 * Alpha variants go through `markerAlpha` rather than being written out, because
 * `rgba(255,51,0,…)` is the same constant in a form no grep for `#FF3300` can see.
 */

export const MARKER_COLOR = "#FF3300";

/** The same value, decomposed — the only legitimate source for alpha variants. */
export const MARKER_RGB: readonly [number, number, number] = [255, 51, 0];

/** `markerAlpha(0.15)` → "rgba(255, 51, 0, 0.15)". */
export const markerAlpha = (alpha: number): string =>
  `rgba(${MARKER_RGB[0]}, ${MARKER_RGB[1]}, ${MARKER_RGB[2]}, ${alpha})`;
