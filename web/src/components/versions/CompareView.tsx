/**
 * CompareView — the two-viewport compare (issue #8, surface 5 — the
 * prioritized surface).
 *
 * Two viewports (the two versions) with SHARED rotation: the compare
 * response's `shared_rotation` contract (identical units + axis convention)
 * is what lets the two client viewports share one camera/rotation state.
 * The parameter diff table (added/removed/changed) sits alongside.
 *
 * No geometry is loaded here — the viewports are placeholders for the
 * render worker's per-version geometry (the design-loop-to-SSE pipeline is
 * a future ticket); the shared-rotation contract and the diff table are
 * the real, testable surface.
 */

import type { VersionCompare } from "../../lib/api";

interface CompareViewProps {
  compare: VersionCompare;
  /** The version id to load the geometry for (per viewport). */
  aId: number;
  bId: number;
}

export function CompareView({ compare, aId, bId }: CompareViewProps) {
  const { a, b, diff, shared_rotation } = compare;
  const empty = diff.count === 0;
  return (
    <section className="compare-view" data-testid="compare-view" aria-label="Version compare">
      <h2>Compare: {a.name} vs {b.name}</h2>

      {/* The two viewports (shared rotation — one camera/rotation state). */}
      <div className="compare-viewport-pair" data-testid="compare-viewport-pair">
        <div className="compare-viewport" data-testid={`compare-viewport-${aId}`}>
          <h3>{a.name}</h3>
          {a.thumbnail && (
            <img src={a.thumbnail} alt={`${a.name} preview`} data-testid={`compare-thumb-${aId}`} />
          )}
        </div>
        <div className="compare-viewport" data-testid={`compare-viewport-${bId}`}>
          <h3>{b.name}</h3>
          {b.thumbnail && (
            <img src={b.thumbnail} alt={`${b.name} preview`} data-testid={`compare-thumb-${bId}`} />
          )}
        </div>
      </div>

      {/* The shared-rotation contract (what lets the two viewports share
          one rotation state). */}
      <div
        className="compare-shared-rotation"
        data-testid="compare-shared-rotation"
        aria-label="Shared rotation contract"
      >
        Shared rotation: {shared_rotation.units} / {shared_rotation.axis_convention}
        {shared_rotation.identical_convention ? " (identical)" : " (differs)"}
      </div>

      {/* The parameter diff table. */}
      <div className="compare-diff-table" data-testid="compare-diff-table">
        {empty ? (
          <p data-testid="compare-no-diff">The two versions are identical.</p>
        ) : (
          <table data-testid="compare-diff">
            <thead>
              <tr>
                <th>Parameter</th>
                <th>{a.name}</th>
                <th>{b.name}</th>
                <th>Change</th>
              </tr>
            </thead>
            <tbody>
              {diff.changed.map((key) => (
                <tr key={key} data-testid={`diff-changed-${key}`}>
                  <td>{key}</td>
                  <td>{String(a.params[key] ?? "—")}</td>
                  <td>{String(b.params[key] ?? "—")}</td>
                  <td>
                    {String(a.params[key] ?? "—")} → {String(b.params[key] ?? "—")}
                  </td>
                </tr>
              ))}
              {diff.added.map((key) => (
                <tr key={key} data-testid={`diff-added-${key}`}>
                  <td>{key}</td>
                  <td>—</td>
                  <td>{String(b.params[key] ?? "—")}</td>
                  <td>added</td>
                </tr>
              ))}
              {diff.removed.map((key) => (
                <tr key={key} data-testid={`diff-removed-${key}`}>
                  <td>{key}</td>
                  <td>{String(a.params[key] ?? "—")}</td>
                  <td>—</td>
                  <td>removed</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </section>
  );
}
