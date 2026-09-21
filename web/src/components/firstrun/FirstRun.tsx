/**
 * FirstRun — the first-run screen (issue #128, W14).
 *
 * The centre column a new project sees before any version exists: a headline,
 * the millimetre contract stated once, one large input (the deck's example as
 * its placeholder), a photo button, Start, four complete starter sentences
 * (the starters teach the register — they are complete, specific sentences,
 * not keywords), and a one-line photo hint that names the hard part.
 *
 * The build plate is drawn to scale behind it (PlateBackdrop) — the
 * constraint arrives as a room, not a warning. No model and no placeholder
 * geometry ever renders here; the empty canvas IS the correct empty state.
 *
 * All strings come from copy.firstRun — nothing is inlined.
 */

import { useState, type FormEvent } from "react";
import copy from "../../copy";

interface FirstRunProps {
  /** Called with the trimmed text when the form submits (Start, or a
   *  starter). Whitespace-only and empty values never fire it. */
  onSend: (text: string) => void;
  /** Called when the photo button is pressed (the parent owns the file
   *  picker — the same photo path as the left pane). */
  onPhotoSelect: () => void;
  /** True while a design loop is in flight — the controls disable. */
  inFlight?: boolean;
}

export function FirstRun({ onSend, onPhotoSelect, inFlight }: FirstRunProps) {
  const [draft, setDraft] = useState("");

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed) return;
    onSend(trimmed);
  };

  return (
    <div
      className="first-run"
      data-testid="first-run"
      style={{
        position: "absolute",
        inset: 0,
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 10,
        pointerEvents: "none",
      }}
    >
      <div
        style={{
          display: "flex",
          flexDirection: "column",
          alignItems: "stretch",
          gap: 16,
          width: "min(560px, 92vw)",
          maxWidth: "100%",
          maxHeight: "100%",
          overflow: "visible",
          boxSizing: "border-box",
          padding: 24,
          borderRadius: "var(--radius)",
          background: "color-mix(in srgb, var(--color-panel) 40%, transparent)",
          border: "1px solid var(--color-hairline)",
          color: "var(--color-fg)",
          pointerEvents: "auto",
        }}
      >
        <h1
          className="first-run-headline"
          data-testid="first-run-headline"
          style={{
            margin: 0,
            fontSize: "var(--font-size-lg)",
            fontWeight: 500,
            textAlign: "center",
          }}
        >
          {copy.firstRun.headline}
        </h1>
        <p
          className="first-run-body"
          data-testid="first-run-body"
          style={{
            margin: 0,
            color: "var(--color-fg-2)",
            textAlign: "center",
          }}
        >
          {copy.firstRun.body}
        </p>

        <form className="first-run-input-form" onSubmit={handleSubmit}>
          <input
            type="text"
            className="first-run-input"
            data-testid="first-run-input"
            placeholder={copy.firstRun.placeholder}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            aria-label={copy.firstRun.headline}
            style={{
              width: "100%",
              padding: "12px 16px",
              fontSize: "var(--font-size-base)",
              color: "var(--color-fg)",
              background: "var(--color-recess)",
              border: "1px solid var(--color-hairline)",
              borderRadius: "var(--radius-sm)",
              boxSizing: "border-box",
            }}
          />
          <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
            <button
              type="button"
              className="first-run-photo-btn"
              data-testid="first-run-photo-btn"
              onClick={onPhotoSelect}
              disabled={inFlight}
              style={{
                padding: "8px 16px",
                border: "1px solid var(--color-hairline)",
                borderRadius: "var(--radius-sm)",
                background: "transparent",
                color: "var(--color-fg-2)",
                cursor: inFlight ? "not-allowed" : "pointer",
              }}
            >
              {copy.shell.addPhoto}
            </button>
            <button
              type="submit"
              className="first-run-start-btn"
              data-testid="first-run-start-btn"
              disabled={inFlight || draft.trim().length === 0}
              style={{
                padding: "8px 20px",
                border: "none",
                borderRadius: "var(--radius-sm)",
                background: "var(--color-live)",
                color: "var(--color-canvas)",
                fontWeight: 500,
                cursor: inFlight || draft.trim().length === 0 ? "not-allowed" : "pointer",
              }}
            >
              {copy.firstRun.start}
            </button>
          </div>
        </form>

        <div
          style={{
            width: "100%",
            display: "flex",
            flexDirection: "column",
            gap: 6,
            marginTop: 8,
          }}
        >
          <span
            className="first-run-starters-label"
            data-testid="first-run-starters-label"
            style={{
              color: "var(--color-muted)",
              fontSize: "var(--font-size-xs)",
            }}
          >
            {copy.firstRun.startersLabel}
          </span>
          {copy.firstRun.starters.map((starter) => (
            <button
              key={starter}
              type="button"
              className="first-run-starter"
              data-testid="first-run-starter"
              onClick={() => onSend(starter)}
              disabled={inFlight}
              style={{
                textAlign: "left",
                padding: "8px 12px",
                border: "1px solid var(--color-hairline)",
                borderRadius: "var(--radius-sm)",
                background: "var(--color-recess)",
                color: "var(--color-fg-2)",
                cursor: inFlight ? "not-allowed" : "pointer",
              }}
            >
              {starter}
            </button>
          ))}
        </div>

        <p
          className="first-run-photo-hint"
          data-testid="first-run-photo-hint"
          style={{
            margin: 0,
            color: "var(--color-faint)",
            fontSize: "var(--font-size-xs)",
            textAlign: "center",
            maxWidth: 420,
            alignSelf: "center",
          }}
        >
          {copy.firstRun.photoHint}
        </p>
      </div>
    </div>
  );
}
