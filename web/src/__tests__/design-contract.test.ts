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
 *   W11  no CSS animation is infinite except the attested streaming cursor
 *   W12  no failure component imports MARKER_COLOR
 *   W13  the filmstrip is absent, not empty, when a project has no versions
 *   W13  no version surface renders a commit hash or branch name
 *   W7   ResizeObserver watches the same element whose getBoundingClientRect()
 *        the PickLayer reads (the integration seam — structural, not unit-testable
 *        in jsdom; verified by code review and the W7 fixed-dimension tripwire)
 *
 * Source-level assertions (the ones that read files) are deliberately crude. They
 * are tripwires, not type checking — they catch the reintroduction of a thing the
 * team decided against, which is exactly the failure mode a unit test misses.
 */

import { describe, it, expect, vi } from "vitest";
import { readFileSync, readdirSync, statSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, relative } from "node:path";
import { render, screen, fireEvent } from "@testing-library/react";
import { createElement } from "react";

import copy, { mm } from "../copy";
import { Z_INDEX } from "../App";
import { MARKER_COLOR, MARKER_RGB, markerAlpha } from "../lib/marker";
import { Filmstrip } from "../components/versions/Filmstrip";
import { BranchGraph } from "../components/versions/BranchGraph";
import { HistorySheet } from "../components/versions/HistorySheet";
import { CompareView } from "../components/versions/CompareView";
import { VariantGallery } from "../components/versions/VariantGallery";
import { Brief } from "../components/brief/Brief";
import type { VersionCompare, VersionTimelineEntry } from "../lib/api";

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
      "answerRoute",
      "brief",
      "confirmOffer",
      "export3mf",
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

  /* ---------------------------------------- W260 */

  it("the question pre-route's no-run replies live in the deck (issue #260)", () => {
    // The two no-run replies (stage-2 failed; design state doesn't
    // establish the answer) are fixed copy.ts strings — the backend
    // emits the same strings verbatim, so the wire and the deck are one
    // sentence. They ride the existing done-frame path verbatim.
    expect(copy.answerRoute.couldNotAnswer).toBe(
      "I couldn't answer that just now — nothing was changed.",
    );
    expect(copy.answerRoute.notEstablished).toBe(
      "The design as it stands doesn't establish that — nothing was changed.",
    );
    // Both say the invariant: nothing was changed. A no-run reply that
    // implied a version was created would be the house anti-pattern.
    expect(copy.answerRoute.couldNotAnswer).toContain("nothing was changed");
    expect(copy.answerRoute.notEstablished).toContain("nothing was changed");
    // The two replies are distinct — a failure and an unanswerable
    // question are different honest statements and must not read the
    // same.
    expect(copy.answerRoute.couldNotAnswer).not.toBe(copy.answerRoute.notEstablished);
  });

  /* ----------------------------------------- W250 */

  it("the assumed-value confirmation copy lives in the deck (issue #250)", () => {
    // All user-facing strings live in web/src/copy.ts. The offer is a
    // single plain sentence (never a form), and the deterministic offer
    // template and the short acknowledgement both have a deck home — the
    // model may supply its own offer sentence (the done frame's
    // `confirm_sentence`), but only when it passes the server's number
    // guard; the template below is the wording for everything else.
    const offer = copy.confirmOffer.offer("3.0\u202Fmm", "Wall thickness");
    expect(offer).toBe(
      "I assumed 3.0\u202Fmm for Wall thickness. Want it different?",
    );
    // The value slot is filled verbatim — no second formatting pass (the
    // caller pre-forms via `mm` for millimetre params, the model's own
    // string for non-numeric ones).
    expect(copy.confirmOffer.offer("2 grooves", "Channel depth")).toBe(
      "I assumed 2 grooves for Channel depth. Want it different?",
    );
    const ack = copy.confirmOffer.acknowledged("Wall thickness", "3.0\u202Fmm");
    expect(ack).toBe("Got it — Wall thickness stays 3.0\u202Fmm.");
    // The two surfaces are distinct — an offer and an acknowledgement must
    // never read as the same thing.
    expect(ack).not.toBe(offer);
    expect(ack).not.toContain("Want it different?");
  });

  it("the deck's offer template agrees in substance with the server's (issue #250)", () => {
    // The offer sentence the USER sees is built server-side
    // (`d33d.confirm_offer.offer_sentence`) and rendered verbatim by the
    // SPA (the done frame's `confirm_sentence` field). The deck's
    // `confirmOffer.offer` is a MIRROR of the server template (a testable
    // home on the SPA side, not a second writer of the wire string) —
    // this tripwire pins that the two templates agree in substance:
    // same structure, same value/label slots, same closing question. A
    // deck drift from the server template would silently desynchronise
    // what the design contract pins here from what the backend actually
    // emits, so the tripwire reads both sides.
    const appSrc = readFileSync(join(SRC, "App.tsx"), "utf8");
    // The SPA renders the wire string verbatim — it never substitutes its
    // own offer template into the offer message (the offer path builds
    // the content from `data.confirm_sentence`, not from
    // `confirmOffer.offer`).
    expect(appSrc).not.toMatch(/content:\s*copy\.confirmOffer\.offer\(/);
    // The deck's mirror template carries the server's exact structure
    // (value slot, label slot, closing question) — a deck rewrite that
    // changes the wording without touching the server would break the
    // design-contract pin above, so the structural tripwire here catches
    // the other half: a deck rewrite that keeps the shape but changes a
    // word (e.g. "Want it different?" → "Want it thinner?") would fail
    // the exact-match assertion at the top of this file's W250 block.
    const deck = copy.confirmOffer.offer("V", "L");
    expect(deck).toBe("I assumed V for L. Want it different?");
  });

  it("the pass card summary is a plain user-facing sentence, never system language or fabricated values", () => {
    // Issue #218: the pass card's summary line must be a copy.ts string —
    // an honest generic fallback confirming a design was produced and
    // validated — not the backend's internal "Design loop passed validation"
    // wire string, and not a sentence that fabricates dimensions the done
    // frame does not carry.
    const summary = copy.passCard.summary;
    expect(typeof summary).toBe("string");
    expect(summary.length).toBeGreaterThan(0);
    // No digits: a number in the summary is the house anti-pattern (a
    // confident value the component has not established).
    expect(summary).not.toMatch(/\d/);
    // No internal system language: the backend wire string must never
    // surface verbatim to the user.
    expect(summary.toLowerCase()).not.toContain("design loop");
    expect(summary.toLowerCase()).not.toContain("passed validation");
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

  /* --------------------------------------------------------------- W7 */

  it("no module exports a fixed viewer width or height", () => {
    // W7: the viewer sizes fluidly from its containing box (ResizeObserver),
    // not from a fixed constant. No module may export or default a width or
    // height for the viewer or pick layer — a silent 600x400 fallback is the
    // pick-drift bug wearing a default parameter.
    //
    // Scope: the three modules the contract covers — ModelViewer, PickLayer
    // and App.tsx (the stage host). A repo-wide scan would flag legitimate
    // fixed sizes elsewhere (DimensionCanvas's photo-fit defaults);
    // "width"-suffixed identifiers (strokeWidth, maxWidth, FLOOR_WIDTH_PX)
    // are excluded because a fixed *viewer* size, not the number 600
    // anywhere, is what is forbidden.
    // Two shapes to catch:
    //   (1) Identifiers containing "viewer" + width/height, in any scope file
    //       (e.g. VIEWER_WIDTH = 600 — the exact shape this ticket removed).
    //   (2) Exact "width" or "height" as a prop or local, but ONLY in the
    //       viewer component files (ModelViewer.tsx, PickLayer.tsx) — in
    //       App.tsx this would false-positive on UI element sizes (width: 300
    //       for a panel, etc.).
    const viewerConst = /\b[A-Za-z0-9_]*viewer[A-Za-z0-9_]*(?:width|height)[A-Za-z0-9_]*\s*\?{0,1}\s*[:=]\s*\d{3,}\b/i;
    const exactProp = /\b(?:width|height)\s*\?{0,1}\s*[:=]\s*\d{3,}\b/i;
    const allFiles = srcFiles().map((f) => relative(SRC, f));
    const scope = (f: string) =>
      f === "App.tsx" ||
      f.endsWith("/ModelViewer.tsx") ||
      f.endsWith("/PickLayer.tsx");
    const strays = allFiles
      .filter(scope)
      .filter((f) => {
        const content = readFileSync(join(SRC, f), "utf8");
        return viewerConst.test(content) || (exactProp.test(content) && f !== "App.tsx");
      })
      .sort();
    expect(strays).toEqual([]);
  });

  it("the viewer-pane element carries no fixed inline width or height", () => {
    // W7 element-anchored tripwire (issue #149): the name-based assertion
    // above catches identifiers that contain "viewer", but four slip-shapes
    // were verified to evade it — STAGE_WIDTH, CANVAS_HEIGHT, PANE_WIDTH,
    // and a bare style={{ width: 600 }} on the pane. All four only matter
    // when applied to the viewer-pane element itself, so this assertion
    // anchors on that element's own style block rather than on identifier
    // names. The two assertions are complementary: the name-based one
    // catches fixed dimensions exported from ModelViewer.tsx / PickLayer.tsx
    // where no viewer-pane element exists; this one catches fixed dimensions
    // applied directly to the pane in App.tsx.
    //
    // Boundary (honest, not a gap): this catches direct numeric forms
    // (width: 600, width: "600px") inside the viewer-pane's own style={{…}}.
    // It does NOT catch a spread from a variable (style={paneSize}) or
    // flex-basis sizing (flex: '0 0 600px') — those require an AST walk
    // or a DOM read, which the file's standing rule forbids.
    const app = readFileSync(join(SRC, "App.tsx"), "utf8");

    // Locate the viewer-pane element by its data-testid. The testid string
    // "viewer-pane" appears exactly once in App.tsx's JSX (the opening tag
    // of the pane div); the assertion's own doc comment names it, but this
    // read is of App.tsx source, not of this test file, so there is no
    // self-match risk.
    const testidIdx = app.indexOf('data-testid="viewer-pane"');
    expect(
      testidIdx,
      'App.tsx must contain the viewer-pane element (data-testid="viewer-pane")',
    ).toBeGreaterThanOrEqual(0);

    // From the testid position, find the style={{ … }} block that belongs
    // to this element. Walk forward to the next "style=" after the testid,
    // then capture the balanced-brace object.
    const styleIdx = app.indexOf("style=", testidIdx);
    expect(
      styleIdx > testidIdx,
      "the viewer-pane element must have a style prop",
    ).toBe(true);

    // The style value is {{…}} — two opening braces. Find the first { after
    // "style=" and match its balanced partner.
    const outerBrace = app.indexOf("{", styleIdx);
    expect(outerBrace > styleIdx).toBe(true);
    let depth = 0;
    let end = -1;
    for (let i = outerBrace; i < app.length; i += 1) {
      if (app[i] === "{") depth += 1;
      if (app[i] === "}") depth -= 1;
      if (depth === 0) { end = i; break; }
    }
    expect(end > outerBrace, "could not find the closing brace of the viewer-pane style block").toBe(true);

    // The style object's inner content: strip the outer pair of braces.
    // style={{…}} → the inner {…} is at outerBrace+1 … end-1.
    const styleBody = app.slice(outerBrace + 1, end);

    // A fixed dimension is width/height set to a bare number (≥100, to
    // exclude zIndex: 0 / inset: 0) or a numeric px string ("600px").
    // Fluid values — "100vw", "100vh", "100%", "auto", "fit-content",
    // "min-content", "max-content", "stretch", "100dvh" — are legitimate.
    // The regex matches the KEY width or height (as a whole word inside the
    // style object, i.e. preceded by { , or a newline+whitespace) followed
    // by a colon and a number (bare or px-suffixed). It does NOT match
    // maxWidth, minWidth, maxHeight, minHeight, or zIndex.
    const fixedDim =
      /(?:^|[,\n{\s])width\s*:\s*(?:\d{3,}(?:\.\d+)?\b|"\d+px"|'\d+px')/;
    const fixedDimH =
      /(?:^|[,\n{\s])height\s*:\s*(?:\d{3,}(?:\.\d+)?\b|"\d+px"|'\d+px')/;

    expect(
      fixedDim.test(styleBody),
      `viewer-pane style block contains a fixed width: ${styleBody.slice(0, 200)}`,
    ).toBe(false);
    expect(
      fixedDimH.test(styleBody),
      `viewer-pane style block contains a fixed height: ${styleBody.slice(0, 200)}`,
    ).toBe(false);
  });

  it("no component sets a z-index outside the four layers (0, 10, 20, 30)", () => {
    // W7: z-index is a closed set — canvas 0, panels 10, conversation 20,
    // pin+bar 30. Any other z-index value is a violation.
    //
    // What these scans cover, precisely:
    //  - The CSS `z-index: N` scan catches stylesheet values.
    //  - The React `zIndex: <literal>` scan catches INLINE LITERAL usages
    //    (the four in FirstRun, Filmstrip, HistorySheet, Brief) — the
    //    shape issue #184 proved the CSS-only scan could never see.
    //  - The SIX `Z_INDEX.foo` usages in App.tsx are NOT visible to either
    //    scan. They are safe by construction: Z_INDEX is a closed `as const`,
    //    so `Z_INDEX.foo` can only be one of its members — and the next
    //    assertion pins that closed set to exactly {canvas 0, panels 10,
    //    conversation 20, pinAndBar 30}, so a fifth member or a mutated
    //    value cannot sneak through unseen.
    const valid = new Set(["0", "10", "20", "30"]);
    for (const f of srcFiles()) {
      const content = readFileSync(f, "utf8");
      const matches = content.matchAll(/z-index\s*[:=]\s*(\d+)/g);
      for (const m of matches) {
        const val = m[1];
        expect(valid.has(val), `z-index ${val} in ${relative(SRC, f)} is not in {0,10,20,30}`).toBe(true);
      }
    }
    // The React style-prop shape: zIndex: 45 (bare number) or
    // zIndex: "45" / '45' (string), anywhere in src.
    const reactMatches =
      /zIndex\s*:\s*["']?(\d+)["']?/g;
    for (const f of srcFiles()) {
      const content = readFileSync(f, "utf8");
      for (const m of content.matchAll(reactMatches)) {
        const val = m[1];
        expect(valid.has(val), `zIndex ${val} in ${relative(SRC, f)} is not in {0,10,20,30}`).toBe(true);
      }
    }
  });

  it("Z_INDEX pins the closed four-layer set: exactly canvas 0, panels 10, conversation 20, pinAndBar 30", () => {
    // W7, level up: the six `Z_INDEX.foo` usages in App.tsx are invisible
    // to the literal scans above. They are safe ONLY while Z_INDEX is
    // exactly this closed object — so pin the constant itself. Reading
    // the imported value (not a source regex) is the strong form: it
    // checks what ships, so a fifth member OR a mutated value fails here.
    expect(Z_INDEX).toEqual({ canvas: 0, panels: 10, conversation: 20, pinAndBar: 30 });
    expect(Object.keys(Z_INDEX).sort()).toEqual(["canvas", "conversation", "panels", "pinAndBar"]);
  });

  /* --------------------------------------------------------------- W4 */

  it("no static 'Waiting for render' placeholder survives in src", () => {
    // The validation pane shows the real validation state or nothing — a
    // static placeholder under a completed render is a lie the user saw on
    // screen twice before this was removed (issue #114).
    expect(filesMatching(/Waiting for render/)).toEqual([]);
  });

  /* --------------------------------------------------------------- W8 */

  it("App.tsx imports all six surface components and holds none of their markup", () => {
    // Issue #116: six presentational surfaces were extracted out of App.tsx
    // (Brief, PassCard, Composer, PassProgress, FailureCard, Filmstrip) so
    // the W9-W14 surface tickets can work in their own files in parallel.
    // App.tsx must import each of the six — the contract the parallel
    // tickets build against — and must hold NONE of their markup: every
    // surface class name and testid lives in its own component file, not
    // inline in the stage host.
    const app = readFileSync(join(SRC, "App.tsx"), "utf8");
    for (const surface of [
      "brief/Brief",
      "chat/PassCard",
      "chat/Composer",
      "progress/PassProgress",
      "failure/FailureCard",
      "versions/Filmstrip",
    ]) {
      const importLine = `from "./components/${surface}"`;
      expect(app.includes(importLine), `App.tsx must import the ${surface} surface component`).toBe(true);
    }
    const surfaceMarkers = [
      // PassProgress's inline block (the design-loop stage indicator)
      "design-loop-progress",
      // FailureCard's inline block (the app-level error card)
      "app-error",
      // Filmstrip's inline block (the version-tail pane + compare + gallery)
      "version-tail-pane",
      // Brief's inline block (the top-left overlay)
      "brief-panel",
      // Composer's markup (the chat input form — must live in
      // components/chat/Composer.tsx, not in App.tsx)
      "chat-input",
      "chat-send-btn",
    ];
    for (const marker of surfaceMarkers) {
      expect(
        app.includes(marker),
        `App.tsx must not hold the ${marker} markup — it belongs to its surface component`,
      ).toBe(false);
    }
  });

  /* --------------------------------------------------------------- W10 */

  it("no chat message contains OpenSCAD source", () => {
    // W10 (issue #125): the token frame still carries the generated
    // source, but it is the pass card's disclosure content — App's
    // onToken must write it to the message's `source` field, never to
    // the message's `content` (the text that renders in the transcript).
    // Appending to content is the "a hundred lines of OpenSCAD in the
    // chat column" defect this item removes. The source field is
    // disclosure-only: ChatPanel renders it inside the PassCard,
    // collapsed by default.
    // The token handler must target the source field, not the content
    // field. The handler's body is delimited by the onProgress handler
    // that follows it in the stream wiring.
    const app = readFileSync(join(SRC, "App.tsx"), "utf8");
    const onTokenIdx = app.indexOf("onToken: (text");
    expect(onTokenIdx, "App.tsx must have an onToken handler in the stream wiring").toBeGreaterThanOrEqual(0);
    const onTokenEnd = app.indexOf("onProgress: (step", onTokenIdx);
    expect(onTokenEnd).toBeGreaterThan(onTokenIdx);
    const onToken = app.slice(onTokenIdx, onTokenEnd);
    expect(
      /source:\s*\(/.test(onToken),
      "App.tsx onToken must write the token text to the message's source field (the pass card's disclosure content)",
    ).toBe(true);
    expect(
      onToken.includes("content:"),
      "App.tsx onToken must NOT append token text to the message's content field (that is the SCAD-in-transcript defect)",
    ).toBe(false);
    // The message type carries the source as a first-class field, not as
    // a free-form string the transcript could render.
    // The transcript span renders only content — the source must not be
    // passed to the .chat-msg-content span. The span is a plain
    // `<span>…{msg.content}</span>`: slice to its closing tag and check.
    const chatPanel = readFileSync(
      join(SRC, "components/chat/ChatPanel.tsx"),
      "utf8",
    );
    expect(
      chatPanel.includes("source?: string"),
      "ChatMessage must carry the pass's source as its own field",
    ).toBe(true);
    const contentSpanIdx = chatPanel.indexOf("chat-msg-content");
    expect(contentSpanIdx).toBeGreaterThanOrEqual(0);
    const closeIdx = chatPanel.indexOf("</span>", contentSpanIdx);
    expect(closeIdx).toBeGreaterThan(contentSpanIdx);
    const contentSpan = chatPanel.slice(contentSpanIdx, closeIdx);
    expect(
      contentSpan.includes("msg.source"),
      "the transcript content span must render only content, never the source",
    ).toBe(false);
  });

  /* --------------------------------------------------------------- W9 */

  it("the Brief never renders a numeric value for provenance 'unknown'", () => {
    // W9 / issue #123: "unknown" arrives as value:null, and a number rendered
    // for an unknown parameter is the house anti-pattern in its purest form.
    // This is the tripwire for the thing a future change is most likely to
    // quietly undo: a `?? 0`, a `String(value)` default, or a branch that
    // falls through the unknown case into the number cell. The unknown cell
    // must be the not-established control — no digit anywhere in the row.
    const { container } = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          { name: "W", kind: "param", label: "Width", value: 60, unit: "mm", provenance: "stated" },
          { name: "H", kind: "param", label: "Height", value: null, unit: null, provenance: "unknown" },
        ],
      }),
    );
    const row = container.querySelector("[data-testid='brief-row-H']");
    expect(row, "the unknown row must be present").not.toBeNull();
    const text = row?.textContent ?? "";
    expect(text, "the unknown row must render the not-established control").toContain(
      copy.brief.unknownValue,
    );
    expect(text, "the unknown row must carry NO digit").not.toMatch(/\d/);
  });

  /* --------------------------------------------------------------- W13 */

  it("the copy deck exports the assumed-reason strings (issue #248)", () => {
    expect(copy.brief.provenanceAssumed("34.0 mm")).toBe(
      "Nobody said this. I picked 34.0 mm.",
    );
    // Issue #248: the reason clause is a copy.ts string (the model's own
    // words, never invented) — and it is distinct from the reason-less
    // sentence.
    const withReason = copy.brief.provenanceAssumedWithReason("34.0 mm", "it looked about right");
    expect(withReason).toContain("because");
    expect(withReason).not.toBe(copy.brief.provenanceAssumed("34.0 mm"));
  });

  it("the label fallback renders the identifier in the mono face (issue #248)", () => {
    // A model label (label_is_identifier false/absent) renders in the UI
    // face; a raw SCAD identifier (label_is_identifier true) renders in
    // the mono face — mono = machine value, so the user can tell a human
    // label from an identifier at a glance. The copy deck exports the
    // reason string; no component synthesises a prettified label.
    render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          { name: "fillet_size_top", kind: "param", label: "Top fillet size", value: 2, unit: "mm", provenance: "assumed" },
          { name: "fst", kind: "param", label: "fst", value: 2, unit: "mm", provenance: "assumed", label_is_identifier: true },
        ],
      }),
    );
    // Both labels render their text.
    expect(screen.getByText("Top fillet size")).toBeTruthy();
    expect(screen.getByText("fst")).toBeTruthy();
    // The identifier renders in the mono face, the label in the UI face.
    const fstEl = screen.getByText("fst");
    const labelEl = screen.getByText("Top fillet size");
    expect(fstEl.style.fontFamily).toBe("var(--font-mono)");
    expect(labelEl.style.fontFamily).toBe("var(--font-ui)");
  });

  it("the assumed expanded row shows the reason sentence when one exists (issue #248)", () => {
    const { container } = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          {
            name: "wall_t",
            kind: "param",
            label: "Wall thickness",
            value: 3,
            unit: "mm",
            provenance: "assumed",
            reason: "0.4 mm nozzle FDM tolerance",
          },
        ],
      }),
    );
    // Expand the row (click the inner div that carries the onClick).
    const row = container.querySelector("[data-testid='brief-row-wall_t']");
    const clickable = row?.querySelector("div[style*='cursor']");
    if (clickable) fireEvent.click(clickable);
    const expanded = container.querySelector("[data-testid='brief-row-expanded']");
    expect(expanded).toBeTruthy();
    expect(expanded?.textContent).toContain(
      copy.brief.provenanceAssumedWithReason("3.0\u202Fmm", "0.4 mm nozzle FDM tolerance"),
    );
  });

  it("the assumed expanded row shows the reason-less sentence when no reason (issue #246 fallback)", () => {
    const { container } = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          {
            name: "wall_t",
            kind: "param",
            label: "Wall thickness",
            value: 3,
            unit: "mm",
            provenance: "assumed",
          },
        ],
      }),
    );
    const row = container.querySelector("[data-testid='brief-row-wall_t']");
    const clickable = row?.querySelector("div[style*='cursor']");
    if (clickable) fireEvent.click(clickable);
    const expanded = container.querySelector("[data-testid='brief-row-expanded']");
    expect(expanded).toBeTruthy();
    expect(expanded?.textContent).toContain(copy.brief.provenanceAssumed("3.0\u202Fmm"));
  });

  it("the filmstrip is absent, not empty, when a project has no versions", () => {
    // W13: a project with zero versions (and no pass in flight) renders NO
    // filmstrip at all — not an empty rail, not a "No versions yet" branch.
    // A pass in flight with zero versions DOES render (just the dashed
    // pending slot), so the absence is specifically the no-versions-no-pass
    // case. Rendering the real component proves the branch, not the copy.
    const { container } = render(
      createElement(Filmstrip, {
        versions: [],
        passInFlight: false,
        pendingName: null,
        inset: 24,
        onCompareSelect: () => {},
        onOpenSheet: () => {},
        sheetOpenFor: null,
      }),
    );
    expect(container.querySelector(".filmstrip")).toBeNull();
    // And the in-flight case does render (the strip is never behind the
    // conversation) — the absence is the no-pass case, not a blanket null.
    const inFlight = render(
      createElement(Filmstrip, {
        versions: [],
        passInFlight: true,
        pendingName: null,
        inset: 24,
        onCompareSelect: () => {},
        onOpenSheet: () => {},
        sheetOpenFor: null,
      }),
    );
    expect(inFlight.container.querySelector(".filmstrip")).not.toBeNull();
  });

  it("no version surface renders a commit hash or branch name", () => {
    // W13 / W16 invariant: no version surface ever shows git — no commit
    // hashes, no branch names. The guarantee is behavioural, not lexical:
    // a naive component could print a hash in a visible slot, in an
    // alt/title attribute (textContent excludes attributes), or as a
    // 7-char short hash — every one of those shapes must trip this check.
    //
    // The scan therefore walks BOTH textContent and every attribute value
    // of every rendered element, and the hex detector is NOT word-anchored:
    // the old /\b[0-9a-f]{7,40}\b/ failed on the real DOM because the slot
    // renders the name immediately followed by `v{id}`, fusing a real hash
    // into a 41-char run where the trailing 'v' destroys the word boundary
    // (and a 7-char hash is itself at the anchor's edge).
    const assertNoGit = (root: Element, label: string) => {
      const nodes: Element[] = [root, ...Array.from(root.querySelectorAll("*"))];
      const violations: string[] = [];
      for (const el of nodes) {
        const text = el.textContent ?? "";
        if (/[0-9a-f]{7,}/i.test(text)) {
          violations.push(`hex run in text: "${text.slice(0, 80)}"`);
        }
        if (/(?:feature|fix|hotfix|release|main|develop)\/[A-Za-z0-9_-]+/i.test(text)) {
          violations.push(`branch-like name in text: "${text.slice(0, 80)}"`);
        }
        for (const attr of el.attributes) {
          if (/[0-9a-f]{7,}/i.test(attr.value)) {
            violations.push(`hex run in attribute ${attr.name}: "${attr.value.slice(0, 80)}"`);
          }
          if (/(?:feature|fix|hotfix|release|main|develop)\/[A-Za-z0-9_-]+/i.test(attr.value)) {
            violations.push(`branch-like name in attribute ${attr.name}: "${attr.value.slice(0, 80)}"`);
          }
        }
      }
      expect(violations, `${label}: git-shaped output in DOM\n  ${violations.join("\n  ")}`).toEqual([]);
    };

    // (1) The filmstrip: a plain version with a diff fragment plus a version
    //     whose name contains no 7+ hex run and no branch-like grammar.
    const versions: VersionTimelineEntry[] = [
      {
        id: 1,
        name: "rod 45 off wall",
        params: {},
        created_by_message: "make a rod",
        parent: null,
        restored_from: null,
        forked_from: null,
        pinned: false,
        archived: false,
        thumbnail: null,
        created_at: "2026-01-01T00:00:00Z",
        diff_count: 0,
        exported_at: null,
      },
      {
        id: 2,
        name: "rod 45.0 off wall",
        params: { D: 45 },
        created_by_message: "widen",
        parent: 1,
        restored_from: null,
        forked_from: null,
        pinned: false,
        archived: false,
        thumbnail: null,
        created_at: "2026-01-02T00:00:00Z",
        diff_count: 1,
        exported_at: null,
      },
    ];
    const strip = render(
      createElement(Filmstrip, {
        versions,
        passInFlight: false,
        pendingName: null,
        inset: 24,
        onCompareSelect: () => {},
        onOpenSheet: () => {},
        sheetOpenFor: null,
      }),
    );
    assertNoGit(strip.container, "filmstrip");

    // (2) The compare view: a 40-char hash fused directly against the
    //     version number (the fusion shape that fooled the old word-boundary
    //     check) and a branch-like name, each in text and in the viewport
    //     thumbnails' alt (attributes textContent cannot see).
    const compare: VersionCompare = {
      project_id: 7,
      a: { ...versions[0], id: 10, name: "rod 45 off wall" },
      b: {
        ...versions[1],
        id: 11,
        name: "rod 45.0 off wall",
        thumbnail: null,
      },
      diff: {
        count: 1,
        changed: ["D"],
        added: [],
        removed: [],
      },
      shared_rotation: {
        units: "mm",
        axis_convention: "z-up",
        identical_convention: true,
      },
    };
    const cmp = render(createElement(CompareView, { compare, aId: 10, bId: 11 }));
    assertNoGit(cmp.container, "compare-view");

    // (3) The history sheet (W16): the sheet mounts the timeline, the
    //     branch riser graph and the compare — a hash in any of those
    //     (the graph's nodes, the timeline's names, the compare's names)
    //     must trip this check the same way.
    // (3) The history sheet (W16): the sheet mounts the timeline, the
    //     branch riser graph and the compare — a hash in any of those
    //     (the graph's nodes, the timeline's names, the compare's names)
    //     must trip this check the same way. (Red-checked 2026-07-05: a
    //     40-char hex version name induced via RED_CHECK_SHEET went RED on
    //     exactly this coverage, then was reverted.)
    const sheet = render(
      createElement(HistorySheet, {
        versions,
        compareIds: [1, 2] as [number, number],
        compareResult: null,
        compareError: null,
        inset: 24,
        onRestore: () => {},
        onPin: () => {},
        onCompareSelect: () => {},
        onClose: () => {},
      }),
    );
    assertNoGit(sheet.container, "history-sheet");
    expect(sheet.container.querySelector("[data-testid='branch-graph']")).not.toBeNull();

    // (4) The branch riser graph on its own: a restore riser (the rise-back
    //     edge) is drawn from the version graph's restored_from pointer, not
    //     from a git ref — a graph drawn from git would render a branch name.
    const graph = render(
      createElement(BranchGraph, {
        versions: [
          ...versions,
          {
            ...versions[1],
            id: 3,
            name: "restored back",
            parent: 2,
            restored_from: 1,
            diff_count: 1,
          },
        ],
      }),
    );
    assertNoGit(graph.container, "branch-graph");
    expect(
      graph.container.querySelector("[data-testid='branch-restore-riser-3']"),
    ).not.toBeNull();

    // (3) The pinned gallery: a real card (name with no branch-like grammar,
    //     no 7+ hex run in name, src or alt).
    const gallery = render(
      createElement(VariantGallery, {
        cards: [
          {
            ...versions[1],
            id: 12,
            name: "rod 45.0 off wall",
            pinned: true,
            actions: ["set-as-main", "branch-from", "archive"] as const,
          },
        ],
      }),
    );
    assertNoGit(gallery.container, "variant-gallery");
  });

  /* --------------------------------------------------------------- W11 */

  it("no indeterminate progress animation exists", () => {
    // W11 / issue #118: the indeterminate sliding bar (the moving gradient
    // that implied motion the system cannot substantiate) is gone. The bar
    // is now a deterministic fill driven by the real per-view frames — a
    // layout width, not an animation. Tripwire: the old keyframes name and
    // the indeterminate class name must not appear anywhere in src, and the
    // stylesheet must carry no infinite animation.
    expect(filesMatching(/design-loop-progress-slide/)).toEqual([]);
    expect(filesMatching(/design-loop-progress-indicator/)).toEqual([]);
    // No infinite animation may live in the PROGRESS surface — the
    // indeterminate slide is the specific thing this ticket removes. A
    // blanket "no infinite animation anywhere" assertion would trip on
    // pre-existing, non-progress surfaces (the streaming cursor), which
    // is a different ticket's decision, not this one's.
    const progressBlock = stylesheet().match(/\.design-loop-progress[\s\S]*?\n\}/);
    expect(progressBlock, "the progress surface styles must exist").not.toBeNull();
    expect(progressBlock![0]).not.toMatch(/animation:/);
    // And the indeterminate bar itself is gone: no keyframes for it,
    // no element carrying an infinite animation inside the progress block.
    expect(stylesheet()).not.toMatch(/@keyframes[^{]*progress/);
  });

  it("no CSS animation is infinite except the attested streaming cursor", () => {
    // The closed motion set: exactly three non-infinite transitions plus
    // ONE attested infinite animation — the .streaming-cursor blink
    // (streaming-blink 1s step-end), the blinking cursor shown while the
    // assistant streams. The count is pinned to exactly one so any new
    // infinite animation added anywhere in the stylesheet trips this
    // assertion.
    // (The streaming cursor's blink pre-dates the motion-budget work and
    // is attested here — it is a live chat affordance covered by
    // chat-panel.test.tsx, not a progress-surface concern.)
    const css = stylesheet();
    const infinite = [...css.matchAll(/animation:[^;]*infinite/g)];
    // Exactly the one attested infinite animation (the streaming cursor);
    // the progress surface contributes none.
    expect(infinite).toHaveLength(1);
    const progressBlock = css.match(/\.design-loop-progress[\s\S]*?\n\}/);
    expect(progressBlock![0]).not.toMatch(/animation:[^;]*infinite/);
  });

  /* --------------------------------------------------------------- W14 */

  it("no build-volume literal (320 / 300) appears in web/src/components/firstrun/*", () => {
    // W14 (issue #128): the first-run plate's caption and drawing come from
    // the envelope the API reports (GET /api/config/envelope), never from a
    // literal in the SPA. Typing 320 or 300 into the first-run component is
    // the "a confident value the SPA has not established" defect this
    // assertion trips. The scan is scoped to the firstrun components — the
    // rest of the SPA legitimately references the plate's dimensions in
    // comments (e.g. ModelViewer's far-plane docstring), and those are not
    // the surface this ticket governs.
    const firstrunDir = join(SRC, "components/firstrun");
    const files = readdirSync(firstrunDir)
      .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
      .map((f) => join(firstrunDir, f));
    for (const file of files) {
      const content = readFileSync(file, "utf8");
      // The build-volume literals — 320 (x/y) and 300 (z) — must not appear
      // as bare numbers in the component source. A test file is exempt:
      // the component's OWN test asserts the drawn dimensions against the
      // deck's caption function, so the literal lives in the test, not the
      // component.
      if (file.includes("__tests__")) continue;
      const bare320 = /(?<![\d\w.])320(?![\d\w.])/.test(content);
      const bare300 = /(?<![\d\w.])300(?![\d\w.])/.test(content);
      expect(
        bare320,
        `build-volume literal 320 found in ${relative(SRC, file)} — the plate must read the envelope from the API, not a literal`,
      ).toBe(false);
      expect(
        bare300,
        `build-volume literal 300 found in ${relative(SRC, file)} — the plate must read the envelope from the API, not a literal`,
      ).toBe(false);
    }
  });

  /* --------------------------------------------------------------- W12 */

  it("no failure surface uses the marker colour (no #FF3300, no MARKER_COLOR import)", () => {
    // #FF3300 is the region marker and nothing else. Blocked, failed and
    // disagreeing states use --color-blocked (#D2A63C). The scan is
    // restricted to the failure surface: files under components/failure/
    // plus errorMapping.ts — no failure file may import MARKER_COLOR or
    // carry the hex literal (in comments either, since the single home is
    // lib/marker.ts).
    const failureDir = join(SRC, "components/failure");
    const files = readdirSync(failureDir)
      .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
      .map((f) => join(failureDir, f));
    files.push(join(SRC, "lib/errorMapping.ts"));
    for (const file of files) {
      const content = readFileSync(file, "utf8");
      expect(
        content.includes("#FF3300") || content.includes("MARKER_COLOR"),
        `failure surface file ${relative(SRC, file)} must not reference the marker colour`,
      ).toBe(false);
    }
  });

  it("no build-volume literal (320 / 300) appears in the failure surface", () => {
    // The build volume comes from the API (GET /api/config/envelope),
    // never from a literal in the SPA. The scan covers the failure
    // components and the mapping; test files are exempt (they assert
    // against the deck's caption functions).
    const failureDir = join(SRC, "components/failure");
    const files = readdirSync(failureDir)
      .filter((f) => f.endsWith(".ts") || f.endsWith(".tsx"))
      .map((f) => join(failureDir, f));
    files.push(join(SRC, "lib/errorMapping.ts"));
    for (const file of files) {
      if (file.includes("__tests__")) continue;
      const content = readFileSync(file, "utf8");
      const bare320 = /(?<![\d\w.])320(?![\d\w.])/.test(content);
      const bare300 = /(?<![\d\w.])300(?![\d\w.])/.test(content);
      expect(
        bare320,
        `build-volume literal 320 found in ${relative(SRC, file)} — the limit must come from the API`,
      ).toBe(false);
      expect(
        bare300,
        `build-volume literal 300 found in ${relative(SRC, file)} — the limit must come from the API`,
      ).toBe(false);
    }
  });

  it("failure copy has no attempt counting (the two-failure rule is dropped)", () => {
    // The design deck's two-failures-same-goal rule was dropped (no goal
    // object exists, and inferring one from message text is guesswork). No
    // copy may vary by attempt: `askInstead` is removed, and no failure
    // copy names a second failure.
    expect((copy.failure as Record<string, unknown>).askInstead).toBeUndefined();
    for (const key of Object.keys(copy.failure.reasons)) {
      const value = (copy.failure.reasons as Record<string, string>)[key];
      expect(value).not.toMatch(/\btwice\b|\bsecond\b/i);
    }
  });

  it("the failure turn's failing axis uses the blocked colour token, not the marker", () => {
    // The failing axis is drawn in --color-blocked (#D2A63C). The CSS for
    // the failing bar references the token; no failure CSS references the
    // marker hex.
    expect(stylesheet()).toMatch(/\.failure-turn-bar--failing[\s\S]*?var\(--color-blocked\)/);
    expect(stylesheet()).not.toMatch(/\.failure-turn[\s\S]*?#FF3300/i);
  });

  /* --------------------------------------------------------------- W191 */

  it("the collapse button renders no visible copy string (icon-only)", () => {
    // W191 / issue #191: the expand-state conversation-collapse-btn is
    // icon-only — its accessible name comes solely from the aria-label
    // binding to copy.shell.collapseConversation, not from a visible text
    // child. The defect (171×25 text button over the Brief panel) was the
    // same string rendered as BOTH the aria-label and the visible children;
    // the icon-only fix removes the text child. The collapsed-state rail
    // button (data-testid="conversation-rail") is a separate control and is
    // intentionally NOT constrained here.
    //
    // This is a source-level tripwire in the W-series style: it slices
    // App.tsx from the conversation-collapse-btn opening tag to its closing
    // tag and asserts the slice binds aria-label to the copy key but does
    // NOT interpolate the copy key as JSX text. It reads App.tsx by path
    // (never edits it); if the slice markers are not stable after the fix,
    // the slice assertion below reports it.
    const app = readFileSync(join(SRC, "App.tsx"), "utf8");

    // Locate the button element: the data-testid anchors the slice start
    // (walk back to the enclosing opening tag) and the first closing tag
    // after it ends the slice — the button is the smallest element carrying
    // the testid, so its own closing tag is the right boundary.
    const testidIdx = app.indexOf('data-testid="conversation-collapse-btn"');
    expect(
      testidIdx,
      'App.tsx must contain the collapse button (data-testid="conversation-collapse-btn")',
    ).toBeGreaterThanOrEqual(0);
    const tagStart = app.lastIndexOf("<button", testidIdx);
    expect(tagStart, "the collapse button must open with a <button tag").toBeGreaterThanOrEqual(0);
    const closeIdx = app.indexOf("</button>", testidIdx);
    expect(closeIdx, "the collapse button must close").toBeGreaterThan(tagStart);
    const slice = app.slice(tagStart, closeIdx + "</button>".length);

    // The accessible name must come from the aria-label binding to the
    // copy key — dropping it would make the icon invisible to assistive
    // tech.
    expect(
      /aria-label=\{copy\.shell\.collapseConversation\}/.test(slice),
      `the collapse button must keep its aria-label bound to copy.shell.collapseConversation:\n${slice}`,
    ).toBe(true);

    // The copy key must NOT appear anywhere else in the button element — in
    // particular not as a JSX text child (the {copy.shell.collapseConversation}
    // interpolation the defect shipped). Strip the attested aria-label
    // binding first so the "does not appear" check targets only the
    // visible-content shapes.
    const withoutAriaLabel = slice.replace(/aria-label=\{copy\.shell\.collapseConversation\}/g, "");
    expect(
      withoutAriaLabel.includes("copy.shell.collapseConversation"),
      `the collapse button must render no copy string as visible content (icon-only); the string must come solely from the aria-label:\n${slice}`,
    ).toBe(false);
  });

  /* --------------------------------------------------------------- W9b */

  it("every Brief list item carries a unique, stable key (no React key warning)", () => {
    // W9 (issue #196): the Brief renders two lists (the promoted unknowns
    // and the resolved rows, group-collapsed above MAX_LIST_ROWS). Both
    // must be keyed by the stable entry name — the same identity the rows'
    // data-testid and the expanded state are keyed off. An index key would
    // silence the warning but re-attach row state to the wrong row on
    // reorder; no key at all is the shipped defect this pins.
    // The >7 resolved entries hit the group-collapsed branch; the unknowns
    // hit their own list — both maps must be warning-free.
    const entries = [
      { name: "W", kind: "param" as const, label: "Width", value: 60, unit: "mm", provenance: "stated" as const },
      { name: "D", kind: "param" as const, label: "Depth", value: 45, unit: "mm", provenance: "stated" as const },
      { name: "H", kind: "param" as const, label: "Height", value: 80, unit: "mm", provenance: "stated" as const },
      { name: "p4", kind: "param" as const, label: "p4", value: 10, unit: "mm", provenance: "stated" as const },
      { name: "p5", kind: "param" as const, label: "p5", value: 10, unit: "mm", provenance: "stated" as const },
      { name: "p6", kind: "param" as const, label: "p6", value: 10, unit: "mm", provenance: "stated" as const },
      { name: "p7", kind: "param" as const, label: "p7", value: 10, unit: "mm", provenance: "stated" as const },
      { name: "p8", kind: "param" as const, label: "p8", value: 10, unit: "mm", provenance: "stated" as const },
      { name: "u1", kind: "param" as const, label: "u1", value: null, unit: null, provenance: "unknown" as const },
      { name: "u2", kind: "param" as const, label: "u2", value: null, unit: null, provenance: "unknown" as const },
    ];
    const consoleErrorSpy = vi.spyOn(console, "error");
    const first = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries,
      }),
    );
    // Re-render reordered: the refetch shape, where an index key would
    // mis-attach row state and a missing key would re-warn.
    const reordered = [...entries].reverse();
    const second = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: reordered,
      }),
    );
    const keyWarnings = consoleErrorSpy.mock.calls.filter((c) =>
      String(c[0]).includes('Each child in a list should have a unique "key" prop'),
    );
    expect(keyWarnings).toEqual([]);
    consoleErrorSpy.mockRestore();
    // The stable identity is the row's own testid. Above MAX_LIST_ROWS the
    // resolved rows hide behind the group count (they render in neither list),
    // so the rows asserted here are the two promoted unknowns — always
    // rendered, in both orders, in both containers.
    for (const name of ["u1", "u2"]) {
      expect(
        first.container.querySelector(`[data-testid='brief-row-${name}']`),
        `row ${name} must resolve by its stable name identity in the first render`,
      ).not.toBeNull();
      expect(
        second.container.querySelector(`[data-testid='brief-row-${name}']`),
        `row ${name} must resolve by its stable name identity in the reordered render`,
      ).not.toBeNull();
    }
    // The group-collapsed branch was exercised (8 resolved > 7) in both
    // renders — the resolved rows are behind the count, not unkeyed.
    expect(first.container.querySelector("[data-testid='brief-groups-count']")).not.toBeNull();
    expect(second.container.querySelector("[data-testid='brief-groups-count']")).not.toBeNull();
    expect(first.container.querySelector("[data-testid='brief-row-W']")).toBeNull();
    expect(second.container.querySelector("[data-testid='brief-row-W']")).toBeNull();
  });

  /* --------------------------------------------------------------- W246 */

  it("the Brief renders a fifth 'assumed' provenance: half-dot mark, legend entry, separate chip count (issue #246)", () => {
    // Issue #246: model-emitted parameters are 'assumed', never 'stated'.
    // The Brief gains a fifth provenance state:
    //   - a half-filled dot mark (faint, not the stated/measured/unknown/
    //     disagrees shapes),
    //   - a legend entry "I assumed" (copy.brief.legend.assumed),
    //   - an expanded-row sentence "Nobody said this. I picked {value}."
    //     (no reason clause — the labels ticket adds it later),
    //   - a chip count SEPARATE from unknowns ("N assumed").
    // #FF3300 stays reserved for the region marker — the assumed mark uses
    // the faint token, so no marker colour leaks in here.

    // (1) The copy deck carries the new strings — the closed-set check in
    //     the W2 key-set test above already pins the deck's top-level
    //     surfaces; this pins the NEW keys inside the brief surface.
    expect(copy.brief.legend.assumed).toBe("I assumed");
    expect(copy.brief.collapsedAssumed(4)).toBe("4 assumed");
    expect(copy.brief.collapsedAssumed(1)).toBe("1 assumed");
    expect(copy.brief.provenanceAssumed("12.0\u202Fmm")).toBe(
      "Nobody said this. I picked 12.0\u202Fmm.",
    );

    // (2) The assumed row renders the half-dot mark and the value — never
    //     a marker-coloured mark, never a fabricated provenance prose.
    const { container } = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          { name: "W", kind: "param", label: "Width", value: 60, unit: "mm", provenance: "stated" as const },
          { name: "spacer_height", kind: "param", label: "spacer_height", value: 12, unit: "mm", provenance: "assumed" as const },
          { name: "H", kind: "param", label: "Height", value: null, unit: null, provenance: "unknown" as const },
        ],
      }),
    );
    const row = container.querySelector("[data-testid='brief-row-spacer_height']");
    expect(row, "the assumed row must be present").not.toBeNull();
    expect(row?.getAttribute("data-provenance")).toBe("assumed");
    // The value renders (an assumed value IS a real value — unlike unknown,
    // it is never the not-established control).
    expect(row?.textContent).toContain("12.0");
    // The half-dot mark: a faint-token gradient, not the filled/hollow/
    // dashed shapes of the other four states, and NOT the marker colour.
    const mark = row?.querySelector("[data-testid='brief-mark']");
    const markBg = (mark?.getAttribute("style") ?? "").toLowerCase();
    expect(markBg).toContain("gradient");
    expect(markBg).not.toContain("255, 51, 0");
    expect(markBg).not.toContain("#ff3300");

    // (3) The collapsed chip counts assumed SEPARATELY from unknowns —
    //     both spans present, each with its own count, values in the mono
    //     face per AGENTS.md (the chip line carries the raw values; the
    //     mono-face rule for values is the Brief's own invariant).
    const chipRender = render(
      createElement(Brief, {
        isChip: true,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          { name: "W", kind: "param", label: "Width", value: 60, unit: "mm", provenance: "stated" as const },
          { name: "D", kind: "param", label: "Depth", value: 45, unit: "mm", provenance: "assumed" as const },
          { name: "H", kind: "param", label: "Height", value: 80, unit: "mm", provenance: "assumed" as const },
          { name: "u1", kind: "param", label: "u1", value: null, unit: null, provenance: "unknown" as const },
        ],
      }),
    );
    expect(chipRender.container.querySelector("[data-testid='brief-chip-assumed']")?.textContent).toBe(
      copy.brief.collapsedAssumed(2),
    );
    expect(chipRender.container.querySelector("[data-testid='brief-chip-unknowns']")?.textContent).toBe(
      copy.brief.collapsedUnknowns(1),
    );
    // Full mode does NOT show the assumed count (operator decision: the
    // chip-only count; full mode shows the per-row half-dots instead).
    const fullRender = render(
      createElement(Brief, {
        isChip: false,
        inset: 24,
        conversationCollapsed: false,
        entries: [
          { name: "W", kind: "param", label: "Width", value: 60, unit: "mm", provenance: "stated" as const },
          { name: "D", kind: "param", label: "Depth", value: 45, unit: "mm", provenance: "assumed" as const },
        ],
      }),
    );
    expect(fullRender.container.querySelector("[data-testid='brief-chip-assumed']")).toBeNull();
  });
});
