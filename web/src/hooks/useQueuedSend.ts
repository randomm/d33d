/**
 * useQueuedSend — the single-slot composer queue (issue #388, extracted
 * from App so the queue state machine does not live inline in the 2000+
 * line shell). The single source of truth for the queue invariants:
 *
 *  1. Every composer send during a run (or in the pre-first-frame window)
 *     fills the single queue slot — it is never a second POST (the server
 *     would reject it with 409). A second send REPLACES the slot's text;
 *     the queued turn's id is stable across replaces.
 *  2. A queued turn is shown LABELLED as queued — a caption and a cancel
 *     button — and is NEVER shown as sent. It leaves the transcript only
 *     when the flush POSTs it (the turn becomes a plain sent message) or
 *     the user cancels it.
 *  3. The queued message can be cancelled — the turn drops and nothing is
 *     sent when the run ends.
 *  4. Programmatic sends (Brief, fill-recut offer buttons) and region-edit
 *     sends do NOT go through this path — gated by their own disabled
 *     controls (the `inFlight` props), never queued.
 *  5. A FAILED flush never loses the message: when the flushed send
 *     rejects (409, network error), the turn is restored as a FAILED
 *     queued turn — labelled, with the `queued.flushFailed` reason and a
 *     resend action that re-enters the NORMAL send path. The slot is NOT
 *     re-armed (no auto-retry on a later terminal signal): the retry is
 *     the user's, via the resend control (documented operator choice —
 *     an automatic retry of a 409 is not safe, a second run may be the
 *     cause).
 *  6. Stale-selection guard: a pending selection at flush time was drawn
 *     for a different message; the flushed POST never re-attaches it —
 *     the queued text routes fresh as a plain chat message and the
 *     selection is cleared (belt path; the RegionEditBar's disabled
 *     Submit is the primary gate). The queued text is never silently
 *     dropped.
 *  7. A run that never ends still settles: `streamEvents` throws on its
 *     client-side total deadline (`streamTotalTimeoutMs` — the deadline
 *     branch in web/src/lib/api.ts fires `onError` and `throw err`), so
 *     the send's `.finally` fires and the flush effect re-keys; the
 *     queued turn can never be stranded unlabelled.
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import { copy } from "../copy";
import type { ChatMessage } from "../components/chat/ChatPanel";

export interface UseQueuedSendArgs {
  /** The authoritative run-in-flight signal (the design-loop flag). */
  designLoopInFlight: boolean;
  /** The settled project id (null before the first send creates it). */
  projectId: number | null;
  /** Transcript writer — the hook appends/replaces/removes the queued
   *  turn and the flush removes it. */
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  /** The settled send path (postChat + stream wiring). The flush
   *  re-enters it via its ref mirror (fresh closure — current transcript).
   *  Returns a promise that rejects when the POST fails (a 409, a
   *  network error) — the hook's flush catches that rejection and
   *  restores the queued message as a failed turn (issue #388, lens
   *  finding 1: a failed flush must not lose the message). */
  continueSend: (text: string, projectIdOverride?: number) => Promise<void>;
  /** The narrow Retry-memory seam (issue #388): the hook calls this ONLY
   *  on non-flush sends — a flush re-send must not clobber the in-flight
   *  run's message (the Retry control retries the in-flight run's
   *  message, not the queued one). The send path itself knows nothing
   *  about the queue. */
  rememberUserMessage: (text: string) => void;
  /** The id factory (the App's monotonically-increasing `nextMsgId`). */
  nextMsgId: (suffix?: string) => string;
  /** The lazy project-creation latch (issue #192 single-flight). */
  ensureProject: () => Promise<number>;
  /** Shared project-creation failure surface (the app-level error card). */
  handleProjectCreationFailure: (e: unknown) => void;
  /** The pending region selection (the stale-selection guard reads a ref
   *  mirror of it; the hook owns the mirror + the flush-time clear). */
  pendingSelection: unknown | null;
  /** Clears the pending selection (the guard's belt path — module doc §6). */
  clearPendingSelection: () => void;
}

/** Drop the single queued turn (by the `queued` flag and the recorded id)
 *  from the transcript. */
function removeQueuedTurn<T extends { id: string; queued?: boolean }>(
  prev: T[],
  idToRemove: string | null,
): T[] {
  return prev.filter((m) => !m.queued && m.id !== idToRemove);
}

export function useQueuedSend({
  designLoopInFlight,
  projectId,
  setMessages,
  continueSend,
  rememberUserMessage,
  nextMsgId,
  ensureProject,
  handleProjectCreationFailure,
  pendingSelection,
  clearPendingSelection,
}: UseQueuedSendArgs) {
  // The queued message text (null = the slot is empty).
  const [queuedText, setQueuedText] = useState<string | null>(null);
  // The flush idempotency latch: exactly one POST per queued message.
  const flushedRef = useRef(false);
  // The pre-first-frame window latch (set on send, cleared on run end).
  const sendInFlightRef = useRef(false);
  // Ref mirror of the newest continueSend (the flush needs the current
  // transcript closure, which the effect's own closure can predate).
  const continueSendRef = useRef(continueSend);
  useEffect(() => {
    continueSendRef.current = continueSend;
  }, [continueSend]);
  // Ref mirror of the pending selection (the stale-selection guard, §6).
  const pendingSelectionRef = useRef(pendingSelection);
  useEffect(() => {
    pendingSelectionRef.current = pendingSelection;
  }, [pendingSelection]);
  // The state key bumped when the .finally clears sendInFlightRef.
  const [runEndCount, setRunEndCount] = useState(0);
  // The queued turn's id (stable across replaces; see the queue-branch
  // comment in handleSendMessage).
  const queuedTurnIdRef = useRef<string | null>(null);

  // The send entry point: queue while a run (or the pre-first-frame
  // window) is open, otherwise direct send. Programmatic sends and
  // region-edit sends never reach this path (module doc §4).
  const handleSendMessage = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      if (designLoopInFlight || sendInFlightRef.current) {
        flushedRef.current = false;
        setQueuedText(trimmed);
        setMessages((prev) => {
          const existing = prev.find((m) => m.queued);
          if (existing) {
            return prev.map((m) =>
              m.queued ? { ...m, content: trimmed, queuedFailure: undefined } : m,
            );
          }
          const qid = nextMsgId("queued");
          queuedTurnIdRef.current = qid;
          return [
            ...prev,
            { id: qid, role: "user" as const, content: trimmed, queued: true },
          ];
        });
        return;
      }

      sendInFlightRef.current = true;
      rememberUserMessage(trimmed);
      if (projectId !== null) {
        // The non-flush send is fire-and-forget: App's internal .catch
        // already surfaces the failure as a turn — the no-op .catch here
        // only prevents an unhandled-rejection warning (the promise now
        // re-throws so the hook's flush can catch it, issue #388).
        void continueSend(trimmed).catch(() => {});
        return;
      }
      void ensureProject()
        .then((id) => continueSend(trimmed, id).catch(() => {}))
        .catch((e) => {
          // A failed project creation releases the latch (the #192
          // single-flight semantics) so the next send retries.
          sendInFlightRef.current = false;
          handleProjectCreationFailure(e);
        });
    },
    [
      projectId,
      continueSend,
      ensureProject,
      handleProjectCreationFailure,
      designLoopInFlight,
      nextMsgId,
      setMessages,
      rememberUserMessage,
    ],
  );

  // The run-end signal: clear the pre-frame latch + bump the flush key.
  const signalRunEnd = useCallback(() => {
    sendInFlightRef.current = false;
    setRunEndCount((c) => c + 1);
  }, []);

  // The run-end flush effect. Non-obvious bit: the effect keys on
  // (designLoopInFlight, runEndCount, queuedText) and the flush is
  // idempotent via flushedRef — exactly one POST per queued message,
  // no matter which terminal signal fires first.
  useEffect(() => {
    if (designLoopInFlight || sendInFlightRef.current) return; // run still in flight
    if (!queuedText || flushedRef.current) return;
    flushedRef.current = true;
    if (pendingSelectionRef.current !== null) {
      // The selection is stale for this message (module doc §6).
      clearPendingSelection();
    }
    const idToRemove = queuedTurnIdRef.current;
    queuedTurnIdRef.current = null;
    setQueuedText(null);
    setMessages((prev) => removeQueuedTurn(prev, idToRemove));
    // The flush re-enters the send path; continueSend returns a promise
    // (the postChat chain) or void (fire-and-forget). The flush catches a
    // rejection (a 409, a network error) to restore the queued message as
    // a failed turn (issue #388, lens finding 1: a failed flush must not
    // lose the message).
    const result = continueSendRef.current(queuedText);
    void result.catch((e: unknown) => {
      // The flushed send REJECTED (module doc §5): restore the message
      // as a failed queued turn — reason + resend action (the resend
      // goes through the normal send path, never a re-flush). The slot
      // is NOT re-armed: no auto-retry on a later terminal signal.
      const detail = e instanceof Error ? e.message : "unknown error";
      const qid = nextMsgId("queued");
      queuedTurnIdRef.current = qid;
      setQueuedText(queuedText);
      setMessages((prev) => [
        ...removeQueuedTurn(prev, idToRemove),
        {
          id: qid,
          role: "user" as const,
          content: queuedText,
          queued: true,
          queuedFailure: detail,
          failure: {
            message: copy.queued.flushFailed,
            detail,
            retryable: true,
          },
          onResendQueued: () => {
            // Resend = a NORMAL send of the same text (clears the failed
            // flag, remembers the message for Retry, goes through the
            // queue-or-direct routing like any composer send).
            setMessages((p) => removeQueuedTurn(p, queuedTurnIdRef.current));
            queuedTurnIdRef.current = null;
            setQueuedText(null);
            handleSendMessage(queuedText);
          },
        },
      ]);
    });
  }, [designLoopInFlight, runEndCount, queuedText, clearPendingSelection, setMessages, nextMsgId, handleSendMessage]);

  // Cancel: drop the queued turn and clear the slot WITHOUT sending.
  const cancelQueuedMessage = useCallback(() => {
    if (queuedText === null) return;
    flushedRef.current = false;
    setQueuedText(null);
    const idToRemove = queuedTurnIdRef.current;
    queuedTurnIdRef.current = null;
    setMessages((prev) => removeQueuedTurn(prev, idToRemove));
  }, [queuedText, setMessages]);

  return {
    queuedText,
    handleSendMessage,
    cancelQueuedMessage,
    signalRunEnd,
  };
}
