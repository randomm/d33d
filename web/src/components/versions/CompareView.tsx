/**
 * CompareView — the two-viewport compare inside the history sheet (W16).
 *
 * The two viewports SHARE ONE CAMERA STATE: the compare response's
 * `shared_rotation` contract (identical units + axis convention) is the
 * contract that lets them; the CAPTION (copy.history.sharedCamera) makes
 * the claim explicit, so the implementation is a shared pose source, not
 * two independent viewers that happen to start at the same angle.
 *
 * The camera pose is a single object the sheet's CompareView owns: both
 * viewports render it, and a rotation applied through EITHER viewport
 * updates the same object, so the other viewport re-renders the same pose.
 * A desynchronised implementation (each viewport with its own pose) makes
 * the caption a lie — and the test that drives one viewport and asserts
 * the other follows is the one that catches it.
 *
 * No geometry is loaded here: the endpoint returns no geometry payload
 * (the per-version geometry is not in the API today), so the viewports
 * render the version thumbnails (when present) as their content. The
 * shared camera is a REAL camera-state source (a live pose object), so the
 * caption is testable even though the geometry is not.
 *
 * The parameter table is BEFORE/AFTER over the UNION of both param sets:
 * unchanged rows are PRESENT AND DIMMED, never omitted — a hidden row is
 * indistinguishable from a row that does not exist, which would make the
 * comparison a claim the interface never established. The diff
 * (added/removed/changed) comes from the endpoint and is consumed as-is;
 * the unchanged set is derived as keys present in both param sets that the
 * endpoint did not list (derivation, not recomputation — the values are
 * never compared here).
 */

import { useState } from "react";
import type { VersionCompare } from "../../lib/api";
import copy from "../../copy";

interface CompareViewProps {
  compare: VersionCompare;
  /** The version id per viewport (testids). */
  aId: number;
  bId: number;
}

/** The one camera state both viewports read and write. */
interface CameraPose {
  /** Yaw in degrees (turning the model). */
  yaw: number;
  /** Pitch in degrees. */
  pitch: number;
}

/** One viewport: renders the SHARED pose (it displays it, it does not own
 *  a copy) and applies rotations through the shared update callback. */
function CompareViewport({
  versionName,
  versionId,
  thumbnail,
  pose,
  onRotate,
}: {
  versionName: string;
  versionId: number;
  thumbnail: string | null;
  pose: CameraPose;
  onRotate: (deltaYaw: number, deltaPitch: number) => void;
}) {
  return (
    <div className="compare-viewport" data-testid={`compare-viewport-${versionId}`}>
      <h3>{versionName}</h3>
      {thumbnail && (
        <img
          src={thumbnail}
          alt={`${versionName} preview`}
          data-testid={`compare-thumb-${versionId}`}
        />
      )}
      {/* The pose the viewport is showing — the SHARED object's current
          values. A rotation through either viewport lands on this object,
          so both readouts agree; the test desynchronises nothing, because
          there is nothing to desynchronise. */}
      <span
        className="compare-viewport-pose"
        data-testid={`compare-viewport-pose-${versionId}`}
      >
        {pose.yaw.toFixed(0)}° / {pose.pitch.toFixed(0)}°
      </span>
      <button
        type="button"
        className="compare-viewport-rotate-btn"
        data-testid={`compare-rotate-${versionId}`}
        aria-label={`Rotate ${versionName}`}
        onClick={() => onRotate(15, 0)}
      >
        Rotate
      </button>
    </div>
  );
}

export function CompareView({ compare, aId, bId }: CompareViewProps) {
  const { a, b, diff, shared_rotation } = compare;
  // The ONE camera state (the shared-rotation contract's client side):
  // both viewports read it and route rotations through it.
  const [pose, setPose] = useState<CameraPose>({ yaw: 0, pitch: 0 });
  const rotate = (dyaw: number, dpitch: number) =>
    setPose((p) => ({ yaw: p.yaw + dyaw, pitch: p.pitch + dpitch }));

  // The union of both param sets — the table's row set. The endpoint's
  // diff categorises which side each row is interesting on; everything
  // else is unchanged and renders dimmed. Order: a's keys first (the
  // "before" order), then b-only keys (the added ones).
  const keys: string[] = [
    ...Object.keys(a.params),
    ...Object.keys(b.params).filter((k) => !(k in a.params)),
  ];

  return (
    <section
      className="compare-view"
      data-testid="compare-view"
      aria-label="Version compare"
    >
      <h2>
        {a.name} vs {b.name}
      </h2>

      {/* The two viewports — one camera state between them. */}
      <div className="compare-viewport-pair" data-testid="compare-viewport-pair">
        <CompareViewport
          versionName={a.name}
          versionId={aId}
          thumbnail={a.thumbnail}
          pose={pose}
          onRotate={rotate}
        />
        <CompareViewport
          versionName={b.name}
          versionId={bId}
          thumbnail={b.thumbnail}
          pose={pose}
          onRotate={rotate}
        />
      </div>

      {/* The shared-camera claim, in the deck's own words. The caption is
          a CLAIM: it is true only because the viewports share one pose
          object — the test that rotates one and reads the other proves it. */}
      <p className="compare-shared-camera" data-testid="compare-shared-camera">
        {copy.history.sharedCamera}
      </p>

      {/* The shared-rotation contract (the endpoint's statement that the
          two viewports are permitted to share one rotation state). */}
      <div
        className="compare-shared-rotation"
        data-testid="compare-shared-rotation"
        aria-label="Shared rotation contract"
      >
        Shared rotation: {shared_rotation.units} / {shared_rotation.axis_convention}
        {shared_rotation.identical_convention ? " (identical)" : " (differs)"}
      </div>

      {/* The before/after parameter table: every key present on either
          side renders; unchanged rows are dimmed, never hidden. */}
      <div className="compare-diff-table" data-testid="compare-diff-table">
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
            {keys.map((key) => {
              const kind = diff.changed.includes(key)
                ? "changed"
                : diff.added.includes(key)
                  ? "added"
                  : diff.removed.includes(key)
                    ? "removed"
                    : "unchanged";
              const aVal = key in a.params ? a.params[key] : null;
              const bVal = key in b.params ? b.params[key] : null;
              const rowClass =
                kind === "unchanged" ? " compare-diff-row--unchanged" : "";
              const testId = `diff-${kind}-${key}`;
              return (
                <tr key={key} data-testid={testId} className={`compare-diff-row${rowClass}`}>
                  <td>{key}</td>
                  <td>{aVal === null ? copy.history.change.notPresent : String(aVal)}</td>
                  <td>{bVal === null ? copy.history.change.notPresent : String(bVal)}</td>
                  <td data-testid={`diff-${kind}-change-${key}`}>
                    {kind === "changed"
                      ? `${String(aVal)} → ${String(bVal)}`
                      : kind === "unchanged"
                        ? ""
                        : copy.history.change[kind]}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
