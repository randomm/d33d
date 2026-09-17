/**
 * PassCard — an assistant turn that produced a version (issue #116 shell).
 *
 * This is a PRESENTATIONAL SHELL: no surface markup exists in the
 * working tree to extract for it. The content — the six-view thumbnail
 * grid, the delta chips, the meta row and the SCAD disclosure — arrives
 * with the pass-card content ticket (W10), which will fill this file.
 * The typed props below are the contract that ticket builds against,
 * derived from the version-created progress frame (the `views` map) and
 * the shared `RenderImage` type App already holds.
 *
 * Per issue #116: a named component with no existing markup gets the
 * minimal typed shell, no invented surface, no copy.
 */

import type { RenderImage } from "../../lib/renderImage";

interface PassCardProps {
  /** The version id this pass produced (null until it exists). */
  versionId: number | null;
  /** The render view images that arrived on this pass (up to six). */
  views: RenderImage[];
}

export function PassCard({ versionId, views }: PassCardProps) {
  return (
    <div
      className="pass-card"
      data-testid="pass-card"
      data-views={views.length}
      data-version={versionId}
    >
      <span className="pass-card-version" data-testid="pass-card-version">
        {versionId !== null ? `v${versionId}` : ""}
      </span>
      {views.map((r) => (
        <img
          key={r.filename}
          src={r.src}
          alt={r.filename}
          className="pass-card-view"
          data-testid={`pass-card-view-${r.filename}`}
        />
      ))}
    </div>
  );
}
