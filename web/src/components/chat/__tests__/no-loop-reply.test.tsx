/**
 * No-loop reply tests (issue #349): a chat message that does NOT start a
 * design loop — a no-version question ("How deep is it?" on a fresh
 * project), a bare affirmation with no pending offer ("yes") — must
 * produce no "Generating design…" stage indicator and no pending history
 * (filmstrip) slot in the SPA.
 *
 * The backend answers these with a terminal `kind: "answer"` done frame
 * and NO design-loop progress frames. The SPA defers the in-flight
 * indicator until the first `design-loop-*` progress frame (issue #349),
 * and the done frame clears any in-flight state before it renders — so
 * neither surface may appear at any point for a no-loop reply.
 *
 * The App-level flow is exercised end to end: the real send handler, the
 * real stream handlers, with postChat/streamEvents stubbed to replay the
 * exact frame sequence the backend emits for each case.
 */

import { render, screen, fireEvent, act, waitFor } from "@testing-library/react";
import { describe, it, expect, vi, beforeEach } from "vitest";
import { useEffect } from "react";
import App from "../../../App";
import { ApiClient, type Project } from "../../../lib/api";
import copy from "../../../copy";

/** jsdom has no GPU context — ModelViewer owns a real three.js
 *  WebGLRenderer. Mock it with a lightweight component (its own test suite
 *  covers the three.js internals); App just needs it mounted. */
vi.mock("../../../components/viewer/ModelViewer", () => ({
  ModelViewer: (props: {
    data: ArrayBuffer | null;
    format: string;
    onReady?: (handle: unknown) => void;
    onLoaded?: (result: unknown) => void;
    hideEmptyState?: boolean;
  }) => {
    useEffect(() => {
      props.onReady?.({
        camera: { position: { x: 0, y: 100, z: 200 } },
        controls: { target: { x: 0, y: 0, z: 0 } },
        modelRoot: null,
      });
      // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [props.data]);
    return (
      <div
        data-testid="model-viewer-mock"
        data-format={props.format}
        data-has-data={props.data !== null}
      >
        {props.data === null && !props.hideEmptyState && (
          <div data-testid="viewer-empty">{copy.shell.viewerEmpty}</div>
        )}
      </div>
    );
  },
  resolvePointPick: vi.fn(),
}));

const PROJECT: Project = {
  id: 1,
  name: "t",
  git_repo_path: "/tmp/repo",
  tags: [],
  notes: "",
  source_photo_path: null,
  created_at: "2026-01-01T00:00:00Z",
};

function makeClient(): ApiClient {
  const client = new ApiClient();
  vi.spyOn(client, "createProject").mockResolvedValue(PROJECT);
  vi.spyOn(client, "listVersions").mockResolvedValue([]);
  vi.spyOn(client, "getProject").mockResolvedValue(PROJECT);
  vi.spyOn(client, "getDesignState").mockResolvedValue({
    entries: [],
    part: null,
    history_missing: false,
  });
  vi.spyOn(client, "getEnvelope").mockResolvedValue({
    x: 320,
    y: 320,
    z: 300,
    unit: "mm",
    verified: false,
  });
  vi.spyOn(client, "postChat").mockResolvedValue({ status: "accepted" });
  return client;
}

/** Drive the send through the first-run composer (the only composer while
 *  the first-run screen is up — the no-loop replies keep the project
 *  versionless, so it stays up). */
function sendFirstComposerMessage(text: string): void {
  const firstRunInput = screen.queryByTestId("first-run-input");
  if (firstRunInput) {
    act(() => {
      fireEvent.change(firstRunInput, { target: { value: text } });
      fireEvent.click(screen.getByTestId("first-run-start-btn"));
    });
    return;
  }
  act(() => {
    fireEvent.change(screen.getByTestId("chat-input"), { target: { value: text } });
    fireEvent.click(screen.getByTestId("chat-send-btn"));
  });
}

/** Capture the stream handlers App passes to streamEvents. */
async function captureStreamHandlers(client: ApiClient) {
  let handlers: Parameters<ApiClient["streamEvents"]>[1] | null = null;
  vi.spyOn(client, "streamEvents").mockImplementation((_id, h) => {
    handlers = h;
    return new Promise(() => {});
  });
  await waitFor(() => expect(handlers).not.toBeNull());
  return handlers!;
}

async function drainStream(client: ApiClient) {
  // Swap the hanging streamEvents promise for a resolved one and flush
  // microtasks, so the `.finally` (the post-stream flag release) runs.
  (client.streamEvents as unknown as ReturnType<typeof vi.fn>).mockResolvedValue(
    undefined,
  );
  await act(async () => {
    await Promise.resolve();
  });
}

describe("App — no-loop replies never show the design indicator (issue #349)", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
  });

  it("a no-version question: a kind=\"answer\" done frame produces no \"Generating design…\" stage and no pending history slot", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("How deep is it?");
    const handlers = await captureStreamHandlers(client);

    // The whole no-loop exchange: the done frame, no progress frames at all.
    act(() => {
      handlers.onDone?.({
        kind: "answer",
        message: "Nothing is built yet — tell me what to make first.",
      });
    });

    // The reply renders as plain text in the transcript.
    await waitFor(() => {
      const answered = screen
        .getAllByTestId("chat-msg-assistant")
        .find((el) => el.textContent?.includes("Nothing is built yet"));
      expect(answered).toBeTruthy();
    });

    // No stage indicator — "Generating design…" never rendered at any
    // point for this reply (it would show the instant this surface
    // existed, which it never did).
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
    expect(screen.queryByTestId("design-loop-stage")).toBeNull();
    // No pending history slot in the filmstrip (no version exists, the
    // pass never went in flight, so the strip is absent — not empty).
    expect(screen.queryByTestId("filmstrip-pending")).toBeNull();
    expect(screen.queryByTestId("filmstrip-pending-name")).toBeNull();
    expect(screen.queryByTestId("version-filmstrip")).toBeNull();

    // Let the stream drain (the finally path) and re-assert: the reply is
    // still plain text and no indicator or pending slot appeared.
    await drainStream(client);
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
    expect(screen.queryByTestId("filmstrip-pending")).toBeNull();
    expect(
      screen
        .getAllByTestId("chat-msg-assistant")
        .some((el) => el.textContent?.includes("Nothing is built yet")),
    ).toBe(true);
  });

  it("a bare affirmation with no pending offer: same — no stage, no pending slot", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("yes");
    const handlers = await captureStreamHandlers(client);

    act(() => {
      handlers.onDone?.({
        kind: "answer",
        message: "There's nothing waiting for a yes right now — what would you like to change?",
      });
    });

    await waitFor(() => {
      const answered = screen
        .getAllByTestId("chat-msg-assistant")
        .find((el) => el.textContent?.includes("There's nothing waiting for a yes"));
      expect(answered).toBeTruthy();
    });
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
    expect(screen.queryByTestId("filmstrip-pending")).toBeNull();

    await drainStream(client);
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
    expect(screen.queryByTestId("filmstrip-pending")).toBeNull();
  });

  it("a real design run still shows the stage and the pending slot (unchanged behaviour)", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const handlers = await captureStreamHandlers(client);

    // The loop starts — this is the FIRST progress frame; the indicator
    // and pending slot may appear from here.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    await waitFor(() => {
      expect(screen.getByTestId("design-loop-progress")).toBeTruthy();
    });
    expect(screen.getByTestId("design-loop-stage").textContent).toBe(
      "Generating design…",
    );
    // The pending history slot appears with the message's name, before the
    // version exists.
    await waitFor(() => {
      expect(screen.getByTestId("filmstrip-pending")).toBeTruthy();
    });
    expect(screen.getByTestId("filmstrip-pending-name").textContent).toBe(
      "make me a 30 mm plate",
    );
  });
});
