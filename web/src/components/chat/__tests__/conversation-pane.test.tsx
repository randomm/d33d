/**
 * ConversationPane tests — the conversation layer's shell (issue #195,
 * extracted from App.tsx; the #116/W8 lineage).
 *
 * Exercises the shell directly (unit boundary): the floating vs docked
 * layout (issue #194), the rail collapse (the header — including the
 * collapse button — is App's, so the pane receives it as a prop), the
 * docked filmstrip render site (docked only; the floating strip stays at
 * the stage level in App.tsx), the failure card, and the photo +
 * dimension-canvas surfaces.
 */

import { render, screen, fireEvent } from "@testing-library/react";
import { describe, it, expect, vi } from "vitest";
import type { ReactNode } from "react";

/** Konva stubs — DimensionCanvas renders a react-konva Stage under jsdom,
 *  which has no canvas 2D context; the same stub pattern as the canvas's
 *  own suite. */
vi.mock("react-konva", () => ({
  Stage: ({ children }: { children?: ReactNode }) => (
    <div data-testid="konva-stage">{children}</div>
  ),
  Layer: ({ children }: { children?: ReactNode }) => (
    <div data-testid="konva-layer">{children}</div>
  ),
  Line: () => <div data-testid="konva-line" />,
  Text: ({ text }: { text?: string }) => <span data-testid="konva-text">{text}</span>,
  Image: () => <div data-testid="konva-image" />,
  Group: ({ children }: { children?: ReactNode }) => (
    <div data-testid="konva-group">{children}</div>
  ),
}));
import { ConversationPane } from "../ConversationPane";
import type { ChatMessage } from "../ChatPanel";
import type { VersionTimelineEntry } from "../../../lib/api";
import type { ViewProgressState } from "../../../lib/viewProgress";
import type { DisplayError } from "../../../lib/errorMapping";
import copy from "../../../copy";

function makeVersion(overrides: Partial<VersionTimelineEntry> = {}): VersionTimelineEntry {
  return {
    id: 1,
    name: "v1",
    params: {},
    created_by_message: "a box",
    parent: null,
    restored_from: null,
    forked_from: null,
    pinned: false,
    archived: false,
    thumbnail: null,
    created_at: "2026-01-01T00:00:00Z",
    diff_count: 0,
    exported_at: null,
    ...overrides,
  };
}

const IDLE_PROGRESS: ViewProgressState = {
  done: 0,
  total: 0,
  renderingStem: null,
  iteration: 0,
  doneStems: [],
};

interface PaneOverrides {
  docked?: boolean;
  collapsed?: boolean;
  projectId?: number | null;
  inFlight?: boolean;
  versions?: VersionTimelineEntry[];
  streamError?: DisplayError | null;
  photoSrc?: string | null;
  photoDimensions?: { width: number; height: number } | null;
  header?: ReactNode | null;
  messages?: ChatMessage[];
}

function renderPane(overrides: PaneOverrides = {}) {
  const onCollapsedChange = vi.fn();
  const onSend = vi.fn();
  const onCompareSelect = vi.fn();
  const onOpenSheet = vi.fn();
  const onPhotoUploaded = vi.fn();
  const onPhotoError = vi.fn();
  const result = render(
    <ConversationPane
      docked={overrides.docked ?? false}
      collapsed={overrides.collapsed ?? false}
      onCollapsedChange={onCollapsedChange}
      projectId={overrides.projectId ?? 7}
      messages={overrides.messages ?? [{ id: "m1", role: "user", content: "a box" }]}
      onSend={onSend}
      inFlight={overrides.inFlight ?? false}
      onBesidePhoto={vi.fn()}
      envelope={null}
      hideComposer={false}
      lastUserMessage=""
      versions={overrides.versions ?? []}
      onCompareSelect={onCompareSelect}
      onOpenSheet={onOpenSheet}
      sheetOpenFor={null}
      header={overrides.header === undefined ? <div data-testid="pane-header-stub">header</div> : overrides.header}
      designLoopStep={null}
      designLoopElapsed={0}
      viewProgress={IDLE_PROGRESS}
      streamError={overrides.streamError ?? null}
      photoSrc={overrides.photoSrc ?? null}
      photoDimensions={overrides.photoDimensions ?? null}
      onPhotoUploaded={onPhotoUploaded}
      onPhotoError={onPhotoError}
    />,
  );
  return { onCollapsedChange, onSend, onCompareSelect, onOpenSheet, onPhotoUploaded, onPhotoError, ...result };
}

describe("ConversationPane", () => {
  it("renders the floating layout with the header, chat, upload and photo surfaces", () => {
    renderPane();
    const pane = screen.getByTestId("app-left-pane");
    expect(pane).toBeTruthy();
    expect(screen.getByTestId("pane-header-stub")).toBeTruthy();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    expect(screen.getByTestId("photo-upload")).toBeTruthy();
    // No docked notice, no rail, no docked filmstrip in floating mode.
    expect(screen.queryByTestId("conversation-docked-notice")).toBeNull();
    expect(screen.queryByTestId("conversation-rail")).toBeNull();
    expect(screen.queryByTestId("version-filmstrip")).toBeNull();
  });

  it("renders the docked layout with the docked notice and the in-bar filmstrip (issue #194)", () => {
    renderPane({
      docked: true,
      projectId: 7,
      versions: [makeVersion(), makeVersion({ id: 2, name: "v2" })],
    });
    const notice = screen.getByTestId("conversation-docked-notice");
    expect(notice).toHaveTextContent(copy.shell.conversationDocked);
    // The filmstrip renders INSIDE the docked bar.
    const strip = screen.getByTestId("version-filmstrip");
    expect(strip).toBeTruthy();
    expect(screen.getByTestId("app-left-pane").contains(strip)).toBe(true);
  });

  it("does NOT render the in-bar filmstrip when docked but the project is not created yet", () => {
    renderPane({ docked: true, projectId: null, versions: [] });
    expect(screen.getByTestId("conversation-docked-notice")).toBeTruthy();
    expect(screen.queryByTestId("version-filmstrip")).toBeNull();
  });

  it("hides the header and shows the rail while collapsed; the rail reopens the pane", () => {
    const { onCollapsedChange } = renderPane({
      messages: [
        { id: "m1", role: "user", content: "a" },
        { id: "m2", role: "user", content: "b" },
        { id: "m3", role: "user", content: "c" },
      ],
      collapsed: true,
    });
    // The header is App's — it is not rendered while collapsed.
    expect(screen.queryByTestId("pane-header-stub")).toBeNull();
    const rail = screen.getByTestId("conversation-rail");
    expect(rail).toHaveTextContent(copy.shell.conversationCollapsed(3));
    expect(rail).toHaveTextContent(copy.shell.openConversation);
    fireEvent.click(rail);
    expect(onCollapsedChange).toHaveBeenCalledWith(false);
  });

  it("renders the rest of the pane when no header prop is supplied (App passes null when collapsed)", () => {
    renderPane({ header: null });
    expect(screen.queryByTestId("pane-header-stub")).toBeNull();
    expect(screen.getByTestId("chat-panel")).toBeTruthy();
    expect(screen.getByTestId("photo-upload")).toBeTruthy();
  });

  it("shows the pass progress while a pass is in flight", () => {
    renderPane({ inFlight: true, versions: [makeVersion()] });
    expect(screen.getByTestId("design-loop-progress")).toBeTruthy();
  });

  it("renders the failure card when streamError is set", () => {
    renderPane({
      streamError: { message: "The build failed.", retryable: false },
    });
    const card = screen.getByTestId("app-error");
    expect(card).toHaveTextContent("The build failed.");
  });

  it("renders the dimension canvas once the photo and its dimensions are known", () => {
    const { unmount } = renderPane({
      photoSrc: "data:image/png;base64,AAA",
      photoDimensions: { width: 800, height: 600 },
    });
    expect(screen.getByTestId("dimension-canvas-container")).toBeTruthy();
    expect(screen.queryByTestId("dimension-canvas-unavailable")).toBeNull();
    unmount();
  });

  it("shows the honest unavailable notice when the photo's dimensions are zero", () => {
    renderPane({
      photoSrc: "data:image/png;base64,AAA",
      photoDimensions: { width: 0, height: 0 },
    });
    expect(screen.queryByTestId("dimension-canvas-container")).toBeNull();
    expect(screen.getByTestId("dimension-canvas-unavailable")).toBeTruthy();
  });

  it("renders neither dimension surface before a photo is uploaded", () => {
    renderPane();
    expect(screen.queryByTestId("dimension-canvas-container")).toBeNull();
    expect(screen.queryByTestId("dimension-canvas-unavailable")).toBeNull();
  });
});
