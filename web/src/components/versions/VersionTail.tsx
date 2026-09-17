/**
 * VersionTail — the version-tail overlay (right edge, issue #8): the timeline
 * rail, the compare pane (CompareView) and the pinned-variants gallery
 * (VariantGallery).
 *
 * Restored as user-reachable surfaces alongside the horizontal filmstrip
 * (issue #117 adversarial fix — the strip selects only; the rail owns the
 * compare-select, restore and pin actions). App owns the version state, the
 * compare selection and its fetch, and the callbacks (the same wiring as on
 * main); the filmstrip's slot click feeds the SAME compare-select rotation,
 * so both surfaces drive one state. W16 relocates the actions to the
 * expanded sheet, at which point this surface retires.
 */

import type { VersionCompare, VersionTimelineEntry } from "../../lib/api";
import { VersionTimeline } from "./VersionTimeline";
import { VariantGallery } from "./VariantGallery";
import { CompareView } from "./CompareView";

interface VersionTailProps {
  /** The version timeline entries (the project, oldest first). */
  versions: VersionTimelineEntry[];
  /** The compare selection: [aId, bId] or null when no compare is open. */
  compareIds: [number, number] | null;
  /** The compare fetch result (null while none / loading). */
  compareResult: VersionCompare | null;
  /** The compare fetch failure message (null when none). */
  compareError: string | null;
  /** The inset from the stage edge (px). */
  inset: number;
  /** Called with the version id to restore (a new forward version). */
  onRestore: (versionId: number) => void;
  /** Called with (versionId, shouldPin). */
  onPin: (versionId: number, pinned: boolean) => void;
  /** Called with the version id (compare-select). */
  onCompareSelect: (versionId: number) => void;
}

export function VersionTail({
  versions,
  compareIds,
  compareResult,
  compareError,
  inset,
  onRestore,
  onPin,
  onCompareSelect,
}: VersionTailProps) {
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
