/**
 * App layout test — verifies the two-pane layout and that NO auto-generated
 * slider/parameter panel is rendered (spec acceptance 5).
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import App from "../../App";
import type { RenderImage } from "../../App";

describe("App layout", () => {
  it("renders the two-pane shell (left chat + right viewer)", () => {
    render(<App />);
    expect(screen.getByTestId("app-shell")).toBeTruthy();
    expect(screen.getByTestId("app-left-pane")).toBeTruthy();
    expect(screen.getByTestId("app-right-pane")).toBeTruthy();
  });

  it("renders the chat panel in the left pane", () => {
    render(<App />);
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
  });

  it("renders the viewer placeholder in the right pane", () => {
    render(<App />);
    expect(screen.getByTestId("viewer-pane")).toBeTruthy();
    expect(screen.getByTestId("viewer-placeholder")).toBeTruthy();
  });

  it("renders validation status and Export 3MF button", () => {
    render(<App />);
    expect(screen.getByTestId("validation-pane")).toBeTruthy();
    expect(screen.getByTestId("export-3mf-btn")).toBeTruthy();
  });

  it("does NOT render an auto-generated slider/parameter panel", () => {
    const { container } = render(<App />);
    // No slider inputs, no auto-generated parameter list
    expect(container.querySelectorAll("input[type='range']")).toHaveLength(0);
    // The pinned strip exists but starts empty
    expect(screen.getByTestId("pinned-empty")).toBeTruthy();
  });

  it("renders six inline render images when provided", () => {
    const renders: RenderImage[] = [
      { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
      { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
      { filename: "view_02_left.png", src: "data:image/png;base64,AAA" },
      { filename: "view_03_right.png", src: "data:image/png;base64,AAA" },
      { filename: "view_04_top.png", src: "data:image/png;base64,AAA" },
      { filename: "view_05_iso.png", src: "data:image/png;base64,AAA" },
    ];
    render(<App renders={renders} />);
    expect(screen.getByTestId("render-img-view_00_front.png")).toBeTruthy();
    expect(screen.getByTestId("render-img-view_05_iso.png")).toBeTruthy();
    expect(screen.getAllByTestId(/^render-img-/)).toHaveLength(6);
  });
});
