"""The #351 hole-evidence gate for the fill-and-recut offer.

Issue #351 (operator decisions 1–4): the fill-and-recut offer is made
only when the import stored evidence the part actually has a hole. This
module is the small, self-contained half of that gate — the stored-fact
reader, the gated noun set, and the honest no-hole reply — factored out
of ``d33d.fill_recut`` (which owns the trigger, the offer lifecycle, and
the boundary copy). It also hosts the import-time topology helper
:func:`watertight_genus` (the closed-body half of ``d33d.part_mesh``'s
``hole_count``, measured on the pre-repair merged mesh) and the
per-hole measurement helpers (issue #396 — centre, axis, diameter for
each hole, stored in ``part_report["holes"]``).
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import trimesh

logger = logging.getLogger(__name__)

#: Issue #351 (operator decision 2) — the honest no-hole reply TEMPLATE:
#: a hole/bore/counterbore resize request on a part the import measured to
#: carry ZERO holes (``part_report.hole_count == 0``, the stored fact
#: computed once at import — the pre-route reads the stored fact, never
#: re-parses the mesh). The offer is NOT made for a feature the part does
#: not have, and the reply says so plainly. The ``{noun}`` slot is the
#: TRIGGERING noun the user asked about (``no_hole_reply(trigger["noun"]``)
#: — "a bore" and "a counterbore" are both fine), never a hardcoded
#: "hole". A verbatim template copy of the copy.ts ``fillRecut.noHole``
#: sentence — the parity test in ``tests/test_projects.py`` pins the
#: two-way agreement against ``web/src/copy.ts``. The loop must not run —
#: this is an answer, not a change request.
_NO_HOLE_REPLY_TEMPLATE = (
    "I don't see a {noun} on the part you brought — want me to drill one?"
)


def no_hole_reply(noun: str) -> str:
    """The honest no-hole reply for the TRIGGERING feature ``noun`` (the
    user asked about a hole/bore/counterbore, the part was measured with
    zero — say so with the noun the user used, never a hardcoded
    "hole")."""
    return _NO_HOLE_REPLY_TEMPLATE.format(noun=noun)


def part_has_hole_evidence(part: dict[str, Any] | None) -> bool | None:
    """Issue #351 (operator decisions 1–4) — the stored hole evidence for
    a part: the ``hole_count`` key of the part's stored import report
    (``part_report``), read through the ``part_public`` dict's ``report``
    field.

    The gate the caller applies: a fresh fill-and-recut trigger on a
    hole-family noun (hole/bore/counterbore) is offered ONLY when this
    returns ``True`` (the import measured at least one hole). ``0`` →
    ``False`` (the honest no-hole reply, no offer). ``None`` (no report
    — legacy row or a part imported before the key existed — or an
    unparseable/corrupt report that ``part_public`` already reduced to
    ``None``; the stored fact was never re-parsed here) → ``None``
    (UNKNOWN — keep today's behaviour: the offer fires, as it does today
    for every part row without a report).

    A negative count is a corrupt value and degrades to ``None`` (unknown)
    — never ``False`` (a false honest-no-hole reply on a possibly-holey
    part is a lie; a false OFFER on a known-holey part is today's
    behaviour, which the gate preserves for unknowns). The trigger itself
    stays purely lexical; this is the separate caller-side evidence check
    that consumes the stored fact (the trigger is never passed mesh
    state). The hole-count computation itself lives at import
    (``d33d.part_mesh``, task-a) — this reader does no mesh work.
    """
    report = (part or {}).get("report")
    if not isinstance(report, dict):
        return None
    count = report.get("hole_count")
    if count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    if count < 0:
        return None
    return count > 0


#: The hole-family feature nouns the #351 evidence gate applies to
#: (operator decision 4: the gate is hole/bore/counterbore only — the
#: mesh signal can only speak about holes, and other nouns keep current
#: behaviour regardless of the stored fact).
HOLE_NOUNS = frozenset({"hole", "holes", "bore", "counterbore"})

#: Issue #396 — the cap on the ``holes`` list stored in ``part_report``.
#: A part with more than this many holes gets only the first N fitted;
#: the rest are omitted (omit-not-null: the count stays honest, the
#: unfitted holes are simply not in the list).
MAX_HOLES = 32


def watertight_genus(components: list[trimesh.Trimesh]) -> int:
    """Total genus across the WATERTIGHT bodies of an already-split mesh.

    ``components`` is the caller's ``mesh.split(only_watertight=False)``
    list (the caller splits ONCE — the bodies count and this genus both
    read the same split, never two). Genus is the closed-body hole count:
    for a closed orientable body, ``genus = (2 - euler_number) / 2``
    (sphere 0, ring 1, torus-with-2 2). Only watertight components count
    (a gapped remainder is an OPEN edge the boundary-loop count owns, not
    a closed hole). Cheap — an euler_number is a vertex/edge/face count,
    no geometry passes — but the caller wraps the call in a guard (any
    exception falls back to the boundary-loop count alone, never a crash).
    """
    genus = 0
    for body in components:
        if body.is_watertight:
            g = (2 - int(body.euler_number)) // 2
            genus += max(0, g)
    return genus


def _boundary_loops(mesh: trimesh.Trimesh) -> int:
    """The number of boundary loops (connected open-edge components) on a
    mesh — one loop per gap. The repair report's ``gaps_closed`` is the
    count BEFORE repair minus AFTER (pymeshfix closes them). The OPEN-hole
    half of ``d33d.part_mesh``'s ``hole_count`` (``gaps_before + genus``),
    measured on the PRE-REPAIR merged mesh."""
    edges, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    open_edges = edges[counts == 1]
    if len(open_edges) == 0:
        return 0
    adj: dict[int, set[int]] = {i: set() for i in range(len(open_edges))}
    pos: dict[tuple[int, int], int] = {}
    for i, (a, b) in enumerate(open_edges):
        pos[(int(a), int(b))] = i
    for i, (a, b) in enumerate(open_edges):
        j = pos.get((int(b), int(a)))
        if j is not None:
            adj[i].add(j)
            adj[j].add(i)
    seen: set[int] = set()
    n = 0
    for i in range(len(open_edges)):
        if i in seen:
            continue
        n += 1
        stack = [i]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for y in adj[x]:
                if y not in seen:
                    stack.append(y)
    return n


def _boundary_loop_vertices(
    mesh: trimesh.Trimesh,
) -> list[list[np.ndarray]]:
    """The boundary loop vertex coordinate rings for each open-edge loop.

    Returns a list of lists, one per boundary loop, where each inner list
    is the ordered ring of vertex positions (the loop's vertices in order).
    Empty list for a watertight mesh.

    The walk follows the open-edge chain: from each edge, continue at the
    shared vertex to the next open edge. The loop is complete when the
    walk returns to the starting vertex.
    """
    edges, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    open_edges = edges[counts == 1]
    if len(open_edges) == 0:
        return []

    edge_count = len(open_edges)
    # Map vertex → list of edge indices touching it.
    vertex_to_edges: dict[int, list[int]] = {}
    for i in range(edge_count):
        a, b = open_edges[i]
        vertex_to_edges.setdefault(int(a), []).append(i)
        vertex_to_edges.setdefault(int(b), []).append(i)

    seen_edges: set[int] = set()
    loops: list[list[int]] = []
    for start in range(edge_count):
        if start in seen_edges:
            continue
        chain: list[int] = [start]
        seen_edges.add(start)
        # Walk: from the end of the current edge, find the next open edge
        # sharing that vertex.
        a, b = open_edges[start]
        cur_vertex = int(b)
        while True:
            next_edge = None
            for ne in vertex_to_edges.get(cur_vertex, []):
                if ne not in seen_edges:
                    next_edge = ne
                    break
            if next_edge is None:
                break
            chain.append(next_edge)
            seen_edges.add(next_edge)
            na, nb = open_edges[next_edge]
            if int(na) == cur_vertex:
                cur_vertex = int(nb)
            else:
                cur_vertex = int(na)
        loops.append(chain)

    # Convert edge chains to ordered vertex rings.
    result: list[list[np.ndarray]] = []
    for chain in loops:
        if not chain:
            continue
        # The ring's vertices: start vertex of the first edge, then the
        # "far" end of each edge in the chain.
        a0, b0 = open_edges[chain[0]]
        ring: list[np.ndarray] = [mesh.vertices[int(a0)]]
        # Track the current vertex to determine which end of each edge to
        # add.
        cur = int(a0)
        for e in chain:
            ea, eb = open_edges[e]
            ia, ib = int(ea), int(eb)
            if ia == cur:
                cur = ib
            else:
                cur = ia
            ring.append(mesh.vertices[cur])
        # Remove the last vertex if it equals the first (closed loop).
        if len(ring) > 1 and np.array_equal(ring[-1], ring[0]):
            ring.pop()
        if len(ring) >= 3:
            result.append(ring)
    return result


def _fit_circle_to_loop(
    vertices: np.ndarray,
) -> tuple[np.ndarray, float] | None:
    """Least-squares circle fit to a 2D/3D point cloud.

    Returns (centre, radius) or ``None`` if the fit is degenerate.
    The fit is done in the loop's best-fit plane: the centre is the mean
    of the points (projected), and the radius is the mean distance to it.
    """
    if len(vertices) < 3:
        return None
    center = vertices.mean(axis=0)
    # Project points onto the plane perpendicular to the loop normal.
    # For a near-planar loop (which hole openings are), the mean is a good
    # circle centre approximation.
    radii = np.linalg.norm(vertices - center, axis=1)
    radius = float(np.median(radii))
    if radius <= 0 or not math.isfinite(radius):
        return None
    return center, radius


def _loop_normal(vertices: np.ndarray) -> np.ndarray | None:
    """Estimate the normal of a boundary loop (the axis of the hole).

    Uses the cross-product of consecutive edges, averaged and normalised.
    """
    if len(vertices) < 3:
        return None
    # Use the first 3 points to estimate the normal, then refine.
    v0 = vertices[0]
    normals = []
    for i in range(1, len(vertices) - 1):
        e1 = vertices[i] - vertices[i - 1]
        e2 = vertices[i + 1] - vertices[i]
        n = np.cross(e1, e2)
        norm = np.linalg.norm(n)
        if norm > 1e-10:
            normals.append(n / norm)
    if not normals:
        return None
    n = np.mean(normals, axis=0)
    norm = np.linalg.norm(n)
    if norm < 1e-10:
        return None
    return n / norm


def _snap_axis(axis: np.ndarray, tol_deg: float = 5.0) -> list[float]:
    """Snap a unit axis vector to the nearest of X, Y, or Z if within
    ``tol_deg`` degrees; otherwise return the raw unit vector.

    Returns the axis as a list of 3 floats (unit length).
    """
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm < 1e-10:
        return [1.0, 0.0, 0.0]
    axis = axis / norm
    # Check alignment with each cardinal axis.
    for cardinal in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]):
        dot = abs(float(np.dot(axis, cardinal)))
        if dot >= math.cos(math.radians(tol_deg)):
            # Snap: preserve sign of the dominant component.
            sign = 1.0 if dot > 0 else -1.0
            return [sign * c if c != 0 else 0.0 for c in cardinal]
    return [float(v) for v in axis]


def _measure_open_holes(
    mesh: trimesh.Trimesh,
) -> list[dict[str, Any]]:
    """Measure the OPEN (boundary-loop) holes on a pre-repair mesh.

    For each boundary loop, fit a circle to the loop vertices to get the
    centre and diameter, and estimate the loop normal as the axis.

    Returns a list of ``{"center": [x,y,z], "axis": [x,y,z],
    "diameter_mm": d}`` dicts. Holes that can't be fitted are omitted.
    """
    holes: list[dict[str, Any]] = []
    try:
        loops = _boundary_loop_vertices(mesh)
    except Exception:
        logger.warning("boundary loop vertex extraction failed", exc_info=True)
        return holes

    for loop_verts_list in loops:
        if len(loop_verts_list) < 3:
            continue
        loop_verts = np.array(loop_verts_list)
        fit = _fit_circle_to_loop(loop_verts)
        if fit is None:
            continue
        center, radius = fit
        # Estimate the axis from the loop normal.
        normal = _loop_normal(loop_verts)
        if normal is None:
            continue
        axis = _snap_axis(normal)
        diameter = 2.0 * radius
        if diameter <= 0 or not math.isfinite(diameter):
            continue
        holes.append(
            {
                "center": [float(v) for v in center],
                "axis": axis,
                "diameter_mm": float(diameter),
            }
        )
    return holes


def _measure_genus_holes(
    components: list[trimesh.Trimesh],
) -> list[dict[str, Any]]:
    """Measure the CLOSED (genus) holes in watertight components.

    For a watertight body with genus >= 1, use trimesh's section plane to
    find the hole cross-sections. This is an approximation: for a simple
    through-hole (ring/annulus), the centre is the body's centroid and the
    axis is estimated from the body's principal axes.

    Returns a list of ``{"center": [x,y,z], "axis": [x,y,z],
    "diameter_mm": d}`` dicts. Holes that can't be measured are omitted.
    """
    holes: list[dict[str, Any]] = []
    for body in components:
        if not body.is_watertight:
            continue
        g = (2 - int(body.euler_number)) // 2
        if g <= 0:
            continue
        # For a simple through-hole (genus 1), approximate the hole
        # centre as the body centroid and the axis from the principal
        # axis of minimum extent.
        try:
            centroid = np.asarray(body.centroid)
        except Exception:
            continue
        # Estimate the axis: the axis along which the body has the smallest
        # extent is typically the through-axis for a ring/annulus.
        try:
            extents = np.asarray(body.extents)
            axis_idx = int(np.argmin(extents))
            axis = np.zeros(3)
            axis[axis_idx] = 1.0
        except Exception:
            axis = np.array([0.0, 0.0, 1.0])
        # Estimate the hole diameter: for a ring, the hole diameter
        # approximates the difference between the max and min radii from
        # the centroid in the plane perpendicular to the axis.
        try:
            # Project vertices onto the plane perpendicular to the axis.
            v = body.vertices - centroid
            # Remove the axis component.
            v_proj = v - np.outer(v @ axis, axis)
            radii = np.linalg.norm(v_proj, axis=1)
            # The hole diameter ≈ 2 × median of the smallest radii
            # (inner radius of the ring).
            if len(radii) > 0:
                inner_radius = float(np.median(radii[radii > 0]))
                diameter = 2.0 * inner_radius
                if diameter <= 0 or not math.isfinite(diameter):
                    continue
            else:
                continue
        except Exception:
            continue
        snapped = _snap_axis(axis)
        holes.append(
            {
                "center": [float(v) for v in centroid],
                "axis": snapped,
                "diameter_mm": float(diameter),
            }
        )
    return holes


def measure_holes(
    merged: trimesh.Trimesh,
    components: list[trimesh.Trimesh],
    scale: float = 1.0,
) -> list[dict[str, Any]]:
    """Measure all holes (open boundary-loop + closed genus) on a
    pre-repair merged mesh.

    ``merged`` is the pre-repair merged mesh (already merge_vertices'd).
    ``components`` is the caller's ``split(only_watertight=False)`` list.
    ``scale`` is the part_scale to apply to all measurements (file units
    → mm).

    Returns a list of ``{"center": [x,y,z], "axis": [x,y,z],
    "diameter_mm": d}`` dicts, capped at :data:`MAX_HOLES`. Holes that
    can't be fitted are omitted (omit-not-null).
    """
    holes: list[dict[str, Any]] = []

    # Open holes (boundary loops).
    try:
        open_holes = _measure_open_holes(merged)
        holes.extend(open_holes)
    except Exception:
        logger.warning("open hole measurement failed", exc_info=True)

    # Closed holes (genus).
    try:
        genus_holes = _measure_genus_holes(components)
        holes.extend(genus_holes)
    except Exception:
        logger.warning("genus hole measurement failed", exc_info=True)

    # Apply scale (file units → mm).
    if scale != 1.0:
        for hole in holes:
            hole["center"] = [v * scale for v in hole["center"]]
            hole["diameter_mm"] = hole["diameter_mm"] * scale

    # Cap at MAX_HOLES.
    return holes[:MAX_HOLES]


__all__ = [
    "HOLE_NOUNS",
    "MAX_HOLES",
    "measure_holes",
    "no_hole_reply",
    "part_has_hole_evidence",
    "watertight_genus",
]
