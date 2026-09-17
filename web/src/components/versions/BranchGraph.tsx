/**
 * BranchGraph — the history sheet's version graph with branch risers (W16).
 *
 * The design-team answer: "a branch is a MARK in the filmstrip and a
 * GRAPH in this sheet." The filmstrip shows only the lineage you are on
 * (fork glyph + sibling count); the risers and the comparison live HERE,
 * because this surface has room to draw a graph.
 *
 * The graph is drawn from the version graph the timeline endpoint returns —
 * {parent, restored_from} edges — NEVER from git (no commit hashes, no
 * branch names; the same invariant the filmstrip's contract pins). A
 * `restored_from` edge is the "rise-back": a version created by restoring
 * an earlier one, drawn rising back from its parent to the version it
 * carries. `forked_from` is null within one project by construction
 * (branch-from creates a NEW project); cross-project fork siblings are
 * out of scope for the graph — the filmstrip's sibling count covers them.
 *
 * Layout: one row per version, top (oldest) to bottom (newest), in list
 * order. Each version is a node; a parent edge is a straight riser from
 * the parent row; a restored_from edge is a riser drawn back UP from the
 * version to its source, labelled. Pinned versions carry the pin mark and
 * their "why" (the message that triggered them — there is no separate
 * pin_reason field in the data shape).
 *
 * Presentational: the parent (HistorySheet) owns the version list.
 */

import type { VersionTimelineEntry } from "../../lib/api";
import copy from "../../copy";

interface BranchGraphProps {
  /** The version timeline entries (oldest first). */
  versions: VersionTimelineEntry[];
}

/** The row geometry (px) for a version id in the list. */
const ROW_H = 44;
const NODE_X = 120; // the main-line node x
const RISER_X = 36; // where the restored-from riser rises
const GRAPH_W = 200;
const GRAPH_H = (n: number) => n * ROW_H + 8;

export function BranchGraph({ versions }: BranchGraphProps) {
  const n = versions.length;
  const yOf = (id: number): number => {
    const idx = versions.findIndex((v) => v.id === id);
    return (idx + 1) * ROW_H - ROW_H / 2 + 4;
  };

  return (
    <section
      className="branch-graph"
      data-testid="branch-graph"
      aria-label="Version graph"
    >
      <h3>{copy.history.allVersions}</h3>
      <svg
        className="branch-graph-svg"
        data-testid="branch-graph-svg"
        width={GRAPH_W}
        height={GRAPH_H(n)}
        role="img"
        aria-label="The version graph: the main line and the restore risers"
      >
        {versions.map((v) => {
          const y = yOf(v.id);
          return (
            <g key={v.id} data-testid={`branch-node-${v.id}`}>
              {/* The main-line riser up to this node (from its parent). */}
              {v.parent !== null && versions.some((p) => p.id === v.parent) && (
                <line
                  data-testid={`branch-parent-edge-${v.id}`}
                  x1={NODE_X}
                  y1={yOf(v.parent)}
                  x2={NODE_X}
                  y2={y}
                  stroke="var(--color-hairline)"
                  strokeWidth={2}
                />
              )}
              {/* The restore riser: drawn from the version's row UP to the
                  version it carries (the rise-back). */}
              {v.restored_from !== null &&
                versions.some((p) => p.id === v.restored_from) && (
                  <g data-testid={`branch-restore-riser-${v.id}`}>
                    <polyline
                      points={`${NODE_X},${y} ${RISER_X},${y} ${RISER_X},${yOf(v.restored_from)} ${NODE_X},${yOf(v.restored_from)}`}
                      fill="none"
                      stroke="var(--color-live)"
                      strokeWidth={1.5}
                    />
                    <text
                      x={RISER_X - 4}
                      y={(y + yOf(v.restored_from)) / 2}
                      transform={`rotate(-90 ${RISER_X - 4} ${(y + yOf(v.restored_from)) / 2})`}
                      fontSize={10}
                      fill="var(--color-muted)"
                    >
                      {copy.history.riserRestored}
                    </text>
                  </g>
                )}
              {/* The node itself: the version id (mono), never a hash. */}
              <circle cx={NODE_X} cy={y} r={5} fill="var(--color-panel)" stroke="var(--color-fg-2)" strokeWidth={1.5} />
              <text
                x={NODE_X + 12}
                y={y + 4}
                fontSize={11}
                fontFamily="var(--font-mono)"
                fill="var(--color-fg)"
              >
                v{v.id} · {v.name}
              </text>
              {/* The pin mark + the why (the version's own message). */}
              {v.pinned && (
                <text
                  data-testid={`branch-pinned-${v.id}`}
                  x={NODE_X + 12}
                  y={y + 18}
                  fontSize={10}
                  fill="var(--color-muted)"
                >
                  {copy.history.pinnedMark(`v${v.id}`)} — {v.created_by_message}
                </text>
              )}
            </g>
          );
        })}
      </svg>
      {/* The legend: what the two edge kinds mean. */}
      <div className="branch-graph-legend" data-testid="branch-graph-legend">
        <span data-testid="branch-legend-parent">{copy.history.riserParent}</span>
        <span data-testid="branch-legend-restored">{copy.history.riserRestored}</span>
      </div>
    </section>
  );
}
