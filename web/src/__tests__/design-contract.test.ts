/**
 * The design contract.
 *
 * These assertions are the design spec in executable form. A failing test cannot
 * be paraphrased, which is the point: the interface decisions that matter are the
 * ones a future change is most likely to quietly undo.
 *
 * STANDING RULE — read before adding anything here.
 *
 *   This file starts with only the assertions that pass today. Every interface
 *   ticket APPENDS its own assertion to this file, in the same PR that makes it
 *   pass. Never land a red assertion ahead of its implementation: main stays
 *   green and the contract accumulates.
 *
 * Assertions still owed, with the ticket that owns each. Use the helpers below;
 * each one should be a two-liner.
 *
 *   #107 no component renders a placeholder model on load
 *   W4   the string "Waiting for render" appears nowhere in src
 *   W7   no module exports a fixed viewer width or height
 *   W7   no component sets a z-index outside the four layers (0, 10, 20, 30)
 *   W8   App.tsx imports all six surface components and holds none of their markup
 *   W9   the Brief never renders a numeric value for provenance "unknown"
 *   W10  no chat message contains OpenSCAD source
 *   W11  no indeterminate progress animation exists
 *   W11  no CSS animation is infinite except the one attested stage ring
 *   W12  no failure component imports MARKER_COLOR
 *   W13  the filmstrip is absent, not empty, when a project has no versions
 *   W13  no version surface renders a commit hash or branch name
 *
 * Source-level assertions (the ones that read files) are deliberately crude. They
 * are tripwires, not type checking — they catch the reintroduction of a thing the
 * team decided against, which is exactly the failure mode a unit test misses.
 */

import { describe, it, expect } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";

import copy, { mm } from "../copy";
import { MARKER_COLOR, MARKER_RGB, markerAlpha } from "../lib/marker";

const SRC = join(dirname(fileURLToPath(import.meta.url)), "..");

/** Every source file under web/src, excluding tests and build output. */
function srcFiles(): string[] {
  const out: string[] = [];
  const walk = (dir: string): void => {
    for (const entry of readdirSync(dir)) {
      const full = join(dir, entry);
      if (statSync(full).isDirectory()) {
        if (entry === "__tests__" || entry === "node_modules") continue;
        walk(full);
        continue;
      }
      if (/\.(ts|tsx|css)$/.test(entry)) out.push(full);
    }
  };
  walk(SRC);
  return out;
}

/** Files (repo-relative) whose contents match `pattern`. */
function filesMatching(pattern: RegExp): string[] {
  return srcFiles()
    .filter((f) => pattern.test(readFileSync(f, "utf8")))
    .map((f) => relative(SRC, f))
    .sort();
}

const stylesheet = (): string => readFileSync(join(SRC, "styles.css"), "utf8");

describe("design contract", () => {
  /* ---------------------------------------------------------------- W1 */

  it("every design token resolves in styles.css", () => {
    const css = stylesheet();
    const tokens = [
      "--color-canvas",
      "--color-panel",
      "--color-recess",
      "--color-hairline",
      "--color-fg",
      "--color-fg-2",
      "--color-muted",
      "--color-faint",
      "--color-live",
      "--color-blocked",
      "--radius",
      "--radius-sm",
      "--font-ui",
      "--font-mono",
    ];
    const missing = tokens.filter((t) => !css.includes(`${t}:`));
    expect(missing).toEqual([]);
  });

  it("blocked states are ochre, because red is spoken for", () => {
    expect(stylesheet()).toMatch(/--color-blocked:\s*#D2A63C/i);
  });

  /* ---------------------------------------------------------------- W2 */

  it("the copy deck exports every documented surface", () => {
    expect(Object.keys(copy).sort()).toEqual([
      "brief",
      "failure",
      "firstPass",
      "firstRun",
      "history",
      "passCard",
      "progress",
      "region",
      "shell",
    ]);
  });

  it("an unestablished value is a phrase, never a number or a dash", () => {
    // Never-stated and not-yet-measured are both real states. A blank is honest;
    // a plausible-looking number is the house anti-pattern.
    for (const value of [
      copy.brief.unknownValue,
      copy.brief.awaitingFirstMeasure,
      copy.brief.remeasuring,
    ]) {
      expect(value).not.toMatch(/\d/);
      // ...and no dash standing in for a value. An intra-word hyphen is fine
      // ("re-measuring"); a leading or free-standing dash is the thing banned.
      expect(value).not.toMatch(/^[—–-]|\s[—–-]\s/);
    }
    expect(copy.brief.unknownValue).toBe("not established");
  });

  it("a failed pass leaves the Brief describing what still exists", () => {
    // The Brief gains a footer; it never adopts the failed candidate.
    expect(copy.brief.failedFooter("The 380 mm rail")).toContain(
      "Nothing above changed",
    );
    expect(copy.failure.attemptBadge("v4")).toContain("v4 is still yours");
  });

  it("the export names the file and hands over to Orca", () => {
    const name = copy.shell.exportFilename("Curtain rod bracket", "v4");
    expect(name).toBe("curtain-rod-bracket-v4.3mf");
    expect(copy.shell.exportDone(name)).toMatch(/orca/i);
    expect(copy.shell.exportDone(name)).toMatch(/millimetres/i);
  });

  it("failure copy names both the measured value and the limit", () => {
    const body = copy.failure.envelope.body(380, 320);
    expect(body).toContain("380.0");
    expect(body).toContain("320.0");
    expect(body).toMatch(/orca/i);
  });

  it("dimensions format identically wherever they appear", () => {
    // One decimal, narrow no-break space, unit attached — decided once in `mm()`.
    expect(mm(60)).toBe("60.0\u202Fmm");
    expect(copy.failure.envelope.overhang(60)).toBe("60.0\u202Fmm past the edge");
    expect(copy.brief.disagreement(80, 79.2)).toContain("0.8\u202Fmm short");
  });

  it("the repair announcement is grammatical at every attempt", () => {
    const max = 3;
    for (let n = 1; n <= max; n += 1) {
      const line = copy.progress.repairAttempt(n, max, "Moved the bore 6 mm up.");
      expect(line).toContain(`Attempt ${n} of ${max}`);
      expect(line).not.toMatch(/\bAttempt \d+ try\b/); // the shape that shipped broken
    }
    expect(copy.progress.repairAttempt(2, max, "x")).toContain("1 try left");
    expect(copy.progress.repairAttempt(3, max, "x")).toMatch(/last one/);
  });

  /* --------------------------------------------------------------- W17 */

  it("the marker colour has exactly one home", () => {
    // #FF3300 is functionally load-bearing: a vision model reads that pixel, and
    // a red-to-blue swap can flip correctness. One definition, imported everywhere.
    expect(filesMatching(/#FF3300/i)).toEqual(["lib/marker.ts"]);
  });

  it("no alpha-composited copy of the marker escapes that home", () => {
    // rgba(255,51,0,…) is the same constant in a form /#FF3300/ cannot see.
    // DimensionCanvas.tsx:588 held one of these; it now calls markerAlpha().
    //
    // marker.ts is EXEMPT rather than required: it is the home, and the only
    // matches inside it are doc comments that name the value on purpose. An
    // assertion that required them would go red the moment someone reworded a
    // comment, with the invariant untouched.
    const strays = filesMatching(/rgba\(\s*255\s*,\s*51\s*,\s*0/i).filter(
      (f) => f !== "lib/marker.ts",
    );
    expect(strays).toEqual([]);
  });

  it("the decomposed marker agrees with the hex it came from", () => {
    // Two representations genuinely exist, because CSS alpha needs the channels.
    // This is the one place where comparing beats counting: nothing stops the
    // two drifting except this.
    const fromChannels = `#${MARKER_RGB.map((c) =>
      c.toString(16).padStart(2, "0"),
    ).join("")}`;
    expect(fromChannels.toUpperCase()).toBe(MARKER_COLOR.toUpperCase());
    expect(markerAlpha(0.15)).toBe("rgba(255, 51, 0, 0.15)");
  });

  it("the stylesheet declares no marker token", () => {
    // Deliberate — see lib/marker.ts. The marker is not part of the palette, and
    // a second home in CSS is a second thing that can drift. There is nothing to
    // keep in agreement because there is only one of it.
    expect(stylesheet()).not.toMatch(/--color-marker/);
  });
});
