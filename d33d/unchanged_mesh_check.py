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
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "UNCHANGED_INSTRUCTION",
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


def _fingerprint_bytes(values: list[Any], n: int) -> bytes:
    """Pack ``values`` (already rounded to 1e-3 mm) as big-endian
    float64. ``n`` is the total element count so the byte string is
    length-unambiguous."""
    out = bytearray()
    for v in values:
        out += int(v).to_bytes(8, "big", signed=True)
    return bytes(out)


def mesh_fingerprint(mesh: Any) -> tuple[str, int] | None:
    """The mesh's geometry fingerprint:
    ``(sha256(sorted rounded vertex set), vertex_set_size)``.

    The vertex set is each vertex rounded to 1e-3 mm, deduplicated
    (STLs repeat vertices per face), and sorted lexicographically, so
    the hash is order-independent, re-export-noise-insensitive, and
    multi-component-safe (it is the union of every component's
    vertices — no per-component assumption). A real geometric change
    (a moved hole, a resized bore, a re-triangulation that moves or
    adds a vertex) changes the vertex set; an OpenSCAD re-export of an
    unchanged model (the v100 repro) re-hashes identically (the 1e-3
    mm rounding absorbs the STL float noise). A different
    triangulation of the same shape (same vertex set, different
    connectivity) re-hashes identically — the check errs toward
    "changed", the safe direction (a correct edit is never blocked by
    a re-triangulation).

    ``None`` when the mesh has zero vertices (a load failure — the
    caller's empty_model gate already handles that shape) or the
    vertices cannot be read.
    """
    try:
        verts = mesh.vertices
        if len(verts) <= 0:
            return None
        rounded = {
            (
                round(float(x) / _ROUND_MM),
                round(float(y) / _ROUND_MM),
                round(float(z) / _ROUND_MM),
            )
            for (x, y, z) in verts
        }
        sorted_set = sorted(rounded)
        vbytes = _fingerprint_bytes(
            [c for v in sorted_set for c in v], len(sorted_set) * 3
        )
        return (hashlib.sha256(vbytes).hexdigest(), len(sorted_set))
    except (ValueError, TypeError, RuntimeError, IndexError, OverflowError):
        return None


def _load_mesh(path: str) -> Any | None:
    """Load the STL at ``path`` for the unchanged-mesh comparison.

    ``None`` on any load failure (missing file, trimesh error) — the
    caller then abstains (a fabricated baseline would make the gate
    lie). The load is the SAME shape on both sides (``process=False``,
    ``force="mesh"``) so the fingerprint comparison is symmetric; NO
    ``merge_vertices`` is applied here — the mesh is measured as the
    exporter wrote it, on both sides.
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
    """The mesh's volume, or ``None`` when it cannot be read.

    ``mesh.volume`` is the SIGNED volume (trimesh sums per-face signed
    tetrahedra) and RAISES only for a mesh whose winding is too
    inconsistent to sum — the caller then abstains on the volume sanity
    leg (the fingerprint comparison is the primary signal). A
    zero-volume mesh (a degenerate shape) returns ``0.0`` — the caller
    treats ``abs(parent) <= 0`` as "the volume leg abstains".
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

    Returns ``None`` (the check abstains — the candidate is allowed to
    pass on its own merits) when:

    - no parent baseline is available: neither ``parent_fingerprint``
      nor a loadable ``parent_stl``. The seam passes the parent's
      pre-computed fingerprint (measured off the same mesh load the
      genus came from); the path-based fallback loads the parent's
      ``model.stl`` and fingerprints it here. When NEITHER is present
      (a v1 design, a 3MF import, a missing file), the check abstains
      — a fabricated baseline would make the gate lie.
    - ``candidate_stl`` is ``None`` or unloadable (the loop's render
      has no STL, or it cannot be read).
    - Either mesh's fingerprint cannot be computed (a zero-vertex load
      is a load failure — the caller's empty_model gate already
      handles that shape).

    Returns ``(evidence, instruction)`` when the candidate's rendered
    mesh has the SAME geometry fingerprint as the parent's (the vertex
    set is equal, and — as a sanity check — the volume is within
    tolerance): the evidence names the unchanged geometry (the loop's
    repair dict carries it to the next iteration's ``REPAIR`` block),
    and the instruction is :data:`UNCHANGED_INSTRUCTION`.

    A DIFFERENT fingerprint (a different vertex set) means the mesh
    changed — however small the change (a moved hole, a resized bore,
    a re-triangulation that moves or adds a vertex) — the check
    returns ``None`` and the candidate is allowed to pass on its own
    merits.
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
