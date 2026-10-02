/**
 * FirstRun — the first-run screen (issue #128, W14; reworked for #334).
 *
 * The centre column a new project sees before any version exists: a headline,
 * the body, two equal cards ("Describe it" / "Start from a file"), and the
 * photo line under both. The file card is a drop target and a file picker
 * accepting .stl/.3mf; dropping a file anywhere on the window also works and
 * highlights the file card while dragging.
 *
 * The build plate is drawn to scale behind it (PlateBackdrop).
 * All strings come from copy.firstRun — nothing is inlined.
 */

import { useState, useRef, useEffect, type FormEvent, type DragEvent } from "react";
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
  /** Called when an STL/3MF file is chosen or dropped. */
  onPartFile?: (file: File) => void;
}

export function FirstRun({ onSend, onPhotoSelect, inFlight, onPartFile }: FirstRunProps) {
  const [draft, setDraft] = useState("");
  const [dragOver, setDragOver] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);

  const handleSubmit = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const trimmed = draft.trim();
    if (!trimmed) return;
    onSend(trimmed);
  };

  const handleFileSelect = (e: React.ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (file && onPartFile) onPartFile(file);
  };

  const handleDrop = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(false);
    const file = e.dataTransfer.files[0];
    if (file && onPartFile) onPartFile(file);
  };

  const handleDragOver = (e: DragEvent) => {
    e.preventDefault();
    setDragOver(true);
  };

  const handleDragLeave = () => setDragOver(false);

  // Window-wide drop: dropping an STL/3MF anywhere on the window routes it
  // to onPartFile (the spec: "dropping a file anywhere on the window also
  // works and highlights the file card while dragging"). A .png dropped on
  // the window is NOT a part (the file card accepts only .stl/.3mf).
  useEffect(() => {
    if (!onPartFile) return;

    const hasStlOr3mf = (dt: DataTransfer): boolean => {
      for (const f of dt.files) {
        const n = f.name.toLowerCase();
        if (n.endsWith(".stl") || n.endsWith(".3mf")) return true;
      }
      return false;
    };

    const onDragOver = (e: globalThis.DragEvent) => {
      if (e.dataTransfer && hasStlOr3mf(e.dataTransfer)) {
        e.preventDefault();
        e.dataTransfer.dropEffect = "copy";
        setDragOver(true);
      }
    };

    const onDrop = (e: globalThis.DragEvent) => {
      if (e.dataTransfer && hasStlOr3mf(e.dataTransfer)) {
        e.preventDefault();
        setDragOver(false);
        const file = e.dataTransfer.files[0];
        if (file) onPartFile(file);
      }
    };

    const onDragLeave = (e: globalThis.DragEvent) => {
      // Only clear when the drag actually leaves the window (not when it
      // moves between child elements).
      if (e.relatedTarget === null || (e.relatedTarget as Node | null) === document.documentElement) {
        setDragOver(false);
      }
    };

    document.addEventListener("dragover", onDragOver);
    document.addEventListener("drop", onDrop);
    document.addEventListener("dragleave", onDragLeave);
    return () => {
      document.removeEventListener("dragover", onDragOver);
      document.removeEventListener("drop", onDrop);
      document.removeEventListener("dragleave", onDragLeave);
    };
  }, [onPartFile]);

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
          width: "min(900px, 92vw)",
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

        {/* Two equal cards */}
        <div
          style={{
            width: "100%",
            display: "flex",
            gap: 20,
            alignItems: "stretch",
          }}
        >
          {/* Describe it card */}
          <section
            className="first-run-describe-card"
            data-testid="first-run-describe-card"
            style={{
              flex: "1 1 0",
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              gap: 14,
              padding: 20,
              boxSizing: "border-box",
              background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
              border: "1px solid var(--color-hairline)",
              borderRadius: "var(--radius)",
            }}
          >
            <h2
              className="first-run-describe-label"
              data-testid="first-run-describe-label"
              style={{ margin: 0, fontSize: "var(--font-size-base)", fontWeight: 500 }}
            >
              {copy.firstRun.describeLabel}
            </h2>
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
          </section>

          {/* Start from a file card — drop target */}
          <section
            className="first-run-file-card"
            data-testid="first-run-file-card"
            onDragOver={handleDragOver}
            onDragLeave={handleDragLeave}
            onDrop={handleDrop}
            style={{
              flex: "1 1 0",
              minWidth: 0,
              display: "flex",
              flexDirection: "column",
              gap: 14,
              padding: 20,
              boxSizing: "border-box",
              background: "color-mix(in srgb, var(--color-panel) 92%, transparent)",
              border: dragOver
                ? "1.5px dashed var(--color-live)"
                : "1px solid var(--color-hairline)",
              borderRadius: "var(--radius)",
              cursor: onPartFile ? "pointer" : "default",
              outline: dragOver ? "none" : undefined,
            }}
          >
            <h2
              className="first-run-file-label"
              data-testid="first-run-file-label"
              style={{ margin: 0, fontSize: "var(--font-size-base)", fontWeight: 500 }}
            >
              {copy.firstRun.fileLabel}
            </h2>
            <div
              className="first-run-file-drop"
              data-testid="first-run-file-drop"
              onClick={() => fileInputRef.current?.click()}
              style={{
                flexGrow: 1,
                minHeight: 132,
                display: "flex",
                flexDirection: "column",
                alignItems: "center",
                justifyContent: "center",
                gap: 10,
                border: "1.5px dashed var(--color-hairline)",
                borderRadius: "var(--radius-sm)",
                cursor: "pointer",
                color: "var(--color-fg-2)",
              }}
            >
              <span style={{ fontSize: "var(--font-size-base)" }}>{copy.firstRun.fileDropLine}</span>
              <span style={{ fontSize: "var(--font-size-xs)" }}>{copy.firstRun.fileChooseLine}</span>
            </div>
            <p
              className="first-run-file-caption"
              data-testid="first-run-file-caption"
              style={{ margin: 0, fontSize: "var(--font-size-xs)", color: "var(--color-fg-2)" }}
            >
              {copy.firstRun.fileCardCaption}
            </p>
            <input
              type="file"
              accept=".stl,.3mf"
              data-testid="first-run-file-input"
              ref={fileInputRef}
              onChange={handleFileSelect}
              aria-label="Import an STL or 3MF file"
              style={{ display: "none" }}
            />
          </section>
        </div>

        {/* Photo line under both cards: the "Add a photo" button (the same
            photo path as the left pane — the label[htmlfor] click routes to
            the frozen photo-file-input) plus its hint line. */}
        <button
          type="button"
          className="first-run-photo-btn"
          data-testid="first-run-photo-btn"
          onClick={onPhotoSelect}
          disabled={inFlight}
          style={{
            alignSelf: "center",
            padding: "8px 16px",
            border: "1px solid var(--color-hairline)",
            borderRadius: "var(--radius-sm)",
            background: "transparent",
            color: "var(--color-fg-2)",
            cursor: inFlight ? "not-allowed" : "pointer",
          }}
        >
          {copy.firstRun.photoBtn}
        </button>
        <p
          className="first-run-photo-hint"
          data-testid="first-run-photo-hint"
          style={{
            margin: "0",
            color: "var(--color-faint)",
            fontSize: "var(--font-size-xs)",
            textAlign: "center",
            maxWidth: 620,
            alignSelf: "center",
          }}
        >
          {copy.firstRun.photoLine}
        </p>
      </div>
    </div>
  );
}
