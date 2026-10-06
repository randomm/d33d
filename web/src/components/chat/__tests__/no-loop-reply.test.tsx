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
async function captureStreamHandlers(client: ApiClient): Promise<{
  handlers: Parameters<ApiClient["streamEvents"]>[1];
  resolve: () => void;
}> {
  let handlers: Parameters<ApiClient["streamEvents"]>[1] | null = null;
  let resolve: (() => void) | null = null;
  vi.spyOn(client, "streamEvents").mockImplementation((_id, h) => {
    handlers = h;
    return new Promise<void>((r) => {
      resolve = r;
    });
  });
  await waitFor(() => expect(handlers).not.toBeNull());
  return { handlers: handlers!, resolve: () => resolve?.() };
}



describe("App — no-loop replies never show the design indicator (issue #349)", () => {
  let client: ApiClient;

  beforeEach(() => {
    client = makeClient();
  });

  it("a no-version question: a kind=\"answer\" done frame produces no \"Generating design…\" stage and no pending history slot", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("How deep is it?");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

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
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });
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
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

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

    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });
    expect(screen.queryByTestId("design-loop-progress")).toBeNull();
    expect(screen.queryByTestId("filmstrip-pending")).toBeNull();
  });

  it("a real design run still shows the stage and the pending slot (unchanged behaviour)", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers } = await captureStreamHandlers(client);

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

  // ----------------------------------------------------------------
  // Issue #388 — the composer queue
  // ----------------------------------------------------------------

  it("a no-loop reply followed by a queued message → one POST after the answer", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // While the run is in flight, type and submit a second message.
    // The Send button is disabled (inFlight=true) but the form's
    // onSubmit handler is on the form element, so submitting the form
    // directly still routes through handleSendMessage, which detects
    // the in-flight state and queues the message (operator decision 1:
    // click-to-terminal-frame, every composer send queues).
    // Type in the chat input and submit the form.
    // The Send button is disabled (inFlight=true), but the form's
    // onSubmit handler is on the form element, so submitting the
    // form directly (as Enter would) still calls handleSendMessage.
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it taller" } });
    });
    const form = chatInput.closest("form");
    act(() => {
      fireEvent.submit(form!);
    });

    // The queued message should appear in the transcript with the caption
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });
    expect(screen.getByTestId("queued-caption").textContent).toBe(
      copy.queued.caption,
    );

    // The queued message text is in the transcript
    const queuedTurn = screen
      .getAllByTestId("chat-msg-user")
      .find((el) => el.textContent?.includes("make it taller"));
    expect(queuedTurn).toBeTruthy();

    // Now the run ends (done frame).
    act(() => {
      handlers.onDone?.({
        message: "Design loop passed validation",
      });
    });

    // Let the stream drain (the .finally increments runEndCount, which
    // re-keys the flush effect with the CURRENT queued text — the
    // onDone-triggered effect run saw a stale closure, so the
    // .finally drain is the reliable flush trigger).
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // The queued message should be flushed (one POST).
    // The flush re-enters continueSend, which calls postChat.
    // The postChat mock is already set up to resolve.
    // After the flush, the queued caption should be gone.
    await waitFor(() => {
      expect(screen.queryByTestId("queued-caption")).toBeNull();
    });

    // The flush should have called postChat once more (total: 2 —
    // the original send + the flush).
    expect(client.postChat).toHaveBeenCalledTimes(2);

    // The flushed message should be a real user turn (no queued caption).
    const flushedTurn = screen
      .getAllByTestId("chat-msg-user")
      .find((el) => el.textContent?.includes("make it taller"));
    expect(flushedTurn).toBeTruthy();
  });

  it("two sends during a run → one POST carrying the second text", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // Send message A (queued).
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it taller" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });

    // Send message B (replaces A in the queue slot).
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it 40 mm tall" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });

    // The queue now shows B's text (A was replaced, not appended).
    await waitFor(() => {
      const queuedTurns = screen
        .getAllByTestId("chat-msg-user")
        .filter((el) => el.querySelector('[data-testid="queued-caption"]'));
      expect(queuedTurns).toHaveLength(1);
      expect(queuedTurns[0].textContent).toContain("make it 40 mm tall");
      expect(queuedTurns[0].textContent).not.toContain("make it taller\n");
    });

    // The run ends.
    act(() => {
      handlers.onDone?.({ message: "Design loop passed validation" });
    });
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // Exactly one flush POST (total: original + flush = 2).
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(2);
    // The flush carried the SECOND text.
    const flushCall = postChatMock.mock.calls[1];
    expect(flushCall[1].message).toBe("make it 40 mm tall");
  });

  it("a double click before the first frame → the second send queues, not POSTs, and flushes once after the answer", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // No design-loop-start frame yet — we are in the pre-first-frame
    // window (sendInFlightRef is true, designLoopInFlight is false).
    // A second composer send in this window must QUEUE, not POST.
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it 40 mm" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });

    // The second message is queued (caption visible), not POSTed.
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });
    // postChat was called exactly once (the original send only).
    expect(client.postChat).toHaveBeenCalledTimes(1);

    // The run ends with a no-loop answer (the terminal frame for this
    // send — no design-loop-start frame was ever emitted, so this is
    // the pre-first-frame flush window: the .finally closes it).
    act(() => {
      handlers.onDone?.({
        kind: "answer",
        message: "It is 30.0 mm wide.",
      });
    });
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // The queued message is flushed exactly once after the answer — one
    // POST total beyond the original (total: 2). This is the acceptance
    // case the pre-first-frame window exercises (a no-loop reply
    // followed by a queued message): the flush re-enters the send path
    // with the CURRENT transcript (the answer is in messages by then),
    // and the flushed turn is a real user message (no queued caption).
    await waitFor(() => {
      expect(screen.queryByTestId("queued-caption")).toBeNull();
    });
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(2);
    expect(postChatMock.mock.calls[1][1].message).toBe("make it 40 mm");
    const flushedTurn = screen
      .getAllByTestId("chat-msg-user")
      .find((el) => el.textContent?.includes("make it 40 mm"));
    expect(flushedTurn).toBeTruthy();
  });

  it("a queued message survives an error frame and is then sent", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // Queue a message during the run.
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it taller" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });

    // The run ends with an ERROR frame — the error-only terminal path:
    // the server emits `error` and the stream closes (no done frame).
    // Resolving the streamEvents promise fires the .finally, which
    // clears designLoopInFlight and bumps runEndCount — the flush
    // effect's keys, which re-runs it with the CURRENT callback.
    act(() => {
      handlers.onError?.({
        message: "Design loop exhausted: error_class_not_ok",
        reason: "error_class_not_ok",
      });
    });
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // The error frame ended the run; the queued message is flushed
    // exactly once after it (the .finally is the terminal signal —
    // faithful to production, where the stream closes after `error`).
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(2);
    expect(postChatMock.mock.calls[1][1].message).toBe("make it taller");
    expect(screen.queryByTestId("queued-caption")).toBeNull();
  });

  it("an error-only terminal (no done frame) still flushes the queued message exactly once", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // Queue a message during the run.
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it 40 mm" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });

    // The run ends with an ERROR frame ONLY — no done frame follows. In
    // production the stream then closes and the .finally is the terminal
    // (it clears designLoopInFlight and bumps runEndCount, which re-keys
    // the flush effect); onDone never runs on this path.
    act(() => {
      handlers.onError?.({
        message: "Design loop exhausted: error_class_not_ok",
        reason: "error_class_not_ok",
      });
    });
    // The stream closes — the .finally fires.
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // The queued message was flushed exactly once (original + flush = 2).
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(2);
    expect(postChatMock.mock.calls[1][1].message).toBe("make it 40 mm");
    expect(screen.queryByTestId("queued-caption")).toBeNull();
  });

  it("a queued 'yes' after a run that replaced the pending offer is a plain fresh chat message — no offer id or token (issue #388, operator decision 2026-10-05: the queued message is routed fresh when the run ends)", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // The run produced an offer (the offer the queued "yes" was meant for).
    act(() => {
      handlers.onProgress("offer", { step: "offer", message: "Should I use 12 mm wall thickness?" });
    });

    // Queue "yes" while the run is still in flight (offer A pending).
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "yes" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });

    // The run ends — it replaced offer A (a newer version superseded it).
    act(() => {
      handlers.onDone?.({ message: "Design loop passed validation" });
    });
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });

    // The queued "yes" is flushed as a PLAIN fresh chat message: the POST
    // body carries only `message` + `chat_history` — no offer id, token,
    // or confirm field. The backend's own offer check (pending offers in
    // versions / fill_recut / confirm_offer) decides whether it is an
    // acceptance; the frontend does not pin it to the old offer.
    await waitFor(() => {
      expect(screen.queryByTestId("queued-caption")).toBeNull();
    });
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(2);
    const flushedBody = postChatMock.mock.calls[1][1];
    expect(flushedBody.message).toBe("yes");
    expect(flushedBody.offer_id).toBeUndefined();
    expect(flushedBody.offer_token).toBeUndefined();
    expect(flushedBody.confirm).toBeUndefined();
    // It is a real user turn in the transcript (no queued caption).
    const flushedTurn = screen
      .getAllByTestId("chat-msg-user")
      .find((el) => el.textContent?.includes("yes"));
    expect(flushedTurn).toBeTruthy();
  });

  it("a queued message can be cancelled — nothing is sent when the run ends (issue #388, operator decision 2026-10-05)", async () => {
    render(<App client={client} />);
    sendFirstComposerMessage("make me a 30 mm plate");
    const { handlers, resolve: resolveStream } = await captureStreamHandlers(client);

    // The loop starts.
    act(() => {
      handlers.onProgress("design-loop-start", { step: "design-loop-start" });
    });

    // Queue a message during the run.
    const chatInput = screen.getByTestId("chat-input");
    act(() => {
      fireEvent.change(chatInput, { target: { value: "make it 40 mm" } });
    });
    act(() => {
      fireEvent.submit(chatInput.closest("form")!);
    });
    await waitFor(() => {
      expect(screen.getByTestId("queued-caption")).toBeTruthy();
    });

    // Cancel the queued message.
    act(() => {
      fireEvent.click(screen.getByTestId("queued-cancel-btn"));
    });
    // The queued turn (caption + text) is gone from the transcript.
    await waitFor(() => {
      expect(screen.queryByTestId("queued-caption")).toBeNull();
    });
    const queuedTurn = screen
      .getAllByTestId("chat-msg-user")
      .find((el) => el.textContent?.includes("make it 40 mm"));
    expect(queuedTurn).toBeUndefined();

    // The run ends — the cancelled message is NOT sent (no flush POST).
    act(() => {
      handlers.onDone?.({ message: "Design loop passed validation" });
    });
    await act(async () => {
      resolveStream();
      await Promise.resolve();
    });
    const postChatMock = client.postChat as unknown as ReturnType<typeof vi.fn>;
    expect(postChatMock).toHaveBeenCalledTimes(1);
  });
});
