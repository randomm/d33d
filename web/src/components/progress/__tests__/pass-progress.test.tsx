/**
 * PassProgress tests — the design-loop stage indicator.
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { PassProgress } from "../PassProgress";

describe("PassProgress", () => {
  it("renders 'Generating design…' on design-loop-start", () => {
    render(<PassProgress step="design-loop-start" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Generating design…");
  });

  it("renders 'Rendering and checking…' on design-loop-pass", () => {
    render(<PassProgress step="design-loop-pass" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Rendering and checking…");
  });

  it("renders 'Saving version…' on version-created", () => {
    render(<PassProgress step="version-created" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Saving version…");
  });

  it("renders the generic fallback for an unknown step", () => {
    render(<PassProgress step="some-unknown-step" elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Working on your design…");
  });

  it("renders the generic fallback for null step", () => {
    render(<PassProgress step={null} elapsed={5} />);
    expect(screen.getByTestId("design-loop-stage").textContent).toBe("Working on your design…");
  });

  it("never shows the raw step token", () => {
    render(<PassProgress step="some-unknown-token" elapsed={5} />);
    expect(screen.getByTestId("design-loop-progress").textContent).not.toContain("some-unknown-token");
  });

  it("shows the elapsed seconds", () => {
    render(<PassProgress step={null} elapsed={12} />);
    expect(screen.getByTestId("design-loop-elapsed").textContent).toBe("12s");
  });

  it("renders the progress bar", () => {
    render(<PassProgress step={null} elapsed={0} />);
    expect(screen.getByTestId("design-loop-progress-bar")).toBeTruthy();
  });
});
