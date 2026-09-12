"""Gate 7 — region containment (ticket #8, workstream task-region).

Implements the harness-side entry point for the region-containment gate.
The gate's metric is owned by ticket #7 (``d33d.print_validation``);
this module **consumes** it and adds the N/A fallback required by the
eval-harness spec.

Spec (docs/backlog/06-region-selection.md, ticket #7):

    Metric: fraction of changed triangle area falling outside the
    selected region's 3D bounding volume, flagged when it exceeds the
    named configurable threshold (baseline 5 %).

N/A fallback (ticket #8, open-question resolution):

    The 2D-polygon → 3D bounding-volume convention is "the
    least-specified part of the design" (ticket #7). Until it is
    implemented and the caller can resolve the lasso to a concrete
    ``bbox_min`` / ``bbox_max`` pair, the harness must report
    ``"N/A, containment convention not available"`` — neither a pass
    nor a hard fail.  The gate reports N/A when the caller cannot
    supply a resolved bounding volume (i.e. the lasso-to-3D-lift
    function is absent or the caller passed ``None`` for either bound).

Threshold sourcing:

    The threshold is read via
    :func:`d33d.print_validation.containment_threshold_pct`, which
    reads the named environment variable
    ``D33D_CONTAINMENT_THRESHOLD_PCT`` (default 5.0).  This module does
    not re-implement the threshold reader — it delegates so the
    harness and the production pipeline always agree on the value.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import trimesh

from d33d import print_validation as pv

# ---------------------------------------------------------------------------
# Result type
# ---------------------------------------------------------------------------

#: Closed status enum for the region-containment gate.
#:
#: ``"pass"``  — the edit stayed inside the selection (spillover ≤ threshold).
#: ``"fail"``  — the edit spilled outside the selection (spillover > threshold).
#: ``"na"``    — the 2D→3D volume convention is not yet available; the
#:               gate cannot be evaluated.  Reported to the report as
#:               "N/A, containment convention not available" — not a
#:               pass, not a hard fail.
RegionGateStatus = Literal["pass", "fail", "na"]


@dataclass(frozen=True)
class RegionGateResult:
    """The gate-7 verdict for one region-scoped edit.

    Fields:
        status:
            ``"pass"``, ``"fail"``, or ``"na"`` (see :data:`RegionGateStatus`).
        spillover_pct:
            The measured fraction of changed triangle area outside the
            region bounding volume, as a percentage.  ``0.0`` when
            ``status == "na"``.
        threshold_pct:
            The threshold the comparison was made against.  Read from
            the named environment variable via
            :func:`d33d.print_validation.containment_threshold_pct`.
            ``0.0`` when ``status == "na"``.
        reason:
            A human-readable explanation of the verdict.  For the N/A
            path, this is exactly
            ``"N/A, containment convention not available"``.
    """

    status: RegionGateStatus
    spillover_pct: float
    threshold_pct: float
    reason: str


#: The exact N/A reason string the harness must emit when the
#: lasso-to-3D-volume convention is not yet available.
NA_REASON: str = "N/A, containment convention not available"


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def run_region_gate(
    pre_mesh: trimesh.Trimesh,
    post_mesh: trimesh.Trimesh,
    bbox_min: tuple[float, float, float] | None,
    bbox_max: tuple[float, float, float] | None,
) -> RegionGateResult:
    """Run gate 7 (region containment) for one region-scoped edit.

    Parameters
    ----------
    pre_mesh:
        The pre-edit mesh (baseline, before the region-scoped edit).
    post_mesh:
        The post-edit mesh (after the region-scoped edit).
    bbox_min:
        The minimum corner of the selected region's 3D bounding volume,
        in the mesh's own coordinate space (mm).  Pass ``None`` when
        the lasso-to-3D-lift convention is not yet available — the gate
        will report ``"na"``.
    bbox_max:
        The maximum corner of the selected region's 3D bounding volume,
        in the mesh's own coordinate space (mm).  Pass ``None`` when
        the lasso-to-3D-lift convention is not yet available.

    Returns
    -------
    RegionGateResult
        The gate verdict.  The threshold is always read via
        :func:`d33d.print_validation.containment_threshold_pct` so the
        harness and the production pipeline use the same value.
    """
    # N/A path: the 2D→3D volume convention is not available.
    # Either bound being None means the caller could not resolve the
    # lasso polygon to a concrete 3D bounding volume — report N/A, not
    # a pass or a fail.
    if bbox_min is None or bbox_max is None:
        return RegionGateResult(
            status="na",
            spillover_pct=0.0,
            threshold_pct=0.0,
            reason=NA_REASON,
        )

    # Delegate to ticket #7's containment gate.
    containment = pv.check_containment(pre_mesh, post_mesh, bbox_min, bbox_max)
    status: RegionGateStatus = "pass" if containment.ok else "fail"
    return RegionGateResult(
        status=status,
        spillover_pct=containment.spillover_pct,
        threshold_pct=containment.threshold_pct,
        reason=(
            f"spillover {containment.spillover_pct:.4f}%"
            + (" ≤ " if containment.ok else " > ")
            + f"threshold {containment.threshold_pct:.4f}%"
        ),
    )
