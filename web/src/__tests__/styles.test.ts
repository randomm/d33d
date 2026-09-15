/**
 * Stylesheet tests (issue #81): assert the single stylesheet exists,
 * defines the token block, activates the class names the components
 * already use, and is imported from main.tsx.
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
      "--color-bg",
      "--color-bg-subtle",
      "--color-border",
      "--color-muted",
      "--color-fg",
      "--color-accent",
      "--font-size-xs",
      "--font-size-sm",
      "--font-size-base",
      "--font-size-lg",
      "--line-height",
      "--font-family",
    ]) {
      expect(root).toContain(`${token}:`);
    }
  });

  it("sets the base font stack, 14px size, and line height on body", () => {
    const body = ruleFor(readStylesheet(), "body");
    expect(body).toContain("font-family: var(--font-family)");
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
    expect(user).toContain("background-color: var(--color-bg-subtle)");
    expect(ruleFor(css, ".chat-msg--assistant")).not.toBeNull();
    // The lasso overlay's containing block depends on .viewer-pane's
    // inline geometry (issues #74/#76) — colour/border only, never
    // position/width/height/overflow.
    const pane = ruleFor(css, ".viewer-pane");
    expect(pane).not.toMatch(/\b(position|width|height|overflow)\s*:/);
  });

  it("wraps SCAD content (pre-wrap, overflow-x auto, monospace) and styles buttons with :hover", () => {
    const css = readStylesheet();
    const content = ruleFor(css, ".chat-msg-content");
    expect(content).toContain("white-space: pre-wrap");
    expect(content).toContain("overflow-x: auto");
    expect(content).toContain("font-family: var(--font-mono)");
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
});
