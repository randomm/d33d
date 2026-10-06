/**
 * useQueuedSend — the single-slot composer queue (issue #388, extracted
 * from App so the queue state machine does not live inline in the 2000+
 * line shell). Owns the queued text, the single queued turn in the
 * transcript, replace, cancel, the `continueSendRef` mirror, the run-end
 * flush (exactly once per queued message), and the pending-selection guard.
 *
 * Operator decisions (2026-10-05) implemented here:
 *  1. Every composer send during a run (or in the pre-first-frame window)
 *     fills the single queue slot — it is never a second POST (the server
 *     would reject it with 409). A second send REPLACES the slot's text;
 *     the queued turn's id is stable across replaces.
 *  2. The queued message can be cancelled — the turn drops from the
 *     transcript and nothing is sent when the run ends.
 *  3. Programmatic sends (Brief, fill-recut offer buttons) and region-edit
 *     sends do NOT go through this path — they are gated by their own
 *     disabled controls (the `inFlight` props), never queued.
 *  4. The flush re-enters the send path with `internal: true`, so the
 *     Retry control keeps the IN-FLIGHT run's message, not the queued one.
 *
 * Stale-selection guard: a pending region selection at flush time was
 * drawn for a different message (the region-edit instruction the user
 * never sent — or one sent alongside the queued text). The flushed POST
 * must never re-attach it — the queued text is routed fresh as a plain
 * chat message (operator decision: "the queued message is routed fresh
 * when the run ends"), it is never silently dropped. The selection is
 * cleared (the belt path — the RegionEditBar's disabled Submit is the
 * primary gate) just before the flush.
 *
 * The stream-disconnect case: if the SSE closes without a terminal frame
 * the .finally never fires, the pre-first-frame window stays open, and a
 * queued message stays visibly queued (the cancel control is still
 * available — it is never silently dropped).
 */
import { useCallback, useEffect, useRef, useState } from "react";
import type { Dispatch, SetStateAction } from "react";
import type { ChatMessage } from "../components/chat/ChatPanel";

/** The single queued user turn (the App's ChatMessage carries the same
 *  `queued` flag; the hook only needs these fields). */
export type QueuedChatMessage = ChatMessage & { queued: boolean };

export interface UseQueuedSendArgs {
  /** The authoritative run-in-flight signal (the design-loop flag). */
  designLoopInFlight: boolean;
  /** The settled project id (null before the first send creates it). */
  projectId: number | null;
  /** Transcript writer — the hook appends/replaces/removes the queued
   *  turn and the flush removes it. The App's real `setMessages`
   *  (Dispatch over `ChatMessage`) satisfies this. */
  setMessages: Dispatch<SetStateAction<ChatMessage[]>>;
  /** The settled send path (postChat + stream wiring). The flush
   *  re-enters it via its ref mirror (fresh closure — current transcript). */
  continueSend: (
    text: string,
    projectIdOverride?: number,
    internal?: boolean,
  ) => void;
  /** The id factory (the App's monotonically-increasing `nextMsgId`). */
  nextMsgId: (suffix?: string) => string;
  /** The lazy project-creation latch (issue #192 single-flight). */
  ensureProject: () => Promise<number>;
  /** Shared project-creation failure surface (the app-level error card). */
  handleProjectCreationFailure: (e: unknown) => void;
  /** The pending region selection (the stale-selection guard reads a ref
   *  mirror of it; the hook owns the mirror + the flush-time clear). */
  pendingSelection: unknown | null;
  /** Clears the pending selection (the guard's belt path — see module
   *  doc). Must not lose other state (the App's setter handles that). */
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
  nextMsgId,
  ensureProject,
  handleProjectCreationFailure,
  pendingSelection,
  clearPendingSelection,
}: UseQueuedSendArgs) {
  // The queued message text. Null = the slot is empty (nothing queued).
  const [queuedText, setQueuedText] = useState<string | null>(null);
  // `flushedRef` makes the flush idempotent: exactly one POST per queued
  // message, no matter which terminal signal fires (the done frame and
  // the stream's .finally both re-key the flush effect).
  const flushedRef = useRef(false);
  // `sendInFlightRef` tracks the pre-first-frame window: set when a
  // direct send is initiated, cleared by the run's .finally (which bumps
  // `runEndCount` in the SAME microtask — the flush effect's keys, so a
  // message registered before the terminal frame still gets a flush
  // effect re-run to pick it up). While true, every composer send queues
  // (operator decision 1: click-to-terminal-frame, never a second POST).
  const sendInFlightRef = useRef(false);
  // Ref mirror of continueSend: the flush effect fires from a terminal
  // signal that can arrive before React has re-rendered with the run's
  // assistant reply (the pre-first-frame window — the .finally bumps
  // runEndCount but designLoopInFlight never transitioned, so no
  // render-in-flight callback is fresher than the effect's). A callback
  // built before the reply landed has a stale `messages` closure (the
  // flush would build chat_history from the pre-run transcript). The ref
  // always points at the newest callback (current messages).
  const continueSendRef = useRef(continueSend);
  useEffect(() => {
    continueSendRef.current = continueSend;
  }, [continueSend]);
  // Ref mirror of pendingSelection, read by the flush effect (the
  // stale-selection guard): a selection pending at flush time was drawn
  // for a different message — the flushed POST must never re-attach it.
  const pendingSelectionRef = useRef(pendingSelection);
  useEffect(() => {
    pendingSelectionRef.current = pendingSelection;
  }, [pendingSelection]);
  // The state key that changes when the .finally clears sendInFlightRef
  // (the pre-frame window closes). Refs do not trigger effects, so the
  // .finally increments it via `signalRunEnd` and the flush effect keys
  // on it (a message registered AFTER the terminal frame — the .finally
  // already fired — still gets a flush-effect re-run to pick it up).
  const [runEndCount, setRunEndCount] = useState(0);
  // The queued turn's id lives in a ref (not the queuedText state) so a
  // re-queue can update the existing queued turn's text in place WITHOUT
  // changing its id. React's reconciliation would drop the turn's DOM
  // node on an id change (the queued caption would vanish on the replace),
  // and the flush's setMessages filter (by id) would miss the replaced
  // turn. The ref is written when the queued turn is appended (new id)
  // and kept across replaces.
  const queuedTurnIdRef = useRef<string | null>(null);

  // The send entry point (issue #192 + #388): routes the message through
  // `continueSend` — OR fills the single composer queue slot when a run
  // is in flight (operator decision 1). Programmatic sends and
  // region-edit sends do NOT go through this path (decisions 2 and 3 —
  // their buttons are disabled, never queued).
  const handleSendMessage = useCallback(
    (text: string) => {
      const trimmed = text.trim();
      if (!trimmed) return;

      // Queue while a run is in flight or in the pre-first-frame window.
      // A second queue operation REPLACES the slot's text (the caption is
      // unchanged); the queued turn's id is stable across replaces (the
      // flush's setMessages filter removes it by id, and React's
      // reconciliation keeps the same DOM node — the caption never
      // vanishes on the replace).
      if (designLoopInFlight || sendInFlightRef.current) {
        flushedRef.current = false;
        setQueuedText(trimmed);
        setMessages((prev) => {
          const existing = prev.find((m) => m.queued);
          if (existing) {
            return prev.map((m) => (m.queued ? { ...m, content: trimmed } : m));
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

      // No run in flight: direct send.
      sendInFlightRef.current = true;
      if (projectId !== null) {
        continueSend(text);
        return;
      }
      void ensureProject()
        .then((id) => continueSend(text, id))
        .catch((e) => {
          // A failed project creation releases the pre-first-frame latch
          // so the next send retries (the original #192 single-flight
          // latch semantics).
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
    ],
  );

  // The run-end signal: the send's .finally clears `sendInFlightRef`
  // (synchronously) and then bumps `runEndCount` — the flush effect's
  // keys, so the flush re-runs with the CURRENT callback (the ref) after
  // the window closes.
  const signalRunEnd = useCallback(() => {
    sendInFlightRef.current = false;
    setRunEndCount((c) => c + 1);
  }, []);

  // The queue flush effect: when a run ends (the terminal frame clears
  // designLoopInFlight, or the stream's .finally closes the
  // pre-first-frame window), flush the queued message. The effect keys on
  // designLoopInFlight (the authoritative in-flight signal), runEndCount
  // (bumped by the .finally, so a message registered AFTER the terminal
  // frame still triggers a re-run), and queuedText (the queue itself).
  // The flushedRef flag keeps the flush idempotent: exactly one POST per
  // queued message, no matter how many terminal signals fire.
  //
  // The flush re-enters the send path through continueSendRef.current —
  // the ref always points at the NEWEST continueSend callback (one whose
  // closure holds the current `messages`, so the flushed POST's
  // chat_history includes the run's reply).
  //
  // Stale-selection guard: a pending selection is a pick the user drew
  // for a DIFFERENT message (the region-edit instruction they never
  // sent, or one sent alongside the queued text). The flushed POST must
  // never re-attach it (a plain `message` + `chat_history` is the
  // contract — the queued message is routed fresh, operator decision
  // 2026-10-05). The belt clear below covers the regression where the
  // selection survives to the flush; the queued text still flushes as a
  // plain chat message — it is never silently dropped.
  useEffect(() => {
    if (designLoopInFlight || sendInFlightRef.current) return; // run still in flight
    if (!queuedText || flushedRef.current) return;
    flushedRef.current = true;
    if (pendingSelectionRef.current !== null) {
      // The selection is stale for this message — drop it (the queued
      // text still flushes, as a plain chat message). The RegionEditBar's
      // disabled Submit (the primary gate, decision 3) makes this
      // regression-only in practice.
      clearPendingSelection();
    }
    const idToRemove = queuedTurnIdRef.current;
    queuedTurnIdRef.current = null;
    setQueuedText(null);
    setMessages((prev) => removeQueuedTurn(prev, idToRemove));
    continueSendRef.current(queuedText, undefined, true);
  }, [designLoopInFlight, runEndCount, queuedText, clearPendingSelection, setMessages]);

  // The queued message can be cancelled: the cancel control drops the
  // queued turn from the transcript and clears the queue slot WITHOUT
  // sending — when the run's terminal frame arrives, the flush effect
  // sees an empty queue and posts nothing. A fresh queue slot (a new
  // turn, a new id) is the next composer send.
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
