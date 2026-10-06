/**
 * Unit tests for lib/versionOrdinals.ts (issue #387).
 *
 * The shared helper must reproduce the Filmstrip's #352 id → ordinal map
 * exactly: 1-based position in the oldest-first list, first occurrence wins
 * on a duplicate id. The Filmstrip's existing tests (filmstrip.test.tsx)
 * are the golden fixtures; these pin the helper's own contract.
 */

import { describe, it, expect } from "vitest";
import type { VersionTimelineEntry } from "../api";
import { versionOrdinals } from "../versionOrdinals";

function entry(id: number, name: string): VersionTimelineEntry {
  return {
    id,
    name,
    source_kind: "llm",
  } as unknown as VersionTimelineEntry;
}

describe("versionOrdinals (lib/versionOrdinals.ts)", () => {
  it("maps each id to its 1-based position in the oldest-first list", () => {
    const versions = [entry(1, "v1"), entry(2, "v2"), entry(3, "v3"), entry(7, "v7")];
    expect(versionOrdinals(versions)).toEqual(
      new Map([
        [1, 1],
        [2, 2],
        [3, 3],
        [7, 4],
      ]),
    );
  });

  it("the ordinal follows list position, never the DB row id", () => {
    // id 64 at position 1 — the QA 2026-10-04 symptom fixture.
    const versions = [entry(64, "first"), entry(65, "second")];
    const ordinals = versionOrdinals(versions);
    expect(ordinals.get(64)).toBe(1);
    expect(ordinals.get(65)).toBe(2);
  });

  it("first occurrence wins on a duplicate id (a later slot must not override)", () => {
    const versions = [entry(5, "first"), entry(6, "mid"), entry(5, "dup"), entry(7, "last")];
    const ordinals = versionOrdinals(versions);
    expect(ordinals.get(5)).toBe(1); // first slot wins — NOT 3
    expect(ordinals.get(6)).toBe(2);
    expect(ordinals.get(7)).toBe(4);
    expect(ordinals.size).toBe(3);
  });

  it("an empty list maps to an empty map", () => {
    const ordinals = versionOrdinals([]);
    expect(ordinals.size).toBe(0);
    expect(ordinals.has(1)).toBe(false);
  });

  it("an unknown id is simply absent from the map (no fallback to the raw id)", () => {
    const ordinals = versionOrdinals([entry(1, "v1")]);
    expect(ordinals.has(64)).toBe(false);
    expect(ordinals.get(64)).toBeUndefined();
  });

  it("import slots still count toward positions (the import rule is a Filmstrip render concern, not the map's)", () => {
    const versions = [
      { ...entry(1, "imported"), source_kind: "import" } as VersionTimelineEntry,
      entry(2, "v2"),
    ];
    expect(versionOrdinals(versions)).toEqual(new Map([[1, 1], [2, 2]]));
  });
});
