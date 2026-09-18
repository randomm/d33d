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

import { useRef, useEffect, useState } from "react";
import type { RenderImage } from "../../lib/renderImage";
import type { RegionEditViewId } from "../../lib/api";
import { MARKER_COLOR } from "../../lib/marker";
import { Composer } from "./Composer";
import { PassCard } from "./PassCard";
import type { DisplayError } from "../../lib/errorMapping";
import { FailureTurn } from "../failure/FailureTurn";

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
}

interface ChatPanelProps {
  messages: ChatMessage[];
  onSend: (text: string) => void;
  /** True while a design loop is in flight — disables the send button. */
  inFlight?: boolean;
  /** The PassCard's enlarged-view close action (issue #125). */
  onBesidePhoto?: () => void;
  /** The build envelope (API-reported) for the failure turn's measured
   *  number (issue #124). Absent → no bars, no numbers. */
  envelope?: { x: number; y: number; z: number } | null;
  /** The version label the failure turn says survived (the latest
   *  version — the failure was never a version). */
  keptVersion?: string | null;
}

export function ChatPanel({
  messages,
  onSend,
  inFlight,
  onBesidePhoto,
  envelope,
  keptVersion,
}: ChatPanelProps) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages]);

  const handleSubmit = (text: string) => {
    onSend(text);
    setInput("");
  };

  return (
    <section className="chat-panel" data-testid="chat-panel">
      <div className="chat-messages" role="log" aria-label="Conversation">
        {messages.map((msg) => {
          // An assistant turn that produced a version is a PassCard;
          // everything else (user turns, clarifying questions, the
          // in-flight streaming turn) stays a plain sentence in the flow.
          const isPass = msg.role === "assistant" && msg.versionId !== undefined;
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
                  keptVersion={keptVersion}
                  inFlight={inFlight === true}
                  onAction={onSend}
                />
              ) : isPass ? (
                <PassCard
                  versionId={msg.versionId ?? null}
                  views={msg.views ?? []}
                  summary={msg.content}
                  source={msg.source}
                  onBesidePhoto={onBesidePhoto}
                />
              ) : (
                <span className="chat-msg-content">{msg.content}</span>
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

        <div ref={bottomRef} />
      </div>

      <Composer
        value={input}
        onChange={setInput}
        onSend={handleSubmit}
        inFlight={inFlight}
      />
    </section>
  );
}
