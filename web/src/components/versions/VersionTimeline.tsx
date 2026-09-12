/**
 * VersionTimeline — the version side rail (issue #8, surface 1).
 *
 * One entry per version: name, triggering-message excerpt, timestamp,
 * thumbnail, and a diff badge ("N params changed"). Per-message restore
 * (surface 3) is the restore button — restoring the current latest version
 * is disabled (the no-op edge case). Pin/star (surface 4) is the pin
 * button. The compare-select button feeds the two-viewport compare
 * (surface 5).
 *
 * The timeline NEVER shows git (no commit hashes, branch names, or raw git
 * output — the same invariant the backend asserts).
 */

import type { VersionTimelineEntry } from "../../lib/api";

interface VersionTimelineProps {
  versions: VersionTimelineEntry[];
  /** The current latest version id (its restore is disabled). */
  latestId: number;
  /** Called with the version id to restore (a new forward version). */
  onRestore?: (versionId: number) => void;
  /** Called with (versionId, shouldPin). */
  onPin?: (versionId: number, pinned: boolean) => void;
  /** Called with the version id (compare-select). */
  onCompareSelect?: (versionId: number) => void;
}

export function VersionTimeline({
  versions,
  latestId,
  onRestore,
  onPin,
  onCompareSelect,
}: VersionTimelineProps) {
  if (versions.length === 0) {
    return (
      <section className="version-timeline" data-testid="version-timeline-empty">
        <p>No versions yet — accepted changes appear here.</p>
      </section>
    );
  }
  return (
    <section className="version-timeline" data-testid="version-timeline" aria-label="Version timeline">
      <h2>
        Versions{" "}
        <span data-testid="version-timeline-count" className="timeline-count">
          {versions.length}
        </span>
      </h2>
      <ul className="timeline-list" data-testid="timeline-list">
        {versions.map((v) => {
          const isLatest = v.id === latestId;
          return (
            <li key={v.id} className="timeline-entry" data-testid={`timeline-entry-${v.id}`}>
              {v.thumbnail && (
                <img
                  className="timeline-thumb"
                  src={v.thumbnail}
                  alt={`${v.name} thumbnail`}
                  data-testid={`timeline-thumb-${v.id}`}
                />
              )}
              <div className="timeline-main">
                <span className="timeline-name" data-testid={`timeline-name-${v.id}`}>
                  {v.name}
                </span>
                {v.diff_count > 0 && (
                  <span className="timeline-diff-badge" data-testid={`timeline-diff-${v.id}`}>
                    {v.diff_count} param{(v.diff_count === 1 ? "" : "s")} changed
                  </span>
                )}
                {v.diff_count === 0 && (
                  <span className="timeline-diff-badge" data-testid={`timeline-diff-${v.id}`} />
                )}
                <span className="timeline-msg" data-testid={`timeline-msg-${v.id}`}>
                  {v.created_by_message}
                </span>
                <span className="timeline-ts" data-testid={`timeline-ts-${v.id}`}>
                  {v.created_at}
                </span>
              </div>
              <div className="timeline-actions">
                <button
                  type="button"
                  className="timeline-restore-btn"
                  data-testid={`timeline-restore-${v.id}`}
                  disabled={isLatest}
                  title={isLatest ? "This is the latest version (no-op)" : "Restore to this version"}
                  onClick={() => onRestore?.(v.id)}
                >
                  Restore
                </button>
                <button
                  type="button"
                  className="timeline-pin-btn"
                  data-testid={`timeline-pin-${v.id}`}
                  title={v.pinned ? "Unpin" : "Pin to gallery"}
                  onClick={() => onPin?.(v.id, !v.pinned)}
                >
                  {v.pinned ? "★" : "☆"}
                </button>
                <button
                  type="button"
                  className="timeline-compare-btn"
                  data-testid={`timeline-compare-${v.id}`}
                  title="Select for compare"
                  onClick={() => onCompareSelect?.(v.id)}
                >
                  Compare
                </button>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
