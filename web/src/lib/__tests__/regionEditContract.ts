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
 *   - module_ids: 0..MAX_REGION_EDIT_MODULE_IDS non-empty strings (EMPTY is
 *     valid since issue #98 — a streamed unnamed STL still selects,
 *     grounded by the marked PNG alone)
 *   - view_id: one of REGION_EDIT_VIEW_IDS (server field-validator, not just
 *     the TS union type — a runtime mock can still produce a bad string)
 *   - marked_png_base64: non-empty, valid base64, no `data:` prefix, decoded
 *     size within MAX_REGION_EDIT_IMAGE_BYTES (route-level check, not on
 *     the Pydantic model, but still a real 413 the client must avoid)
 *   - point: present (the single picked point in the view's CSS-pixel
 *     space — the lasso polygon was replaced by this in issue #98)
 *   - instruction: non-empty (not whitespace-only)
 */
import { expect } from "vitest";
import {
  MAX_REGION_EDIT_IMAGE_BYTES,
  MAX_REGION_EDIT_MODULE_IDS,
  REGION_EDIT_VIEW_IDS,
  type RegionEditRequest,
} from "../api";

const BASE64_RE = /^[A-Za-z0-9+/]*={0,2}$/;

/** Assert `body` satisfies every server-side constraint
 *  `RegionEditRequest` enforces. Throws (via `expect`) on the first
 *  violation, so failures point at the specific broken field. */
export function assertValidRegionEditRequest(body: RegionEditRequest): void {
  expect(Array.isArray(body.module_ids)).toBe(true);
  // 0 is VALID (issue #98): the marked point is the grounding, the module
  // ids are supplementary context.
  expect(body.module_ids.length).toBeLessThanOrEqual(MAX_REGION_EDIT_MODULE_IDS);
  for (const id of body.module_ids) {
    expect(typeof id).toBe("string");
    expect(id.length).toBeGreaterThan(0);
  }

  expect(typeof body.view_id).toBe("string");
  expect(REGION_EDIT_VIEW_IDS).toContain(body.view_id);

  expect(typeof body.marked_png_base64).toBe("string");
  expect(body.marked_png_base64.length).toBeGreaterThan(0);
  expect(body.marked_png_base64.startsWith("data:")).toBe(false);
  expect(body.marked_png_base64).toMatch(BASE64_RE);
  // Decoded byte size, not the base64 string length (base64 is ~4/3 the
  // decoded size) — matches the route's own `base64.b64decode(...)` +
  // `len(image_bytes)` check.
  const decodedBytes = Math.floor((body.marked_png_base64.length * 3) / 4);
  expect(decodedBytes).toBeLessThanOrEqual(MAX_REGION_EDIT_IMAGE_BYTES);

  expect(typeof body.point).toBe("object");
  expect(body.point).not.toBeNull();
  expect(typeof body.point.x).toBe("number");
  expect(typeof body.point.y).toBe("number");

  expect(typeof body.instruction).toBe("string");
  expect(body.instruction.trim().length).toBeGreaterThan(0);
}
