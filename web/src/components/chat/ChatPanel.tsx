/**
 * ChatPanel — the operator-facing chat surface (left pane).
 *
 * Displays the conversation transcript, inline render images, and an input
 * area for the next message. The panel is a pure presentational component
 * driven by props; the parent (App) owns the message state.
 *
 * Design notes from 05-spa-core.md:
 * - Inline render images appear in the chat turn where they arrive (not in
 *   a separate gallery).
 * - SSE token streaming appends to the active assistant message.
 * - No auto-generated slider/parameter panel — input is text + photo only.
 */

import { useRef, useEffect, useState, type FormEvent } from "react";
import type { RenderImage } from "../../App";

export interface ChatMessage {
  id: string;
  role: "user" | "assistant" | "system";
  content: string;
  /** True while streaming tokens are being appended */
  streaming?: boolean;
}

interface ChatPanelProps {
  messages: ChatMessage[];
  onSend: (text: string) => void;
  /** Render images to display inline (arrived over SSE). */
  renders: RenderImage[];
}

export function ChatPanel({ messages, onSend, renders }: ChatPanelProps) {
  const [input, setInput] = useState("");
  const bottomRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    bottomRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, renders]);

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = input.trim();
    if (!trimmed) return;
    onSend(trimmed);
    setInput("");
  };

  return (
    <section className="chat-panel" data-testid="chat-panel">
      <div className="chat-messages" role="log" aria-label="Conversation">
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`chat-msg chat-msg--${msg.role}`}
            data-testid={`chat-msg-${msg.role}`}
          >
            <span className="chat-msg-content">{msg.content}</span>
            {msg.streaming && (
              <span className="streaming-cursor" data-testid="streaming-cursor">
                ▌
              </span>
            )}
          </div>
        ))}

        {/* Inline render images — appear in chat turn order */}
        {renders.length > 0 && (
          <div className="chat-renders" data-testid="chat-renders">
            {renders.map((r) => (
              <img
                key={r.filename}
                src={r.src}
                alt={r.filename}
                className="chat-render-img"
                data-testid={`render-img-${r.filename}`}
              />
            ))}
          </div>
        )}

        <div ref={bottomRef} />
      </div>

      <form className="chat-input-form" onSubmit={handleSubmit}>
        <input
          type="text"
          className="chat-input"
          data-testid="chat-input"
          placeholder="Type a message…"
          value={input}
          onChange={(e) => setInput(e.target.value)}
          aria-label="Chat message input"
        />
        <button
          type="submit"
          className="chat-send-btn"
          data-testid="chat-send-btn"
          disabled={!input.trim()}
        >
          Send
        </button>
      </form>
    </section>
  );
}
