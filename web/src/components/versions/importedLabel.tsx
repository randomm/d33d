/**
 * importedLabel — the "v1 — Imported {filename}" version label (issue
 * #338, decision 8).
 *
 * For the version whose `source_kind` is `import`, the SPA renders "v1 —
 * Imported " followed by the filename from design-state `part.filename`.
 * The filename renders in mono, ellipsis-truncated, with a full-name
 * title attribute, and is never interpreted (a hostile filename is inert
 * text). This applies in the timeline, the filmstrip and the history
 * sheet. If `part.filename` is unavailable, fall back to the version's
 * existing `name`.
 */

import type { PartReportInfo, VersionTimelineEntry } from "../../lib/api";
import copy from "../../copy";

/** The label for a version that may be an import (issue #338, decision 8). */
export function importedVersionLabel(
  version: VersionTimelineEntry,
  part: PartReportInfo | null | undefined,
): React.JSX.Element {
  const isImport = version.source_kind === "import";
  const filename = isImport ? (part?.filename ?? null) : null;
  if (filename) {
    return (
      <span data-testid="version-imported-label">
        {copy.history.importedLabelPrefix}
        <span
          className="mono-face"
          data-testid="version-imported-filename"
          title={filename}
          style={{
            display: "inline-block",
            maxWidth: "100%",
            overflow: "hidden",
            textOverflow: "ellipsis",
            whiteSpace: "nowrap",
            verticalAlign: "bottom",
          }}
        >
          {filename}
        </span>
      </span>
    );
  }
  // Fallback: the version's existing name (non-import, or an import whose
  // part.filename is unavailable).
  return <span data-testid="version-label-fallback">{version.name}</span>;
}
