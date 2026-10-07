/**
 * ChatPanel — the operator-facing chat surface (left pane).
 *
 * Displays the conversation transcript, and an input area for the next
 * message. The panel is a pure presentational component driven by props;
 * the parent (App) owns the message state.
 *
 * Design notes:
 * - An assistant turn that produced a version renders as a PassCard:
 *   summary prose, the view thumbnails, and the source disclosure —
 *   the SCAD source arrives on the token frame and is disclosure
 *   content, never chat text (issue #125, W10).
 * - Clarifying questions stay plain sentences in the flow.
 * - SSE token streaming appends to the active assistant message.
 * - No auto-generated slider/parameter panel — input is text + photo only.
 */

import { useRef, useEffect, useState, useMemo } from "react";
import type { RenderImage } from "../../lib/renderImage";
import { versionOrdinals } from "../../lib/versionOrdinals";
import type { RegionEditViewId, VersionTimelineEntry } from "../../lib/api";
import { MARKER_COLOR } from "../../lib/marker";
import { Composer } from "./Composer";
import { PassCard } from "./PassCard";
import { FLEX_FILL } from "./flexFill";
import type { DisplayError } from "../../lib/errorMapping";
import { FailureTurn } from "../failure/FailureTurn";
import copy from "../../copy";

// The marker colour's single home is lib/marker.ts (issue #110); the name
// is re-exported here for existing consumers.
export { MARKER_COLOR };

/** A persisted region selection attached to a chat message (06-region-
 *  selection.md UX: "selection persists as a thumbnail on the chat
 *  message so scrolling back shows what 'this bit' meant"). */
export interface ChatMessageSelection {
  /** The composited red-marked view PNG, as a data URL or relative URL. */
  thumbnail: string;
  /** Which of the six views the selection was drawn on. */
  viewId: RegionEditViewId;
  /** Ranked module identifiers the selection resolved to (top-most first). */
  moduleIds: string[];
}

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  /** True while streaming tokens are being appended */
  streaming?: boolean;
  /** Region selection this message was sent with, if any. Persists with
   *  the message so scrolling back shows what "this bit" meant. */
  selection?: ChatMessageSelection;
  /** The version id this turn produced — set when the version-created
   *  frame arrives (issue #125). An assistant message with this set
   *  renders as a PassCard. */
  versionId?: number | null;
  /** The render view images this turn produced (up to six). */
  views?: RenderImage[];
  /** The generated source for this turn's disclosure. Arrives on the
   *  token frame; disclosure content, never chat text (W10). */
  source?: string;
  /** Present ONLY on a failure turn (issue #124, W12): the failure is a
   *  turn in the conversation, not a card beside it. A message with this
   *  set renders as a FailureTurn. */
  failure?: DisplayError;
  /** Issue #390 (operator decision 2026-10-05): the version label that
   *  survived, FROZEN AT THE MOMENT THE FAILURE CARD IS WRITTEN. Present
   *  only on a failure turn — the failure turn's survivor line renders
   *  from this value, never from the live latest-version in the timeline
   *  (the pre-fix bug: a turn-1 card written before any version existed
   *  later read the label of a version made in turn 2). A later run that
   *  creates a new version MUST NOT re-word this turn. The value is the
   *  same label the shell computes from the timeline at write time; when
   *  the timeline is empty the field is absent and the line is not
   *  rendered (no invented survivor). */
  keptVersion?: string;
  /** Issue #250: present when this plain assistant message is an assumed-value
   *  confirmation acknowledgement ("Got it — {label} stays {value}.").
   *  The rendered content is split into label and value spans so the value
   *  (and the identifier-fallback label) render in the mono face, per the
   *  project rule that measurements render in mono and prose in the UI face.
   *  The offer message itself ("I assumed {value} for {label}…") renders
   *  as an ordinary plain message — no marker, no extra fields. */
  confirmAck?: { label: string; value: string };
  /** Issue #338 (decision 7): present when this plain assistant message is
   *  a fill-and-recut offer (the region-edit or chat-route trigger fired).
   *  The message renders the boundary sentence + the [Yes, do that] / [Leave
   *  it] buttons. `pending` is true while the offer is still the server's
   *  pending offer (buttons enabled); false after either button is pressed
   *  or the offer is no longer pending (buttons disabled). */
  fillRecutOffer?: { pending: boolean };
  /** Issue #388: true on a QUEUED user turn — the message the composer sent
   *  while a design run was in flight (or in the pre-first-frame window).
   *  It is NOT a real user message yet: it sits in the transcript with a
   *  caption and is POSTed once, by the flush, when the run's terminal
   *  frame arrives. At most one such turn exists at a time (a second
   *  composer send REPLACES its text, not appends a second slot). When the
   *  flush's send REJECTS (a 409, a network error) the turn is restored
   *  with `queuedFailure` set — it renders as a failed queued turn (reason
   *  + resend), never as a sent message (issue #388: the queued message
   *  must not vanish). */
  queued?: boolean;
  /** Issue #388 (failed flush): the reason the queued turn's flush send
   *  rejected. Present only on a restored queued turn — it renders the
   *  `queued.flushFailed` reason and a resend action. The message is never
   *  shown as sent, never lost. */
  queuedFailure?: string;
}

interface ChatPanelProps {
  messages: ChatMessage[];
  onSend: (text: string) => void;
  /** Issue #388 (operator decision 2026-10-05): drop the queued turn
   *  without sending it. Absent (or a no-op) when nothing is queued. */
  onCancelQueued?: () => void;
  /** Issue #388 (failed flush): the resend control's handler — the queued
   *  text re-enters the normal send path (a fresh chat send, never a
   *  re-flush). Supplied by the shell from the hook's exported
   *  `resendQueued`; the handler is NOT carried on the message. */
  onResendQueued?: (text: string) => void;
  /** True while a design loop is in flight — disables the send button. */
  inFlight?: boolean;
  /** The PassCard's enlarged-view close action (issue #125). */
  onBesidePhoto?: () => void;
  /** Issue #387: the version timeline (oldest first). The panel derives
   *  each PassCard's user-facing label ("vN", the timeline ordinal) from
   *  this list via the shared `versionOrdinals` helper — the same map the
   *  Filmstrip uses, so the card and the strip cannot drift. An id not in
   *  the list (stale — the version-created frame arrived before the
   *  timeline refetch landed) yields no number, never the raw id. */
  versions?: VersionTimelineEntry[];
  /** The build envelope (API-reported) for the failure turn's measured
   *  number (issue #124). Absent → no bars, no numbers. */
  envelope?: { x: number; y: number; z: number } | null;
  /** Issue #352 (operator decision 4): false when the export button is
   *  actually disabled (the part's units are unsettled or a design pass
   *  is in flight) — the failure turn's survived line then omits the
   *  "still exportable" claim. Absent defaults to true (the pre-#352
   *  behaviour). */
  exportable?: boolean;
  /** Issue #193: hide the composer while the first-run screen is up
   *  (the first-run screen carries its own composer, so exactly one
   *  must be visible). Defaults to false for direct-render tests. */
  hideComposer?: boolean;
}

export function ChatPanel({
  messages,
  onSend,
  onCancelQueued,
  onResendQueued,
  inFlight,
  onBesidePhoto,
  versions,
  envelope,
  // Issue #390: there is no panel-level `keptVersion` — each failure
  // message carries its own frozen label (`ChatMessage.keptVersion`),
  // and the live timeline value must not reach the failure turn.
  exportable,
  hideComposer,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const messagesRef = useRef<HTMLDivElement>(null);
  const ordinalById = useMemo(() => versionOrdinals(versions ?? []), [versions]);

  // The transcript is the DELIVERED sole scroll container (issue #220,
  // adversarial round 1): the ticket's text names the pane, but the pane's
  // overflowY:auto is intentionally left inert (never overflow — the
  // transcript below is the element that actually scrolls). Scrolling the
  // transcript directly (instead of scrollIntoView, which walks up to the
  // nearest scrollable ancestor) keeps auto-scroll deterministic and
  // independent of the composer/upload that now sit as pinned siblings
  // below it.
  // Invariant relied on: every App.tsx mutation that GROWS the transcript
  // (token / progress / done frames) must produce a NEW messages array
  // reference so this effect re-fires on content growth, not just on
  // array-identity change. Batching token updates into a single final
  // setMessages would silently stop the scroll-to-bottom behaviour.
  useEffect(() => {
    const el = messagesRef.current;
    // Null-ref guard: the ref can be null before the first commit, and the
    // method is absent in minimal DOM stubs (this is a null/method guard,
    // not a jsdom guard — jsdom implements scrollTo as a no-op).
    if (el && typeof el.scrollTo === "function") {
      el.scrollTo({ top: el.scrollHeight, behavior: "smooth" });
    }
  }, [messages]);

  const handleSubmit = (text: string) => {
    onSend(text);
    setInput("");
  };

  return (
    <section
      className="chat-panel"
      data-testid="chat-panel"
      style={FLEX_FILL}
    >
      <div
        ref={messagesRef}
        className="chat-messages"
        role="log"
        aria-label="Conversation"
        style={{ ...FLEX_FILL, overflowY: "auto" }}
      >
        {messages.map((msg) => {
          // An assistant turn that produced a version is a PassCard;
          // everything else (user turns, clarifying questions, the
          // in-flight streaming turn) stays a plain sentence in the flow.
          const isPass = msg.role === "assistant" && msg.versionId !== undefined;
          const vid = msg.versionId ?? null;
          const isFailure = msg.failure !== undefined;
          return (
            <div
              key={msg.id}
              className={`chat-msg chat-msg--${msg.role}`}
              data-testid={`chat-msg-${msg.role}`}
            >
              {isFailure ? (
                <FailureTurn
                  error={msg.failure!}
                  envelope={envelope}
                  // Issue #390: the survivor line renders from the label
                  // FROZEN ON THE MESSAGE (captured at card-write time) —
                  // never from the panel-level `keptVersion`, which tracks
                  // the live timeline and would re-word older cards when a
                  // later turn makes a new version.
                  keptVersion={msg.keptVersion}
                  exportable={exportable}
                  inFlight={inFlight === true}
                  onAction={onSend}
                />
              ) : isPass ? (
                <PassCard
                  versionId={vid}
                  versionOrdinal={vid === null ? null : ordinalById.get(vid) ?? null}
                  views={msg.views ?? []}
                  summary={msg.content}
                  source={msg.source}
                  onBesidePhoto={onBesidePhoto}
                />
              ) : msg.confirmAck ? (
                // Issue #250: the confirmation acknowledgement is a plain
                // message (no form, no buttons) whose measured value renders
                // in the mono face. The label may be a raw SCAD identifier
                // (the model-supplied label's fallback) — it renders in the
                // mono face too, so the user can tell it from a human label
                // at a glance (the same rule as the Brief's rows).
                <span className="chat-msg-content" data-testid="confirm-ack-msg">
                  <span className="chat-msg-confirm-label">Got it — {msg.confirmAck.label} stays </span>
                  <span
                    className="chat-msg-confirm-value"
                    style={{ fontFamily: "var(--font-mono)" }}
                  >
                    {msg.confirmAck.value}.
                  </span>
                </span>
              ) : msg.fillRecutOffer ? (
                // Issue #338 (decision 7): the fill-and-recut offer renders
                // the boundary sentence + the [Yes, do that] / [Leave it]
                // buttons. Yes sends the acceptance through the existing
                // chat offer path (runs the loop); Leave it sends the
                // decline (clears the pending offer, no loop). Both buttons
                // disable after either is pressed, or once the offer is no
                // longer pending.
                <span className="chat-msg-content" data-testid="fill-recut-offer-msg">
                  <span className="chat-msg-offer-sentence">{msg.content}</span>
                  <span className="chat-msg-offer-buttons" data-testid="fill-recut-offer-buttons">
                    <button
                      type="button"
                      data-testid="fill-recut-offer-yes"
                      className="fill-recut-offer-btn fill-recut-offer-btn--yes"
                      disabled={!msg.fillRecutOffer.pending || inFlight === true}
                      onClick={() => onSend(copy.fillRecut.offerYes)}
                      style={{
                        cursor:
                          msg.fillRecutOffer.pending && inFlight !== true
                            ? "pointer"
                            : "not-allowed",
                        opacity:
                          msg.fillRecutOffer.pending && inFlight !== true ? 1 : 0.5,
                      }}
                    >
                      {copy.fillRecut.offerYes}
                    </button>
                    <button
                      type="button"
                      data-testid="fill-recut-offer-no"
                      className="fill-recut-offer-btn fill-recut-offer-btn--no"
                      disabled={!msg.fillRecutOffer.pending || inFlight === true}
                      onClick={() => onSend(copy.fillRecut.offerNo)}
                      style={{
                        cursor:
                          msg.fillRecutOffer.pending && inFlight !== true
                            ? "pointer"
                            : "not-allowed",
                        opacity:
                          msg.fillRecutOffer.pending && inFlight !== true ? 1 : 0.5,
                      }}
                    >
                      {copy.fillRecut.offerNo}
                    </button>
                  </span>
                </span>
              ) : (
                <span className="chat-msg-content">{msg.content}</span>
              )}
              {msg.queued && (
                <span className="chat-msg-queued-actions">
                  <span
                    className="chat-msg-queued-caption"
                    data-testid={
                      msg.queuedFailure ? "queued-flush-failed-reason" : "queued-caption"
                    }
                  >
                    {msg.queuedFailure ? copy.queued.flushFailed : copy.queued.caption}
                  </span>
                  {msg.queuedFailure ? (
                    // The flush's send rejected: the message is restored, not
                    // lost. Resend goes through the NORMAL send path (a fresh
                    // chat send — never a re-flush); the hook removes the
                    // failed flag when the message leaves the failed state.
                    <button
                      type="button"
                      className="chat-msg-queued-resend"
                      data-testid="queued-resend-btn"
                      onClick={() => onResendQueued?.(msg.content)}
                      style={{
                        marginLeft: 8,
                        padding: "2px 8px",
                        border: "1px solid var(--color-blocked)",
                        borderRadius: 4,
                        background: "transparent",
                        color: "var(--color-blocked)",
                        cursor: "pointer",
                        fontSize: 11,
                      }}
                    >
                      {copy.queued.resend}
                    </button>
                  ) : (
                    <button
                      type="button"
                      className="chat-msg-queued-cancel"
                      data-testid="queued-cancel-btn"
                      aria-label={copy.queued.cancel}
                      onClick={onCancelQueued}
                      style={{
                        marginLeft: 8,
                        padding: "2px 8px",
                        border: "1px solid var(--color-hairline)",
                        borderRadius: 4,
                        background: "transparent",
                        color: "var(--color-fg-2)",
                        cursor: "pointer",
                        fontSize: 11,
                      }}
                    >
                      {copy.queued.cancel}
                    </button>
                  )}
                </span>
              )}
              {msg.selection && (
                <img
                  src={msg.selection.thumbnail}
                  alt={`selection on ${msg.selection.viewId}`}
                  className="chat-selection-thumbnail"
                  data-testid={`selection-thumbnail-${msg.id}`}
                  style={{ borderColor: MARKER_COLOR }}
                />
              )}
              {msg.streaming && (
                <span className="streaming-cursor" data-testid="streaming-cursor">
                  ▌
                </span>
              )}
            </div>
          );
        })}

      </div>

      <Composer
        value={input}
        onChange={setInput}
        onSend={handleSubmit}
        hidden={hideComposer}
      />
    </section>
  );
}
