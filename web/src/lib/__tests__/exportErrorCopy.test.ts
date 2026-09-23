/**
 * Unit tests for displayExportError (issue #233).
 *
 * The component-level suite (export-3mf.test.tsx) covers the real
 * error classes; this suite pins the mapping rule directly and the
 * inherited-prototype guard: an error_class that names an
 * Object.prototype member ("constructor" / "toString" / "__proto__")
 * must fall through to the generic sentence, never resolve to an
 * inherited function.
 */

import { describe, it, expect } from "vitest";
import { ApiError } from "../api";
import { displayExportError } from "../exportErrorCopy";
import { copy } from "../../copy";

describe("displayExportError", () => {
  it.each(["constructor", "toString", "__proto__"])(
    "errorClass %s falls through to the generic sentence (no inherited-prototype leak)",
    (errorClass) => {
      const d = displayExportError(
        new ApiError(502, { error_class: errorClass }, errorClass),
      );
      expect(d.message).toBe(copy.export3mf.failed);
      expect(typeof d.message).toBe("string");
    },
  );

  it("a validation class (slice) maps to the failure.reasons sentence", () => {
    const d = displayExportError(new ApiError(502, "raw detail", "slice"));
    expect(d.message).toBe(copy.failure.reasons.slice);
  });

  it("'conflict' maps to export3mf.conflict", () => {
    const d = displayExportError(new ApiError(409, { detail: "in flight" }, "conflict"));
    expect(d.message).toBe(copy.export3mf.conflict);
  });

  it("an unmapped error_class ('unknown') maps to the generic sentence", () => {
    const d = displayExportError(new ApiError(502, "raw", "unknown"));
    expect(d.message).toBe(copy.export3mf.failed);
  });

  it("an ApiError with no error_class maps to the generic sentence", () => {
    const d = displayExportError(new ApiError(404, "Not Found"));
    expect(d.message).toBe(copy.export3mf.failed);
  });

  it("a non-ApiError rejection maps to the generic sentence", () => {
    const d = displayExportError(new Error("network down"));
    expect(d.message).toBe(copy.export3mf.failed);
  });

  it("keeps the raw detail for collapse display", () => {
    const d = displayExportError(new ApiError(502, "validation failed — raw", "slice"));
    expect(d.detail).toBe("validation failed — raw");
  });
});
