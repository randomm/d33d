/**
 * The backend's tier-3 offer/ack sentence builders, rendered here as the
 * exact wire strings `d33d/confirm_offer.py` emits (issue #265).
 *
 * The SPA has no build step that imports Python, so the design-contract pin
 * ("the backend's tier-3 offer/ack mm spelling agrees with the deck's
 * mm()") compares these strings against the deck's `mm()` renderings.
 *
 * These are MIRRORS of `offer_sentence` / `ack_sentence` in
 * `d33d/confirm_offer.py` (the #250 tripwire pattern in reverse: the deck
 * mirrors the server, this file mirrors the server on the deck side so a
 * web test can assert the two agree). If the backend templates or value
 * spellings change, change these here AND in the Python source AND in
 * `tests/versioning/test_issue250_offer.py` — the Python side is the
 * source of truth, this file is what keeps the web pin honest.
 */

/** Mirror of `d33d.confirm_offer.mm_formatted` — byte-identical to the
 * deck's `mm()` (one decimal + U+202F + `mm`). */
const mmFormatted = (value: number): string => `${value.toFixed(1)}\u202fmm`;

/** The value's bare spelling (`d33d.confirm_offer.format_param_value`,
 * `f"{value:g}"`): non-numeric values verbatim, numbers in general form
 * (`40` → "40", `1.5` → "1.5", `3.0` → "3"). */
const formatValue = (value: number | string | boolean): string =>
  typeof value === "number" ? String(Math.round(value * 1e12) / 1e12) : String(value);

interface OfferEntry {
  name: string;
  label?: string;
  value: number | string | boolean;
  /**
   * The param's metadata unit / axis (the `offer_entry` graft: the
   * model-declared `unit` — the design-state entry's default `"mm"`
   * never counts — or a declared axis). `"mm"` / an axis → the
   * mm-formatted spelling; absent / non-mm → the bare
   * `format_param_value` spelling (the #265 rule).
   */
  meta_unit?: string;
  param_axis?: string;
}

/** Mirror of `d33d.confirm_offer.mm_value_str`: the mm-formatted spelling
 * ONLY for a genuinely-mm param, the bare value otherwise. */
function mmValueStr(entry: OfferEntry): string {
  if (typeof entry.value !== "number") return formatValue(entry.value);
  const isMm =
    (typeof entry.meta_unit === "string" && entry.meta_unit === "mm") ||
    (typeof entry.param_axis === "string" && entry.param_axis !== "");
  return isMm ? mmFormatted(entry.value) : formatValue(entry.value);
}

/**
 * Mirror of `d33d.confirm_offer.offer_sentence`'s deterministic template
 * (no model sentence — the wire path the design-contract test pins).
 */
export function offer_sentence(entry: OfferEntry): string {
  const value = mmValueStr(entry);
  const label = entry.label ?? entry.name;
  return `I assumed ${value} for ${label}. Want it different?`;
}

/** Mirror of `d33d.confirm_offer.ack_sentence`. */
export function ack_sentence(entry: OfferEntry): string {
  const value = mmValueStr(entry);
  const label = entry.label ?? entry.name;
  return `Got it — ${label} stays ${value}.`;
}
