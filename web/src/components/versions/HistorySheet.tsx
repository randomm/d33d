/**
 * HistorySheet — the expanded history sheet (W16, issue #127).
 *
 * The sheet is where the compare / restore / pin actions belong. It is an
 * OVERLAY over the canvas — not a route, not a page (there is no router in
 * the app, and the four-layer contract leaves no room for one). It is
 * reached FROM the filmstrip (the strip's slot opens it), which is the
 * sheet's only entry point; the filmstrip is absent at zero versions, so
 * the sheet is only ever reachable when at least one version exists —
 * an empty sheet state is unreachable and is not built (copy.history.empty
 * was removed from the deck as dead copy for exactly this reason).
 *
 * Contents (the W16 surface):
 *   - the branch riser graph (BranchGraph): the version graph with restore
 *     risers; pinned variants marked with their why
 *   - the two-viewport compare (CompareView) with the shared camera and
 *     the before/after table (unchanged rows dimmed, not hidden)
 *   - the restore + branch actions on the timeline rows (VersionTimeline)
 *   - the pinned variants gallery (VariantGallery)
 *
 * The sheet REPLACES the transitional VersionTail rail (issue #117
 * adversarial fix): compare, restore and pin are all reachable from this
 * surface, so the rail retires. The sheet's layer is 10 (panels) — the
 * same layer as the filmstrip it is opened from, the brief and the export
 * pane: a floating panel over the canvas. Layer 20 is the conversation
 * (left, 420px wide) and layer 30 is the pin+bar; a sheet at 20 would sit
 * under the conversation column and at 30 would float above the
 * region-edit bar it has no business outranking.
 *
 * Presentational: the parent (App) owns the version state, the compare
 * selection and its fetch, and all the callbacks — the same wiring as the
 * rail had, so nothing the rail's tests asserted about the backend
 * round-trips changes.
 */

import type { VersionCompare, VersionTimelineEntry } from "../../lib/api";
import { VersionTimeline } from "./VersionTimeline";
import { VariantGallery } from "./VariantGallery";
import { CompareView } from "./CompareView";
import { BranchGraph } from "./BranchGraph";

interface HistorySheetProps {
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
  /** Issue #194: the sheet's BOTTOM offset from the stage edge (px). The
   *  default keeps the sheet inset on both edges (the floating layout);
   *  when the conversation is docked at the bottom the parent passes the
   *  bar-clearing value (band height + inset) so the sheet occupies the
   *  canvas band above the bar. The top always stays at the inset — the
   *  sheet is a stage-level sibling, so its offsets resolve against the
   *  stage, never against the docked pane. */
  sheetBottomOffset?: number;
  /** Called with the version id to restore (a new forward version). */
  onRestore: (versionId: number) => void;
  /** Called with (versionId, shouldPin). */
  onPin: (versionId: number, pinned: boolean) => void;
  /** Called with the version id (compare-select). */
  onCompareSelect: (versionId: number) => void;
  /** Called to close the sheet (back to the filmstrip). */
  onClose: () => void;
}

export function HistorySheet({
  versions,
  compareIds,
  compareResult,
  compareError,
  inset,
  sheetBottomOffset,
  onRestore,
  onPin,
  onCompareSelect,
  onClose,
}: HistorySheetProps) {
  return (
    <div
      className="history-sheet"
      data-testid="history-sheet"
      aria-label="History sheet"
      style={{
        position: "absolute",
        top: inset,
        right: inset,
        bottom: sheetBottomOffset ?? inset,
        width: 360,
        zIndex: 10,
        overflowY: "auto",
      }}
    >
      {/* The sheet's header + close. The sheet is an overlay: its close
          button returns to the filmstrip; there is no route to navigate. */}
      <div className="history-sheet-header" data-testid="history-sheet-header">
        <button
          type="button"
          className="history-sheet-close"
          data-testid="history-sheet-close"
          onClick={onClose}
          aria-label="Close the history sheet"
        >
          ×
        </button>
      </div>

      {/* The branch riser graph — the sheet's reason to be an overlay with
          room (the filmstrip cannot draw this). */}
      <BranchGraph versions={versions} />

      {/* The compare pane: shown when two versions are selected (the same
          compare-select rotation as the rail had). */}
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

      {/* The timeline: the restore + pin + compare-select actions live on
          these rows (the sheet is where they belong). */}
      <VersionTimeline
        versions={versions}
        latestId={versions.length > 0 ? versions[versions.length - 1].id : 0}
        onRestore={onRestore}
        onPin={onPin}
        onCompareSelect={onCompareSelect}
      />

      {/* The pinned variants gallery (the pin action's browse surface). */}
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
