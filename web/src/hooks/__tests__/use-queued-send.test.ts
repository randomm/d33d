/**
 * useQueuedSend — focused unit tests (issue #388, the extracted hook).
 * Exercises the queue state machine in isolation: queue, replace, cancel,
 * flush-once on done, flush-once on an error-only terminal, no flush when
 * cancelled, and the stale-selection belt path.
 *
 * The flush is driven by a real state transition (designLoopInFlight
 * false via a state setter + signalRunEnd), which re-keys the flush effect.
 * `waitFor` polls with real timers so the effect settles before assertions.
 */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { act, cleanup, waitFor } from "@testing-library/react";
import { renderHook } from "@testing-library/react";
import { useState } from "react";
import { useQueuedSend } from "../useQueuedSend";
import type { ChatMessage } from "../../components/chat/ChatPanel";

type Args = Parameters<typeof useQueuedSend>[0];

function makeArgs(overrides?: Partial<Args>): Args {
  const base = {
    designLoopInFlight: false,
    projectId: 1,
    setMessages: vi.fn(),
    continueSend: vi.fn(),
    nextMsgId: vi.fn().mockImplementation((suffix?: string) => `msg-test-${suffix ?? ""}`),
    ensureProject: vi.fn().mockResolvedValue(1),
    handleProjectCreationFailure: vi.fn(),
    pendingSelection: null,
    clearPendingSelection: vi.fn(),
  };
  return { ...base, ...overrides } as Args;
}

function setMessagesSpy(initial: ChatMessage[]): {
  setMessages: import("react").Dispatch<import("react").SetStateAction<ChatMessage[]>>;
  messages: () => ChatMessage[];
} {
  let current: ChatMessage[] = initial;
  const spy = vi.fn(
    (updater: import("react").SetStateAction<ChatMessage[]>) => {
      current = typeof updater === "function" ? updater(current) : updater;
    },
  );
  return {
    setMessages: spy,
    messages: () => current,
  };
}

/**
 * Wrap the hook in a component that owns `inFlight` state, so the flush
 * effect's `designLoopInFlight` key changes through a real React state
 * update (not a renderHook prop swap). Returns the hook result + a
 * setter to flip the in-flight flag.
 */
function useTestHook(args: Omit<Args, "designLoopInFlight">) {
  const [inFlight, setInFlight] = useState(true);
  const hook = useQueuedSend({ ...args, designLoopInFlight: inFlight });
  return { ...hook, setInFlight };
}

describe("useQueuedSend", () => {
  beforeEach(() => {
    cleanup();
    vi.clearAllMocks();
  });

  it("queues a message while a run is in flight (does not send)", () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ designLoopInFlight: true, setMessages });
    const { result } = renderHook(() => useQueuedSend(args));

    act(() => {
      result.current.handleSendMessage("make it taller");
    });

    expect(args.continueSend).not.toHaveBeenCalled();
    expect(result.current.queuedText).toBe("make it taller");
    const msgs = messages();
    expect(msgs).toHaveLength(1);
    expect(msgs[0].queued).toBe(true);
    expect(msgs[0].content).toBe("make it taller");
  });

  it("replaces the queued message on a second send (does not append a second turn)", () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ designLoopInFlight: true, setMessages });
    const { result } = renderHook(() => useQueuedSend(args));

    act(() => {
      result.current.handleSendMessage("make it taller");
    });
    act(() => {
      result.current.handleSendMessage("make it 40 mm tall");
    });

    expect(result.current.queuedText).toBe("make it 40 mm tall");
    const msgs = messages();
    const queued = msgs.filter((m) => m.queued);
    expect(queued).toHaveLength(1);
    expect(queued[0].content).toBe("make it 40 mm tall");
  });

  it("cancel: drops the queued turn and clears the slot; no flush on run end", () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ designLoopInFlight: true, setMessages });
    const { result } = renderHook(() => useQueuedSend(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });
    act(() => {
      result.current.cancelQueuedMessage();
    });

    expect(result.current.queuedText).toBeNull();
    expect(messages().filter((m) => m.queued)).toHaveLength(0);

    // The run ends — the cancelled message is NOT sent (no flush POST).
    act(() => {
      result.current.signalRunEnd();
    });
    expect(args.continueSend).not.toHaveBeenCalled();
  });

  it("flush-once on done: the queued message is sent exactly once when the run ends", async () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ setMessages });
    const { result } = renderHook(() => useTestHook(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });
    expect(result.current.queuedText).toBe("make it 40 mm");

    // The run ends: the terminal frame clears designLoopInFlight (the
    // state setter flips it), and the stream's .finally fires
    // (signalRunEnd). The flush effect runs after the render triggered
    // by designLoopInFlight=false and the runEndCount bump (both the
    // effect's keys).
    act(() => {
      result.current.setInFlight(false);
    });
    act(() => {
      result.current.signalRunEnd();
    });
    await waitFor(() => {
      expect(args.continueSend).toHaveBeenCalledTimes(1);
    });

    expect(args.continueSend).toHaveBeenCalledWith("make it 40 mm", undefined, true);
    expect(result.current.queuedText).toBeNull();
    expect(messages().filter((m) => m.queued)).toHaveLength(0);
  });

  it("flush-once on an error-only terminal (no done frame): the .finally still triggers the flush", async () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ setMessages });
    const { result } = renderHook(() => useTestHook(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });
    expect(result.current.queuedText).toBe("make it 40 mm");

    // Error-only terminal: the stream closes without a done frame.
    // The .finally fires (signalRunEnd); designLoopInFlight was already
    // cleared by the error frame.
    act(() => {
      result.current.setInFlight(false);
    });
    act(() => {
      result.current.signalRunEnd();
    });
    await waitFor(() => {
      expect(args.continueSend).toHaveBeenCalledTimes(1);
    });

    expect(args.continueSend).toHaveBeenCalledWith("make it 40 mm", undefined, true);
    expect(result.current.queuedText).toBeNull();
    expect(messages().filter((m) => m.queued)).toHaveLength(0);
  });

  it("no flush when cancelled: cancelling before the run ends means nothing is sent", () => {
    const { setMessages } = setMessagesSpy([]);
    const args = makeArgs({ setMessages });
    const { result } = renderHook(() => useTestHook(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });
    act(() => {
      result.current.cancelQueuedMessage();
    });

    // The run ends.
    act(() => {
      result.current.setInFlight(false);
    });
    act(() => {
      result.current.signalRunEnd();
    });

    // The cancelled message was NOT sent.
    expect(args.continueSend).not.toHaveBeenCalled();
  });

  it("stale-selection guard: a pending selection at flush time is cleared (belt path); the message still flushes as a plain chat message (never silently dropped)", async () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const staleSelection = { viewId: "front" };
    const args = makeArgs({
      setMessages,
      pendingSelection: staleSelection,
    });
    const { result } = renderHook(() => useTestHook(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });
    expect(result.current.queuedText).toBe("make it 40 mm");

    // The run ends; the pending selection is still set (stale).
    act(() => {
      result.current.setInFlight(false);
    });
    act(() => {
      result.current.signalRunEnd();
    });
    await waitFor(() => {
      expect(args.continueSend).toHaveBeenCalledTimes(1);
    });

    // The stale selection was cleared (the belt path).
    expect(args.clearPendingSelection).toHaveBeenCalledTimes(1);
    // The queued message was STILL flushed (not silently dropped) — as a
    // plain chat message (internal=true, no selection attached).
    expect(args.continueSend).toHaveBeenCalledWith("make it 40 mm", undefined, true);
    expect(result.current.queuedText).toBeNull();
    expect(messages().filter((m) => m.queued)).toHaveLength(0);
  });

  it("a direct send (no run in flight) sends immediately, not through the queue", () => {
    const { setMessages, messages } = setMessagesSpy([]);
    const args = makeArgs({ designLoopInFlight: false, setMessages });
    const { result } = renderHook(() => useQueuedSend(args));

    act(() => {
      result.current.handleSendMessage("make it 40 mm");
    });

    // The message was sent immediately (not queued).
    expect(args.continueSend).toHaveBeenCalledTimes(1);
    expect(args.continueSend).toHaveBeenCalledWith("make it 40 mm");
    // Nothing in the queue.
    expect(result.current.queuedText).toBeNull();
    expect(messages().filter((m) => m.queued)).toHaveLength(0);
  });
});
