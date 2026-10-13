/**
 * Issue #433: the reply sentences for the done frame's `feature_mismatches`
 * list. The server sends DATA only (`{noun, stated_mm, made_mm, label}` per
 * mismatching feature); the sentence is written here from `copy.ts`, so the
 * user-facing prose has exactly one source.
 */
import { confirmOffer } from "./copy";

export function featureMismatchSentences(raw: unknown): string[] {
  if (!Array.isArray(raw)) return [];
  const out: string[] = [];
  for (const item of raw) {
    if (item === null || typeof item !== "object") continue;
    const r = item as Record<string, unknown>;
    if (
      typeof r.noun !== "string" ||
      typeof r.label !== "string" ||
      typeof r.stated_mm !== "number" ||
      typeof r.made_mm !== "number"
    ) {
      continue;
    }
    out.push(confirmOffer.featureMismatch(r.noun, r.stated_mm, r.label, r.made_mm));
  }
  return out;
}
