/**
 * Filmstrip — the version-tail overlay (right edge): the timeline rail
 * plus the compare pane and the pinned-variants gallery.
 *
 * Presentational: the parent (App) owns the version state, the
 * compare selection and its fetch, and the timeline callbacks. Extracted
 * verbatim from App.tsx's inline `version-timeline-pane` (issue #116 —
 * surface extraction). The per-entry horizontal film strip arrives with
 * the filmstrip content ticket (W13), which will rework this surface.
 */

import type { VersionCompare, VersionTimelineEntry } from "../../lib/api";
import { VersionTimeline } from "../versions/VersionTimeline";
import { VariantGallery } from "../versions/VariantGallery";
import { CompareView } from "../versions/CompareView";

interface FilmstripProps {
  /** The version timeline entries (the project, oldest first). */
  versions: VersionTimelineEntry[];
  /** The compare selection: [aId, bId] or null when no compare is open. */
  compareIds: [number, number] | null;
  /** The compare fetch result (null while none / loading). */
  compareResult: VersionCompare | null;
  /** The compare fetch failure message (null when none). */
  compareError: string | null;
  /** The top/right inset from the stage edge (px). */
  inset: number;
  /** Called with the version id to restore (a new forward version). */
  onRestore: (versionId: number) => void;
  /** Called with (versionId, shouldPin). */
  onPin: (versionId: number, pinned: boolean) => void;
  /** Called with the version id (compare-select). */
  onCompareSelect: (versionId: number) => void;
}

export function Filmstrip({
  versions,
  compareIds,
  compareResult,
  compareError,
  inset,
  onRestore,
  onPin,
  onCompareSelect,
}: FilmstripProps) {
  return (
    <div
      className="version-tail-pane"
      data-testid="version-timeline-pane"
      style={{
        position: "absolute",
        top: inset,
        right: inset,
        width: 300,
        zIndex: 10,
        maxHeight: `calc(100% - ${inset * 2}px)`,
        overflowY: "auto",
      }}
    >
      <VersionTimeline
        versions={versions}
        latestId={versions.length > 0 ? versions[versions.length - 1].id : 0}
        onRestore={onRestore}
        onPin={onPin}
        onCompareSelect={onCompareSelect}
      />
      {compareIds !== null && compareResult !== null && (
        <div data-testid="compare-pane">
          {compareError && (
            <p data-testid="compare-error" role="alert">
              {compareError}
            </p>
          )}
          <CompareView compare={compareResult} aId={compareIds[0]} bId={compareIds[1]} />
        </div>
      )}
      {versions.filter((v) => v.pinned && !v.archived).length > 0 && (
        <div data-testid="gallery-pane">
          <VariantGallery
            cards={versions
              .filter((v) => v.pinned && !v.archived)
              .map((v) => ({
                ...v,
                actions: ["set-as-main", "branch-from", "archive"] as const,
              }))}
          />
        </div>
      )}
    </div>
  );
}
