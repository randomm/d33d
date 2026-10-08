"""Unchanged-mesh post-check (issue #419).

The design loop's ok-render post-check for an edit turn (v2+) whose
rendered mesh is unchanged from its parent version's rendered mesh: a
candidate whose volume and face count equal the parent's (within a small
relative epsilon) is NOT a change — it does not pass. The QA v100
repro: a plate import, "make the center hole 38 mm" → the fill-and-
recut offer was accepted, but the model's SCAD had
``scale(1) import("part.stl")`` with NO semicolon, so the following
``difference() { ... }`` became a CHILD of ``import()`` (which ignores
children) and re-exported the parent byte-for-byte (664 faces,
56315.87 mm3, delta 0) — and the loop passed "Your design is ready".

The check runs in the ok-render post-check chain of
``d33d.design_loop.run_design_loop_async`` (after the stack-height
post-check, before the pass return), and rides the EXISTING
``geometrically_wrong`` class (no new error_class) via ``route_repair``
— the same shape as the #317 screw / #386 through-hole / #412 stack /
#332 import-guard post-checks.

The mesh comparison is LOAD-AND-COMPARE ONLY (no trimesh boolean ops —
CI has no boolean backend): the candidate's ``render.stl`` and the
parent's stored ``model.stl`` are both loaded with trimesh and their
total (pre-merge) face counts and signed volumes are compared within a
small relative tolerance. Both sides go through the SAME load
(``trimesh.load(process=False, force="mesh")`` without a subsequent
``merge_vertices``), so the comparison is symmetric: face counts and
volumes measured by identical means are comparable even though the
two files were written by different OpenSCAD invocations (re-export
float noise).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "UNCHANGED_INSTRUCTION",
    "unchanged_mesh_check",
]

#: The relative tolerance on the VOLUME comparison (issue #419): a
#: re-export of identical geometry is not byte-identical (STL floats
#: drift in low-order bits across exports), but a genuine ~1 mm edit
#: on a ~56 k mm3 part changes the volume by well OVER 0.1% (a 1 mm
#: deep, 10 mm diameter pocket on the v100 plate is ~78.5 mm3 ≈ 0.14%
#: of 56315.87 mm3 — over the floor; a 1 mm change anywhere on a
#: smaller part is a much larger percentage). ``max(0.1% of the parent,
#: 0.1 mm3 absolute floor)`` is the pass threshold: below it the mesh
#: is "unchanged" (the check fires), at or above it the mesh changed
#: (the check passes).
_VOLUME_REL_TOL = 0.001

#: The absolute floor on the volume comparison (mm3): a tiny part
#: (sub-mm scale) where 0.1% is below the noise floor of the export
#: itself — the floor keeps the epsilon from vanishing.
_VOLUME_ABS_FLOOR_MM3 = 0.1

#: The relative tolerance on the FACE COUNT comparison: re-triangulation
#: of identical geometry can shift the face count slightly (a different
#: export order, a re-split of a quad); a genuine edit that changes the
#: geometry changes the count by more than 1% (a pocket adds faces, a
#: recut changes the count by the number of facets the cut crosses).
_FACE_REL_TOL = 0.01


#: The repair instruction the loop carries to the next iteration when
#: the check fires (issue #419): the candidate's rendered mesh is
#: identical to the parent's, so the requested change did not take.
#: The named instruction is the single definition of the fix text (the
#: check module owns the detection, this constant owns the fix — the
#: #317 split, kept in one place).
UNCHANGED_INSTRUCTION = (
    "The rendered mesh is unchanged from the previous version — the "
    "requested change did not take. The most common cause on an import "
    "project is a missing semicolon after `scale(<scale>) import("
    '"part.stl")`: without it the following `difference() { ... }` '
    "becomes a child of `import()`, which ignores its children, so the "
    "render re-exports the parent identical. Add the semicolon so the "
    "difference() is a top-level operation on the imported mesh, and "
    "make sure the subtraction actually removes material (the cylinder "
    "in a difference() must overlap the parent's volume — a `position=` "
    "argument on a builtin that does not accept it (e.g. `cylinder` "
    "has no `position` parameter) is silently dropped by OpenSCAD, "
    "which warns in the render log and places the shape at the origin "
    "— re-check the named arguments against the builtin's actual "
    "parameters)."
)


def _load_mesh(path: str) -> Any | None:
    """Load the STL at ``path`` for the unchanged-mesh comparison.

    ``None`` on any load failure (missing file, trimesh error) — the
    caller then abstains (a fabricated baseline would make the gate
    lie). The load is the SAME shape on both sides (``process=False``,
    ``force="mesh"``) so the face-count / volume comparison is
    symmetric; NO ``merge_vertices`` is applied here — the mesh is
    measured as the exporter wrote it, on both sides.
    """
    if not path or not isinstance(path, str):
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import trimesh

        return trimesh.load(str(p), process=False, force="mesh")
    except Exception:  # any load failure → abstain, never a raise
        logger.debug("unchanged-mesh check: failed to load %r", path)
        return None


def _volume(mesh: Any) -> float | None:
    """The mesh's volume, or ``None`` when it cannot be read.

    ``mesh.volume`` is the SIGNED volume (trimesh sums per-face signed
    tetrahedra) and RAISES only for a mesh whose winding is too
    inconsistent to sum — the caller then abstains on the volume leg
    and relies on the face count alone (the one the v100 repro is
    decisive on). A zero-volume mesh (a degenerate shape) returns
    ``0.0`` — the caller treats ``abs(parent) <= 0`` as "the volume
    leg abstains" (the face count is the sole signal then)."""
    try:
        return float(mesh.volume)
    except (ValueError, TypeError, RuntimeError, IndexError):
        return None


def unchanged_mesh_check(
    *,
    parent_stl: str | None,
    candidate_stl: str | None,
    parent_volume_mm3: float | None = None,
    parent_face_count: int | None = None,
    scad_source: str = "",
) -> tuple[str, str] | None:
    """Issue #419: the unchanged-mesh post-check (detection).

    Returns ``None`` (the check abstains — the candidate is allowed to
    pass on its own merits) when:

    - ``parent_stl`` is ``None`` AND ``parent_volume_mm3`` is ``None``
      (no parent version, no stored ``model.stl`` — a v1 design, a 3MF
      import, a missing file). The parent side must have SOME baseline;
      when pre-computed stats are supplied (``parent_volume_mm3`` and
      ``parent_face_count``), ``parent_stl`` may be ``None`` (the path
      is not needed — the stats were already measured off the same mesh
      load the genus came from).
    - ``candidate_stl`` is ``None`` or unloadable (the loop's
      render has no STL, or it cannot be read).
    - Either mesh's face count cannot be read (a zero-face load is a
      load failure — the caller's empty_model gate already handles
      that shape).

    Returns ``(evidence, instruction)`` when the candidate's rendered
    mesh equals the parent's (volume and face count within the
    relative epsilon): the evidence names the unchanged volume and
    face count (the loop's repair dict carries it to the next
    iteration's ``REPAIR`` block), and the instruction is
    :data:`UNCHANGED_INSTRUCTION`.
    """
    # The parent side: either a path to load, or pre-computed stats.
    # Both must be present for the check to have a baseline.
    parent: Any | None = None
    parent_faces: int | None = None
    parent_vol: float | None = None

    if parent_volume_mm3 is not None or parent_face_count is not None:
        # Pre-computed stats path: the parent mesh was already loaded
        # (off the event loop) in the seam that also measured the genus.
        # Use those stats directly — no second load.
        if parent_face_count is not None and parent_face_count > 0:
            parent_faces = parent_face_count
        if parent_volume_mm3 is not None:
            parent_vol = parent_volume_mm3
        if parent_faces is None:
            return None  # no usable face count → abstain
    elif parent_stl is not None:
        # Path-based path (the original design): load the parent mesh.
        parent = _load_mesh(parent_stl)
        if parent is None:
            return None
        parent_faces = len(parent.faces)
        if parent_faces <= 0:
            return None
        parent_vol = _volume(parent)
    else:
        # No parent at all → abstain.
        return None

    if candidate_stl is None or not isinstance(candidate_stl, str) or not candidate_stl:
        return None
    candidate = _load_mesh(candidate_stl)
    if candidate is None:
        return None
    candidate_faces = len(candidate.faces)
    if candidate_faces <= 0:
        return None
    candidate_vol = _volume(candidate)

    # Face count: the primary signal (re-export noise on face count is
    # small — a re-triangulation of the same geometry shifts it by a
    # handful of facets at most, never by 1%).
    face_delta = abs(candidate_faces - parent_faces)
    face_tol = max(_FACE_REL_TOL * parent_faces, 1.0)
    faces_changed = face_delta > face_tol

    # Volume: the secondary signal (relative tolerance with an absolute
    # floor — see the module constants). A non-watertight mesh cannot
    # yield a volume; the check then relies on the face count alone
    # (the v100 repro is decisive on face count: 664 == 664).
    if parent_vol is not None and candidate_vol is not None and abs(parent_vol) > 0.0:
        vol_delta = abs(candidate_vol - parent_vol)
        vol_tol = max(_VOLUME_REL_TOL * abs(parent_vol), _VOLUME_ABS_FLOOR_MM3)
        vol_changed = vol_delta > vol_tol
    else:
        # One side non-watertight (or zero-volume): the volume leg
        # abstains; the face count is the sole signal (still a valid
        # "unchanged" check — a genuine edit changes the face count by
        # more than 1% on any non-trivial part).
        vol_changed = False

    if faces_changed or vol_changed:
        return None

    # Unchanged: build the evidence naming the unchanged numbers.
    if parent_vol is not None:
        _vol = f"{parent_vol:,.2f}"
    else:
        _vol = "unknown (non-watertight)"
    if candidate_vol is not None:
        _cvol = f"{candidate_vol:,.2f}"
    else:
        _cvol = _vol
    evidence = (
        f"rendered mesh is unchanged from the parent version "
        f"(volume {_vol} mm3, {parent_faces} faces; "
        f"candidate: {_cvol} mm3, {candidate_faces} faces)"
    )
    return evidence, UNCHANGED_INSTRUCTION
