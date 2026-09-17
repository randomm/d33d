/**
 * Brief tests — the "what are we building" overlay (top-left).
 */

import { render, screen } from "@testing-library/react";
import { describe, it, expect } from "vitest";
import { Brief } from "../Brief";

describe("Brief", () => {
  it("renders the eyebrow line", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel")).toBeTruthy();
    expect(screen.getByTestId("brief-panel").textContent).toBe("What we're building");
  });

  it("renders as a chip when isChip is true", () => {
    render(<Brief isChip inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("chip");
  });

  it("renders as a full panel when isChip is false", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    expect(screen.getByTestId("brief-panel").getAttribute("data-mode")).toBe("full");
  });

  it("positions with the given inset", () => {
    render(<Brief isChip={false} inset={24} conversationCollapsed={false} />);
    const el = screen.getByTestId("brief-panel");
    expect(el.style.top).toBe("24px");
    expect(el.style.left).toBe("24px");
    expect(el.style.position).toBe("absolute");
    expect(el.style.zIndex).toBe("10");
  });

  it("overrides margin-top only when the conversation is not collapsed (byte-identical to the pre-extraction inline style)", () => {
    const { unmount } = render(
      <Brief isChip={false} inset={24} conversationCollapsed={false} />
    );
    expect(screen.getByTestId("brief-panel").style.marginTop).toBe("0px");
    unmount();
    render(<Brief isChip={false} inset={24} conversationCollapsed />);
    expect(screen.getByTestId("brief-panel").style.marginTop).toBe("");
  });
});
