/**
 * Composer — the chat input form (the message box + Send).
 *
 * Presentational: driven by props (value / onChange / inFlight) with
 * onSend fired on submit. Extracted verbatim from the input form at the
 * bottom of ChatPanel.tsx (issue #116 — surface extraction); ChatPanel
 * now renders this instead of its inline form, so the `chat-input` /
 * `chat-send-btn` markup lives in exactly one file.
 */

import type { FormEvent } from "react";

interface ComposerProps {
  /** The current input value. */
  value: string;
  /** Called with the next value as the user types. */
  onChange: (value: string) => void;
  /** Called with the trimmed text when the form is submitted (only
   *  non-empty values — whitespace-only drafts never fire it). */
  onSend: (text: string) => void;
  /** True while a design loop is in flight — disables the Send button. */
  inFlight?: boolean;
  /** True → the whole form is not rendered (issue #193 — the first-run
   *  screen carries its own composer, so the chat's must be absent, not
   *  hidden-with-CSS: the screen must hold exactly one composer). */
  hidden?: boolean;
}

export function Composer({ value, onChange, onSend, inFlight: _inFlight, hidden }: ComposerProps) {
  if (hidden) return null;

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = value.trim();
    if (!trimmed) return;
    onSend(trimmed);
  };

  return (
    <form className="chat-input-form" onSubmit={handleSubmit}>
      <input
        type="text"
        className="chat-input"
        data-testid="chat-input"
        placeholder="Type a message…"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        aria-label="Chat message input"
      />
      <button
        type="submit"
        className="chat-send-btn"
        data-testid="chat-send-btn"
        // Issue #388: the Send button is NOT disabled while a run is in
        // flight — it queues (the queued caption appears). The `inFlight`
        // prop is no longer used for the disabled check; it is retained
        // for the visual dimming the parent may apply.
        disabled={!value.trim()}
      >
        Send
      </button>
    </form>
  );
}
