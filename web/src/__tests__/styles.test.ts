/**
 * Stylesheet tests (issue #81, retargeted by issue #112): assert the
 * single stylesheet exists, defines the token block, activates the
 * class names the components already use, and is imported from
 * main.tsx.
 *
 * Issue #112 replaced the Primer colour ramp with the dark-instrument
 * token set and two webfonts (Space Grotesk / JetBrains Mono), and
 * added the font <link> to web/index.html. The :root token list, the
 * body font stack, and the chat-role tint assertions below now read
 * the new token names; the chat-role test additionally pins the
 * user-tint to the overlay recipe's 92% alpha panel tint
 * (rgba(19,22,25,0.92) — the --color-panel value #13161A at 92% alpha).
 *
 * jsdom applies no CSS, so the rules are verified by reading the file
 * from disk (the same file Vite bundles and Playwright's e2e specs
 * exercise via computed style).
 */

import { readFileSync } from "node:fs";
import path from "node:path";
import { describe, it, expect } from "vitest";

const STYLES_PATH = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../styles.css");
const MAIN_PATH = path.resolve(path.dirname(new URL(import.meta.url).pathname), "../main.tsx");

function readStylesheet(): string {
  return readFileSync(STYLES_PATH, "utf-8");
}

/** Extract the body of a rule (selector, { ... }) by scanning braces. */
function ruleFor(css: string, selector: string): string | null {
  const needle = `${selector} {`;
  const idx = css.indexOf(needle);
  if (idx === -1) return null;
  const open = idx + needle.length - 1;
  let depth = 0;
  for (let i = open; i < css.length; i++) {
    if (css[i] === "{") depth++;
    if (css[i] === "}") {
      depth--;
      if (depth === 0) return css.slice(open + 1, i);
    }
  }
  return null;
}

describe("web/src/styles.css (issue #81)", () => {
  it("is imported once from main.tsx", () => {
    const main = readFileSync(MAIN_PATH, "utf-8");
    expect(main).toMatch(/import\s+["']\.\/styles\.css["']/);
  });

  it("defines the :root token block (spacing, colours, type scale)", () => {
    const css = readStylesheet();
    const root = ruleFor(css, ":root");
    expect(root).not.toBeNull();
    for (const token of [
      "--space-1",
      "--space-2",
      "--space-3",
      "--space-4",
      "--space-6",
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
      "--font-size-xs",
      "--font-size-sm",
      "--font-size-base",
      "--font-size-lg",
      "--line-height",
      "--radius",
      "--radius-sm",
      "--font-ui",
      "--font-mono",
    ]) {
      expect(root).toContain(`${token}:`);
    }
    // W1/W17: the region marker lives only in web/src/lib/marker.ts — the
    // stylesheet declares no marker token (checked at the custom-property
    // position, so the "no marker token" doc comment is not a false positive).
    expect(root).not.toContain("--color-marker:");
  });

  it("sets the UI face, 14px size, and line height on body", () => {
    const body = ruleFor(readStylesheet(), "body");
    expect(body).toContain("font-family: var(--font-ui)");
    expect(body).toContain("font-size: var(--font-size-sm)");
    expect(body).toContain("line-height: var(--line-height)");
  });

  it("keeps html/body/#root at zero margin and full height (moved from index.html)", () => {
    const css = readStylesheet();
    const idx = css.indexOf("#root {");
    expect(idx).not.toBe(-1);
    const open = css.indexOf("{", idx); // the brace right after `#root`
    let depth = 0;
    let close = -1;
    for (let i = open; i < css.length; i++) {
      if (css[i] === "{") depth++;
      if (css[i] === "}") {
        depth--;
        if (depth === 0) {
          close = i;
          break;
        }
      }
    }
    // The selector list starts after the previous rule's closing brace.
    const prevClose = css.lastIndexOf("}", open);
    const selector = css.slice(prevClose + 1, open);
    expect(selector).toMatch(/html,\s*body,\s*#root\s*$/);
    const body = css.slice(open + 1, close);
    expect(body).toContain("margin: 0");
    expect(body).toContain("height: 100%");
  });

  it("activates the class names the components already use", () => {
    const css = readStylesheet();
    for (const selector of [
      ".chat-panel",
      ".chat-msg",
      ".chat-msg--user",
      ".chat-msg--assistant",
      ".chat-msg--system",
      ".chat-renders",
      ".chat-render-img",
      ".chat-input-form",
      ".app-right",
      ".viewer-pane",
      ".version-tail-pane",
      ".validation-pane",
    ]) {
      expect(css).toContain(selector);
    }
  });

  it("constrains render thumbnails (max-width:100%, height:auto) and grids .chat-renders", () => {
    const css = readStylesheet();
    const img = ruleFor(css, ".chat-render-img");
    expect(img).toContain("max-width: 100%");
    expect(img).toContain("height: auto");
    const renders = ruleFor(css, ".chat-renders");
    expect(renders).toContain("display: grid");
  });

  it("distinguishes chat roles by background tint, without touching .viewer-pane geometry", () => {
    const css = readStylesheet();
    const user = ruleFor(css, ".chat-msg--user");
    // #112: the user tint is the panel colour at the overlay recipe's 92% alpha,
    // expressed via color-mix so it cannot drift from --color-panel.
    expect(user).toContain("background-color: color-mix(in srgb, var(--color-panel) 92%, transparent)");
    expect(ruleFor(css, ".chat-msg--assistant")).not.toBeNull();
    // The lasso overlay's containing block depends on .viewer-pane's
    // inline geometry (issues #74/#76) — colour/border only, never
    // position/width/height/overflow.
    const pane = ruleFor(css, ".viewer-pane");
    expect(pane).not.toMatch(/\b(position|width|height|overflow)\s*:/);
  });

  it("wraps prose content (pre-wrap removed for the UI face — issue #125) and styles buttons with :hover", () => {
    const css = readStylesheet();
    const content = ruleFor(css, ".chat-msg-content");
    // W10: the SCAD source no longer lands in the transcript, so the
    // monospace treatment is gone — the message body is prose in the UI
    // face (the pass card's disclosure keeps the mono).
    expect(content).toContain("font-family: var(--font-ui)");
    expect(content).not.toContain("font-mono");
    expect(content).not.toContain("white-space: pre-wrap");
    // A :hover rule exists (impossible with inline styles).
    expect(css).toMatch(/:hover/);
    // The disabled state gets a consistent treatment.
    expect(css).toContain(":disabled");
  });

  it("adds no framework or dependency: plain custom properties and selectors only", () => {
    const css = readStylesheet();
    expect(css).not.toMatch(/@import/);
    expect(css).not.toMatch(/@use|@mixin|@media/);
  });

  it("loads the two webfonts from web/index.html (400 and 500 only)", () => {
    // #112: --font-ui / --font-mono name Space Grotesk and JetBrains Mono,
    // neither of which is available system-wide — index.html loads them via
    // a Google Fonts <link> (a runtime network dependency the app did not
    // have before; the browser falls back to the system stacks in the
    // tokens when the fetch fails). One direct assertion on the exact href
    // pins both families, both weights, and display=swap in one shot.
    const html = readFileSync(
      path.resolve(path.dirname(new URL(import.meta.url).pathname), "../../index.html"),
      "utf-8",
    );
    const href = /href="([^"]*css2[^"]*)"/.exec(html)?.[1] ?? "";
    expect(href).toBe(
      "https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400,500&family=JetBrains+Mono:wght@400,500&display=swap",
    );
  });
});
