"""Unchanged-mesh post-check (issue #419).

The design loop's ok-render post-check for an edit turn (v2+) whose
rendered mesh is unchanged from its parent version's rendered mesh.
"Unchanged" means the GEOMETRY is identical — not just its summary
stats. The candidate's rendered mesh is loaded with trimesh and its
geometry fingerprint is compared against the parent's: the parent
fingerprint comes from the SAME load the seam already made (the
``parent_fingerprint`` kwarg) or, on the path-based fallback, is
computed from the parent's ``model.stl`` right here. The fingerprint is
the exact VERTEX SET — each coordinate rounded to 1e-3 mm, deduplicated
(STLs repeat vertices per face), sorted lexicographically, and
sha256-hashed — a single hash (the vertex set alone already separates
every real geometric change, and it is multi-component-safe by
construction). A volume-within-tolerance comparison rides as a sanity
check.

Why the fingerprint and not summary stats?

Volume, face count, and centroid are all translation-invariant or
scale-insensitive: a legitimate edit that moves a small hole (a Ø3
bore moved 10 mm on a 120 mm plate) shifts the volume centroid by
~0.007 mm and changes the volume by < 1e-6 mm3 — every summary stat
sits inside tolerance and the old check flagged a correct edit as
"nothing moved" (false fail, the PM's #419 concern). The fingerprint
is exact: ANY real geometric change, however small, moves at least one
vertex (a moved hole moves its rim; a resized bore moves its wall; a
re-triangulation moves or adds a vertex), and an OpenSCAD re-render of
an unchanged model (the v100 case: ``import()`` plus an ignored child)
re-exports the identical geometry (identical vertex set) — the STL
float noise in low-order bits is absorbed by the 1e-3 mm rounding.

A DIFFERENT triangulation of the same shape (same vertices, different
connectivity) does not change the vertex set — the check passes,
erring toward "changed", the safe direction (a correct edit is never
blocked by a re-triangulation). The same reasoning holds for a
re-triangulation that MOVES or ADDS a vertex (the common case): the
vertex set changes, the check fires, and the candidate is flagged for
repair — which is correct, since a re-triangulation that moves
vertices is not an unchanged mesh.

The mesh comparison is LOAD-AND-COMPARE ONLY (no trimesh boolean ops —
CI has no reliable boolean backend): the candidate's ``render.stl`` and
the parent's stored ``model.stl`` (or the seam's pre-computed
fingerprint) are compared via their fingerprints. Both sides go through
the SAME load shape (``trimesh.load(process=False, force="mesh")``,
no ``merge_vertices`` — the mesh is measured as the exporter wrote
it), so the comparison is symmetric even though the two files were
written by different OpenSCAD invocations (re-export float noise).
"""

from __future__ import annotations

import hashlib
import logging
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

__all__ = [
    "MESH_UNCHANGED_REASON",
    "UNCHANGED_INSTRUCTION",
    "fingerprint_from_rounded_vertices",
    "fingerprint_stl",
    "mesh_fingerprint",
    "unchanged_mesh_check",
]

#: The vertex coordinate rounding (mm) used by the fingerprint: 1e-3
#: mm is far below the precision of any printable edit (sub-micron
#: noise in the STL float round-trip) and far above the export noise,
#: so identical geometry re-exported by a different OpenSCAD
#: invocation hashes identically while any real geometric change
#: (a moved vertex, an added vertex) hashes differently.
_ROUND_MM = 1e-3

#: The relative tolerance on the VOLUME sanity check: a re-export of
#: identical geometry is not byte-identical (STL floats drift in
#: low-order bits across exports), but a genuine ~1 mm edit on a
#: ~50 k mm3 part changes the volume by well over 0.1%. ``max(0.1% of
#: the parent, 0.1 mm3 absolute floor)`` is the pass threshold.
_VOLUME_REL_TOL = 0.001

#: The absolute floor on the volume comparison (mm3): a tiny part
#: (sub-mm scale) where 0.1% is below the noise floor of the export
#: itself — the floor keeps the epsilon from vanishing.
_VOLUME_ABS_FLOOR_MM3 = 0.1


#: The structured reason the design loop's unchanged-mesh post-check
#: fires: the candidate's rendered mesh is GEOMETRICALLY identical to
#: the parent's (issue #419). The single name of the fired-check reason
#: — the check module owns the detection, :data:`UNCHANGED_INSTRUCTION`
#: owns the fix, and this constant owns the reason the loop's repair
#: dict, ``_exhausted``'s reason swap, the failure archive's vocabulary
#: and the adapter's version-skip all compare against.
MESH_UNCHANGED_REASON = "mesh_unchanged"

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


def fingerprint_from_rounded_vertices(
    vertices: Iterable[tuple[float, float, float]],
) -> tuple[str, int] | None:
    """The shared fingerprint helper (module docstring: the rationale).

    ``vertices`` is any iterable of ``(x, y, z)`` coordinate triples;
    ``None`` for an empty vertex set or an unreadable vertex set.
    """
    try:
        rounded = {
            (
                round(float(x) / _ROUND_MM),
                round(float(y) / _ROUND_MM),
                round(float(z) / _ROUND_MM),
            )
            for (x, y, z) in vertices
        }
    except (ValueError, TypeError, RuntimeError, IndexError, OverflowError):
        return None
    if not rounded:
        return None
    # Vectorised sort/hash (issue #419 lens round 2): the ROUNDING step
    # stays in pure Python (``round(float(x) / 1e-3)`` — Python's exact
    # half-up integer rounding of the float division; numpy's
    # ``round(x / 1e-3)`` does double rounding of the already-inexact
    # quotient and drifts on ties, so it must NOT replace it). The
    # dedup/sort/hash is numpy's: ``np.unique(axis=0)`` sorts the
    # integer rows lexicographically (the same order as ``sorted`` over
    # the int tuples) and the native int64 big-endian pack matches the
    # old per-int ``to_bytes`` output byte-for-byte (the equality is
    # pinned by the reference test in tests/test_design_loop.py, which
    # keeps the old implementation as a reference and compares
    # fingerprints on every committed STL fixture).
    rounded_arr = np.asarray(list(rounded), dtype=np.int64)
    unique = np.unique(rounded_arr, axis=0)
    vbytes = unique.astype(">i8").tobytes()
    return (hashlib.sha256(vbytes).hexdigest(), int(unique.shape[0]))


def mesh_fingerprint(mesh: Any) -> tuple[str, int] | None:
    """The mesh's geometry fingerprint (module docstring: the rationale).

    ``None`` when the mesh has zero vertices or the vertices cannot be
    read.
    """
    try:
        verts = mesh.vertices
        if len(verts) <= 0:
            return None
    except (ValueError, TypeError, RuntimeError, IndexError, OverflowError):
        return None
    return fingerprint_from_rounded_vertices(verts)


def fingerprint_stl(path: str | None) -> tuple[str, int] | None:
    """Issue #432: the geometry fingerprint of the STL at ``path`` — one
    load, then :func:`mesh_fingerprint`. ``None`` abstains (missing or
    unreadable file, or an empty mesh)."""
    mesh = _load_mesh(path)
    if mesh is None:
        return None
    return mesh_fingerprint(mesh)


def _load_mesh(path: str) -> Any | None:
    """Load the STL at ``path`` (module docstring: the load shape).

    ``None`` on any load failure — the caller abstains (a fabricated
    baseline would make the gate lie).
    """
    if not path or not isinstance(path, str):
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        import trimesh

        return trimesh.load(str(p), process=False, force="mesh")
    except (OSError, ValueError, RuntimeError):
        # Missing file, unreadable bytes, or a trimesh parse failure —
        # any load failure → abstain, never a raise. The exception is
        # logged with its traceback (the ``genus_from_stl`` pattern) and
        # only the path's BASENAME is logged — never the full path
        # (issue #356's host-path-disclosure convention).
        logger.warning(
            "unchanged-mesh check: failed to load %r", p.name, exc_info=True
        )
        return None


def _volume(mesh: Any) -> float | None:
    """The mesh's signed volume (module docstring: the sanity leg).

    ``None`` when the volume cannot be read (the caller abstains on
    the volume leg — the fingerprint comparison is the primary signal).
    """
    try:
        return float(mesh.volume)
    except (ValueError, TypeError, RuntimeError, IndexError, ZeroDivisionError):
        return None


def unchanged_mesh_check(
    *,
    parent_stl: str | None,
    candidate_stl: str | None,
    parent_fingerprint: tuple[str, int] | None = None,
    parent_volume_mm3: float | None = None,
    parent_face_count: int | None = None,
) -> tuple[str, str] | None:
    """Issue #419: the unchanged-mesh post-check (detection).

    Module docstring: the fingerprint rationale. Returns
    ``(evidence, instruction)`` when the candidate's rendered mesh has
    the SAME geometry fingerprint as the parent's, ``None`` when it
    changed or the check abstains (no parent baseline, no candidate
    STL, or an unreadable fingerprint — a fabricated baseline would
    make the gate lie).
    """
    # The parent side: either a pre-computed fingerprint (the seam
    # measured it off the same load as the genus) or a path to load.
    # When both are present the fingerprint is used (the path-based
    # load stays a fallback only).
    parent_fp: tuple[str, int] | None = None
    if parent_fingerprint is not None:
        parent_fp = parent_fingerprint
    elif parent_stl is not None:
        parent = _load_mesh(parent_stl)
        if parent is not None:
            parent_fp = mesh_fingerprint(parent)
    if parent_fp is None:
        return None  # no parent baseline → abstain

    if candidate_stl is None or not isinstance(candidate_stl, str) or not candidate_stl:
        return None
    candidate = _load_mesh(candidate_stl)
    if candidate is None:
        return None
    candidate_fp = mesh_fingerprint(candidate)
    if candidate_fp is None:
        return None
    candidate_vol = _volume(candidate)

    # -- Leg 1 (primary): the geometry fingerprint (the exact vertex
    # set). ANY real geometric change, however small, changes the
    # vertex set; a re-export of identical geometry (the v100 repro)
    # re-hashes identically (1e-3 mm rounding absorbs the STL float
    # noise).
    if candidate_fp[0] != parent_fp[0]:
        return None  # geometry changed → the check does NOT fire

    # -- Leg 2 (sanity): volume within tolerance. A fingerprint match
    # implies the vertex set is identical, so the volume is within
    # tolerance by construction; this leg guards against a degenerate
    # case and keeps the evidence honest (it names the volume the
    # operator can verify).
    if (
        parent_volume_mm3 is not None
        and candidate_vol is not None
        and abs(parent_volume_mm3) > 0.0
    ):
        vol_delta = abs(candidate_vol - parent_volume_mm3)
        vol_tol = max(
            _VOLUME_REL_TOL * abs(parent_volume_mm3), _VOLUME_ABS_FLOOR_MM3
        )
        if vol_delta > vol_tol:
            return None  # fingerprint matched but volume moved → not unchanged

    # Fingerprint match: the geometry is identical. Build the evidence
    # naming the unchanged numbers.
    if parent_volume_mm3 is not None:
        _vol = f"{parent_volume_mm3:,.2f}"
    else:
        _vol = "unknown (non-watertight)"
    if candidate_vol is not None:
        _cvol = f"{candidate_vol:,.2f}"
    else:
        _cvol = _vol
    _pf = parent_face_count if parent_face_count is not None else "unknown"
    evidence = (
        f"rendered mesh is unchanged from the parent version "
        f"(geometry fingerprint {parent_fp[0][:12]}…; "
        f"{parent_fp[1]} vertices; volume {_vol} mm3, "
        f"{_pf} faces; candidate: {_cvol} mm3)"
    )
    return evidence, UNCHANGED_INSTRUCTION
