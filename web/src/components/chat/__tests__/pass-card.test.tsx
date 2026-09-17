/**
 * PassCard tests — the presentational shell for an assistant turn that
 * produced a version (issue #116 shell; content arrives with W10).
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PassCard } from "../PassCard";
import type { RenderImage } from "../../../lib/renderImage";

const views: RenderImage[] = [
  { filename: "view_00_front.png", src: "data:image/png;base64,AAA" },
  { filename: "view_01_back.png", src: "data:image/png;base64,AAA" },
];

describe("PassCard", () => {
  it("renders the version label when a version id is present", () => {
    render(<PassCard versionId={4} views={views} />);
    expect(screen.getByTestId("pass-card-version").textContent).toBe("v4");
  });

  it("renders no version label when no version exists yet", () => {
    render(<PassCard versionId={null} views={views} />);
    expect(screen.getByTestId("pass-card-version").textContent).toBe("");
  });

  it("renders one image per view that arrived", () => {
    render(<PassCard versionId={null} views={views} />);
    expect(screen.getByTestId("pass-card-view-view_00_front.png")).toBeTruthy();
    expect(screen.getByTestId("pass-card-view-view_01_back.png")).toBeTruthy();
    expect(screen.getAllByTestId(/^pass-card-view-/)).toHaveLength(2);
  });

  it("records the view count on the card", () => {
    render(<PassCard versionId={null} views={views} />);
    expect(screen.getByTestId("pass-card").getAttribute("data-views")).toBe("2");
  });

  it("renders no view images when none arrived", () => {
    render(<PassCard versionId={1} views={[]} />);
    expect(screen.queryAllByTestId(/^pass-card-view-/)).toHaveLength(0);
  });
});
