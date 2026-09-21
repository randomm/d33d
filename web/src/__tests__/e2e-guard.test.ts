/**
 * E2E skip-server guard regression test (issue #207).
 *
 * The guard's comparison logic lives in a pure, side-effect-free module
 * (web/src/e2e-guard.ts) so it can be unit-tested without loading
 * playwright.config.ts, whose top-level mkdtemp + process handlers make it
 * unsuitable as a test target. The helper takes the D33D_DATA_DIR value as a
 * parameter (no env mutation needed between cases), which also removes the
 * env-leak hazard the issue's test-surface note calls out.
 *
 * Inputs: unset, empty string, whitespace-only (fires); a real path (does
 * not fire).
 */

import { describe, it, expect } from "vitest";
import { shouldGuardE2eSkipServer } from "../e2e-guard";

describe("shouldGuardE2eSkipServer (issue #207)", () => {
  it("fires when D33D_DATA_DIR is unset (undefined)", () => {
    expect(shouldGuardE2eSkipServer(undefined)).toBe(true);
  });

  it("fires when D33D_DATA_DIR is an empty string", () => {
    expect(shouldGuardE2eSkipServer("")).toBe(true);
  });

  it("fires when D33D_DATA_DIR is whitespace-only", () => {
    expect(shouldGuardE2eSkipServer("   ")).toBe(true);
    expect(shouldGuardE2eSkipServer("\t")).toBe(true);
    expect(shouldGuardE2eSkipServer("\n ")).toBe(true);
  });

  it("does not fire when D33D_DATA_DIR is a real path", () => {
    expect(shouldGuardE2eSkipServer("/tmp/d33d-e2e-throwaway")).toBe(false);
  });

  it("does not fire for any non-blank value, even one that resolves to ~/.d33d", () => {
    // The guard validates declared intent, not the value: even a path that
    // ends up at the live default (via symlink/tilde) means the operator
    // set the variable explicitly, so the guard stays off.
    expect(shouldGuardE2eSkipServer("~/.d33d")).toBe(false);
    expect(shouldGuardE2eSkipServer("/Users/janni/.d33d")).toBe(false);
  });

  it("treats whitespace around a real path as set (only fully-blank fires)", () => {
    expect(shouldGuardE2eSkipServer("  /tmp/d33d-e2e  ")).toBe(false);
  });
});
