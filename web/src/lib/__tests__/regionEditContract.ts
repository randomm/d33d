/**
 * Shared assertion helper for `RegionEditRequest`'s server-side contract
 * (`d33d/app.py`'s `RegionEditRequest`, Pydantic `Field` constraints).
 *
 * `app-layout.test.tsx` mocks `ApiClient.createRegionEdit` at the object
 * level, which hides payload-shape violations — e.g. the ticket #29
 * regression where App.tsx sent `instruction: ""` and every real request
 * 422'd, but the mocked test still passed. This helper re-asserts every
 * server constraint against the actual request body a test captured, so a
 * future contract violation fails here even if the mock itself doesn't care.
 *
 * Constraints mirrored from `RegionEditRequest` (`d33d/app.py`):
 *   - module_ids: 1..MAX_REGION_EDIT_MODULE_IDS non-empty strings
 *   - view_id: present (non-empty string)
 *   - marked_png_base64: non-empty, valid base64, no `data:` prefix
 *   - polygon: at least 3 points
 *   - instruction: non-empty (not whitespace-only)
 */
import { expect } from "vitest";
import { MAX_REGION_EDIT_MODULE_IDS, type RegionEditRequest } from "../api";

const BASE64_RE = /^[A-Za-z0-9+/]*={0,2}$/;

/** Assert `body` satisfies every server-side constraint
 *  `RegionEditRequest` enforces. Throws (via `expect`) on the first
 *  violation, so failures point at the specific broken field. */
export function assertValidRegionEditRequest(body: RegionEditRequest): void {
  expect(Array.isArray(body.module_ids)).toBe(true);
  expect(body.module_ids.length).toBeGreaterThanOrEqual(1);
  expect(body.module_ids.length).toBeLessThanOrEqual(MAX_REGION_EDIT_MODULE_IDS);
  for (const id of body.module_ids) {
    expect(typeof id).toBe("string");
    expect(id.length).toBeGreaterThan(0);
  }

  expect(typeof body.view_id).toBe("string");
  expect(body.view_id.length).toBeGreaterThan(0);

  expect(typeof body.marked_png_base64).toBe("string");
  expect(body.marked_png_base64.length).toBeGreaterThan(0);
  expect(body.marked_png_base64.startsWith("data:")).toBe(false);
  expect(body.marked_png_base64).toMatch(BASE64_RE);

  expect(Array.isArray(body.polygon)).toBe(true);
  expect(body.polygon.length).toBeGreaterThanOrEqual(3);

  expect(typeof body.instruction).toBe("string");
  expect(body.instruction.trim().length).toBeGreaterThan(0);
}
