/**
 * PassCard — an assistant turn that produced a version (issue #116 shell,
 * filled by issue #125 — packet item W10).
 *
 * Renders, in order: the summary prose (UI face), the view thumbnails as
 * a 3x2 grid captioned by view label, the partial-views line when fewer
 * than six arrived, and the collapsed source disclosure (mono,
 * line-counted). The SCAD source is DISCLOSURE CONTENT — it arrives on
 * the token frame and must never appear as chat message text (design
 * contract, W10).
 *
 * Opening a thumbnail enlarges it in place and offers a "Close this
 * view" action. The enlarged render is what the model is compared
 * against; the views are PNG thumbnails (the frame carries no geometry),
 * so the button closes the enlargement and the streamed model (mounted
 * on the version-created frame's stl_data_uri) simply remains beside the
 * photo in the stage — it was there the whole time. The label was
 * formerly "Beside the photo", which advertised a placement the PNG
 * cannot perform: one click showed nothing and the audience — makers
 * who check every control — learned not to trust the next one.
 * A ticket that carries view geometry on the frame can turn the action
 * into a real swap and relabel it accordingly. Delta chips and the elapsed-time meta row are not rendered:
 * the version-created frame carries no previous dimensions, no elapsed
 * time and no changed-line data, and fabricating them is the house
 * anti-pattern (see the W10 comment on issue #125). They land with
 * whatever ticket extends the frame.
 *
 * The view filenames follow the render worker's fixed VIEWS contract
 * ("view_00_front.png" ... "view_05_iso.png"); the caption label is
 * derived from the filename stem — never from the index, so a reordered
 * or partial set stays honest.
 */

import { useState } from "react";
import type { RenderImage } from "../../lib/renderImage";
import copy from "../../copy";

/** Map a view filename ("view_00_front.png") to its caption label.
 *  Unknown stems fall back to the bare stem — never an index. */
function viewLabel(filename: string): string {
  const stem = filename.replace(/^view_\d+_/, "").replace(/\.png$/, "");
  const idx = copy.passCard.viewLabels.findIndex(
    (l) => l.toLowerCase() === stem.toLowerCase(),
  );
  return idx >= 0 ? copy.passCard.viewLabels[idx] : stem;
}

interface PassCardProps {
  /** The version id this pass produced (null until it exists). */
  versionId: number | null;
  /** The render view images that arrived on this pass (up to six). */
  views: RenderImage[];
  /** The pass's summary prose (UI face, no dimensions). */
  summary?: string;
  /** The generated source (the disclosure's content — never chat text). */
  source?: string;
  /** The enlarged view's close action (issue #125). */
  onBesidePhoto?: () => void;
}

const TOTAL_VIEWS = copy.passCard.viewLabels.length;

export function PassCard({
  versionId,
  views,
  summary,
  source,
  onBesidePhoto,
}: PassCardProps) {
  const [enlarged, setEnlarged] = useState<string | null>(null);
  const [sourceOpen, setSourceOpen] = useState(false);

  const sourceLines = source ? source.split("\n").length : 0;

  return (
    <div
      className="pass-card"
      data-testid="pass-card"
      data-views={views.length}
      data-version={versionId}
    >
      {versionId !== null && (
        <span className="pass-card-version" data-testid="pass-card-version">
          {`v${versionId}`}
        </span>
      )}
      {summary && (
        <span className="pass-card-summary" data-testid="pass-card-summary">
          {summary}
        </span>
      )}
      {views.length > 0 && (
        <div className="pass-card-views" data-testid="pass-card-views">
          {views.map((r) => (
            <figure key={r.filename} className="pass-card-view-figure">
              <button
                type="button"
                className={`pass-card-view-button${enlarged === r.filename ? " pass-card-view-button--enlarged" : ""}`}
                aria-label={viewLabel(r.filename)}
                aria-pressed={enlarged === r.filename}
                data-testid={`pass-card-view-${r.filename}`}
                onClick={() =>
                  setEnlarged((cur) => (cur === r.filename ? null : r.filename))
                }
              >
                <img src={r.src} alt={viewLabel(r.filename)} className="pass-card-view" />
              </button>
              <figcaption className="pass-card-view-label">
                {viewLabel(r.filename)}
              </figcaption>
            </figure>
          ))}
        </div>
      )}
      {views.length > 0 && views.length < TOTAL_VIEWS && (
        <span className="pass-card-partial" data-testid="pass-card-partial">
          {copy.passCard.partialViews(views.length, TOTAL_VIEWS)}
        </span>
      )}
      {source !== undefined && (
        <>
          <button
            type="button"
            className="pass-card-source-toggle"
            data-testid="pass-card-source-toggle"
            aria-expanded={sourceOpen}
            onClick={() => setSourceOpen((o) => !o)}
          >
            {sourceOpen
              ? copy.passCard.closeDisclosure
              : copy.passCard.sourceDisclosure(sourceLines)}
          </button>
          {sourceOpen && (
            <pre className="pass-card-source" data-testid="pass-card-source">
              {source}
            </pre>
          )}
        </>
      )}
      {enlarged !== null &&
        (() => {
          const v = views.find((r) => r.filename === enlarged);
          if (!v) return null;
          return (
            <div className="pass-card-enlarged" data-testid="pass-card-enlarged">
              <img
                src={v.src}
                alt={viewLabel(v.filename)}
                className="pass-card-view pass-card-view-enlarged"
              />
              <button
                type="button"
                className="pass-card-action"
                data-testid="pass-card-beside-photo"
                onClick={() => {
                  onBesidePhoto?.();
                  setEnlarged(null);
                }}
              >
                {copy.passCard.viewActions.closeView}
              </button>
            </div>
          );
        })()}
    </div>
  );
}
