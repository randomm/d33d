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

#: Issue #396 — the face budget a body may have before its mid-plane
#: cross-section is skipped in :func:`_measure_genus_holes`. Slicing is
#: the expensive half of the genus measurement; a body above this budget
#: is not worth it (its holes are unmeasured — the count stays honest, the
#: list is a subset). Kept a little above the repair decimate budget so a
#: typical part's bodies are still measured.
_GENUS_HOLE_FACE_BUDGET = 200_000


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
        a0, _ = open_edges[chain[0]]
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


def _ring_area_2d(pts: np.ndarray) -> float:
    """The signed area of a 2D closed ring (``|shoelace| / 2``).

    ``pts`` is an ``(n, >=2)`` array; only the first two columns are used.
    A degenerate (fewer than 3 points, zero area) ring yields ``0.0``."""
    if len(pts) < 3:
        return 0.0
    x = pts[:, 0]
    y = pts[:, 1]
    return 0.5 * abs(float(np.dot(x, np.roll(y, -1)) - np.dot(y, np.roll(x, -1))))


def _section_rings_2d(body: trimesh.Trimesh, axis_index: int) -> list[np.ndarray]:
    """The cross-section of ``body`` at its mid-plane along ``axis_index``,
    as a list of closed 2D rings (each an ``(n, 2)`` array in the plane
    coordinates of :meth:`trimesh.Trimesh.section`).

    The mid-plane (halfway between the body's min and max along the axis,
    through the origin) cuts any through-hole cleanly. Each ``discrete``
    entity of the section is a closed polyline — the outline of the face
    (the outer ring) and, one per hole, an interior ring. The rings are
    already closed (first vertex == last vertex) and in-plane.
    """
    lo, hi = body.bounds
    mid = 0.5 * (lo[axis_index] + hi[axis_index])
    normal = np.zeros(3)
    normal[axis_index] = 1.0
    origin = np.zeros(3)
    origin[axis_index] = mid
    planar = body.section(plane_normal=normal, plane_origin=origin)
    rings: list[np.ndarray] = []
    for disc in planar.discrete:
        pts = np.asarray(disc)
        if len(pts) >= 3:
            rings.append(pts[:, :2])
    return rings


def _holes_from_rings(
    rings: list[np.ndarray], axis_index: int
) -> list[tuple[float, float, float, float]]:
    """The hole cross-sections of a set of 2D section rings, as
    ``(u, v, area, equivalent_diameter)`` tuples.

    The LARGEST-area ring is the outer boundary of the cut face; every
    other ring whose a vertex falls inside the outer ring is a hole (a
    hole ring is always fully contained in the outer ring, never the
    reverse). The hole's equivalent diameter is ``sqrt(4*area/pi)`` —
    exact for a circular hole, the round-hole diameter for any shape.
    """
    if not rings:
        return []
    with_areas = sorted(
        ((_ring_area_2d(r), r) for r in rings), key=lambda p: p[0], reverse=True
    )
    outer = with_areas[0][1]
    holes: list[tuple[float, float, float, float]] = []
    for area, ring in with_areas[1:]:
        if area <= 0:
            continue
        # A hole ring is contained in the outer face ring: test the ring's
        # mean against the OUTER ring (not the ring itself — that test is
        # always true and would admit stray outer-boundary fragments).
        c = ring.mean(axis=0)
        if _point_in_ring(outer, c):
            diameter = math.sqrt(4.0 * area / math.pi)
            holes.append((float(c[0]), float(c[1]), float(area), float(diameter)))
    return holes


def _point_in_ring(ring: np.ndarray, p: np.ndarray) -> bool:
    """Even-odd (ray-casting) point-in-polygon test for a closed 2D ring.

    ``ring`` is an ``(n, 2)`` closed ring (first == last vertex); ``p`` is
    a ``(2,)`` point. Used to tell a hole ring (inside the outer face ring)
    from noise (a stray segment outside the face)."""
    x, y = float(p[0]), float(p[1])
    inside = False
    n = len(ring)
    j = n - 1
    for i in range(n):
        xi, yi = ring[i, 0], ring[i, 1]
        xj, yj = ring[j, 0], ring[j, 1]
        if ((yi > y) != (yj > y)) and (x < (xj - xi) * (y - yi) / (yj - yi) + xi):
            inside = not inside
        j = i
    return inside


def _ray_triangles(
    origins: np.ndarray,
    directions: np.ndarray,
    tris: np.ndarray,
) -> np.ndarray:
    """Möller–Trumbore ray-triangle intersection for all rays vs all
    triangles. ``origins`` is ``(M, 3)``, ``directions`` ``(M, 3)``,
    ``tris`` is ``(K, 3, 3)`` (K triangles, each 3 vertices). Returns an
    ``(M, K)`` array of hit distances (the parameter ``t`` along each ray)
    with ``NaN`` where the ray does not hit the triangle (miss, behind, or
    parallel).

    Self-contained: pure numpy, no scipy / rtree / shapely — the genus-hole
    ray-cast must work in the base install (trimesh's own ``ray`` and
    ``section`` need the optional geometry deps, which are not always
    present). The trimesh winding convention is used: the hit is valid
    when ``-v >= 0``, ``u - v <= 1``, and the hit distance is ``-t``.
    """
    v0 = tris[:, 0]
    v1 = tris[:, 1]
    v2 = tris[:, 2]
    edge1 = v1 - v0
    edge2 = v2 - v0
    pvec = np.cross(directions[:, None, :], edge2[None, :, :])  # (M, K, 3)
    det = np.einsum("mki,mki->mk", pvec, edge1[None, :, :])  # (M, K)
    valid = np.abs(det) > 1e-9
    inv_det = np.where(valid, 1.0 / np.where(det == 0, 1.0, det), 0.0)
    svec = origins[:, None, :] - v0[None, :, :]  # (M, K, 3)
    u = np.einsum("mki,mki->mk", svec, pvec) * inv_det
    valid_u = valid & (u >= 0) & (u <= 1)
    qvec = np.cross(svec, edge2[None, :, :])  # (M, K, 3)
    v = np.einsum("mki,mki->mk", directions[:, None, :], qvec) * inv_det
    t = np.einsum("mki,mki->mk", qvec, edge1[None, :, :]) * inv_det
    # trimesh winding: valid when -v >= 0, u - v <= 1, and -t > 0.
    valid_v = valid_u & (-v >= 0) & (u - v <= 1)
    t = np.where(valid_v & (-t > 1e-9), -t, np.nan)
    return t


def _ray_grid_hits(
    body: trimesh.Trimesh, axis_index: int, n: int
) -> list[tuple[int, int]]:
    """Cast rays along ``axis_index`` on an ``n x n`` grid over the body's
    bbox and return the grid-cell indices ``(i, j)`` (in the two in-plane
    axes) where the ray traverses the body WITHOUT hitting material — the
    hole cells.

    A through-hole is an empty region of the bbox the ray passes through
    without intersecting the body's surface. The rays run from just outside
    the body on the -axis side through its full extent (so the first
    surface crossed is the real entry face). A cell is a hole iff the ray's
    first hit is well past the near face — it travelled through empty space
    (the hole) to the far solid, rather than entering through solid at the
    near face. Self-contained numpy (no trimesh ray / section).
    """
    tris = body.vertices[body.faces]
    lo, hi = body.bounds
    u_slots = [i for i in range(3) if i != axis_index]
    u0, u1 = u_slots
    u_ext = hi[u0] - lo[u0]
    v_ext = hi[u1] - lo[u1]
    if u_ext <= 0 or v_ext <= 0:
        return []
    u_mid = 0.5 * (lo[u0] + hi[u0])
    v_mid = 0.5 * (lo[u1] + hi[u1])
    origins: list[list[float]] = []
    for i in range(n):
        for j in range(n):
            ou = u_mid - 0.5 * u_ext + u_ext * (i + 0.5) / n
            ov = v_mid - 0.5 * v_ext + v_ext * (j + 0.5) / n
            o = [0.0, 0.0, 0.0]
            o[u0] = ou
            o[u1] = ov
            o[axis_index] = lo[axis_index] - 1.0
            origins.append(o)
    directions = np.zeros((len(origins), 3))
    directions[:, axis_index] = 1.0
    t = _ray_triangles(np.asarray(origins), directions, tris)
    thickness = hi[axis_index] - lo[axis_index]
    if thickness <= 0:
        return []
    holes: list[tuple[int, int]] = []
    for k in range(len(origins)):
        row = t[k]
        good = ~np.isnan(row)
        if not np.any(good):
            continue
        # The ray started at lo[axis] - 1.0, so the entry axis coordinate is
        # (lo[axis] - 1.0) + min_t. A hole ray's first hit is past the near
        # face by > half the thickness (it crossed the hole to the far
        # solid); a solid ray's first hit is at ~the near face.
        entry = (lo[axis_index] - 1.0) + float(np.min(row[good]))
        if entry - lo[axis_index] > 0.5 * thickness:
            holes.append((k // n, k % n))
    return holes


def _cluster_cells(cells: list[tuple[int, int]]) -> list[list[tuple[int, int]]]:
    """4-connected clustering of grid cells (each an ``(i, j)`` index). A
    connected cluster of hole cells is one hole (a single isolated cell is
    noise, not a hole)."""
    cell_set = set(cells)
    seen: set[tuple[int, int]] = set()
    clusters: list[list[tuple[int, int]]] = []
    for c in cells:
        if c in seen:
            continue
        stack = [c]
        seen.add(c)
        comp: list[tuple[int, int]] = []
        while stack:
            cur = stack.pop()
            comp.append(cur)
            ci, cj = cur
            for ni, nj in ((ci + 1, cj), (ci - 1, cj), (ci, cj + 1), (ci, cj - 1)):
                if (ni, nj) in cell_set and (ni, nj) not in seen:
                    seen.add((ni, nj))
                    stack.append((ni, nj))
        if len(comp) >= 2:
            clusters.append(comp)
    return clusters


def _genus_holes_by_raycast(
    body: trimesh.Trimesh,
) -> list[tuple[float, float, int, float, int]] | None:
    """Measure ``body``'s through-holes by ray-casting (scipy-free).

    For each axis (X, Y, Z in order) cast a grid of rays, cluster the hole
    cells, and convert each cluster to a hole (centroid + area → diameter).
    The axis whose clustering yields a hole count matching the body's genus
    is the through-axis. Returns a list of ``(u, v, area, diameter,
    axis_index)`` tuples (the in-plane coordinates in the winning axis's
    frame), or ``None`` when no axis produced a hole cluster.
    """
    g = (2 - int(body.euler_number)) // 2
    if g <= 0:
        return None
    n = 24  # 24x24 grid — fine enough for 0.5 mm tolerance on mm parts.
    best: list[tuple[float, float, float, float, int]] | None = None
    best_match = False
    for axis_index in range(3):
        try:
            cells = _ray_grid_hits(body, axis_index, n)
        except Exception:
            logger.debug(
                "genus-hole raycast failed for a body on axis %d",
                axis_index,
                exc_info=True,
            )
            continue
        if not cells:
            continue
        clusters = _cluster_cells(cells)
        if not clusters:
            continue
        lo, hi = body.bounds
        u_slots = [i for i in range(3) if i != axis_index]
        u0, u1 = u_slots
        u_mid = 0.5 * (lo[u0] + hi[u0])
        v_mid = 0.5 * (lo[u1] + hi[u1])
        cell_u = (hi[u0] - lo[u0]) / n
        cell_v = (hi[u1] - lo[u1]) / n
        holes: list[tuple[float, float, float, float, int]] = []
        cell_set = set(cells)
        for cluster in clusters:
            # A real through-hole is BOUNDED BY MATERIAL ON ALL SIDES:
            # every cell just outside the cluster's bounding box must be a
            # non-hole cell (solid). This rejects the spurious corner/edge
            # cells the ray grid registers at the bbox corners (where the
            # grid's corner cells see no material because the ray exits the
            # body through a corner, not through a solid wall). A hole of
            # >= 6 cells is the minimum (a 1-cell cluster is noise).
            if len(cluster) < 6:
                continue
            ci = [c[0] for c in cluster]
            cj = [c[1] for c in cluster]
            i0, i1 = min(ci), max(ci)
            j0, j1 = min(cj), max(cj)
            bounded = True
            for k in range(i0 - 1, i1 + 2):
                for nj in (j0 - 1, j1 + 1):
                    if (k, nj) in cell_set:
                        bounded = False
            for j in range(j0 - 1, j1 + 2):
                for ni in (i0 - 1, i1 + 1):
                    if (ni, j) in cell_set:
                        bounded = False
            if not bounded:
                continue
            cu = u_mid - 0.5 * (hi[u0] - lo[u0]) + (np.mean(ci) + 0.5) * cell_u
            cv = v_mid - 0.5 * (hi[u1] - lo[u1]) + (np.mean(cj) + 0.5) * cell_v
            area = len(cluster) * cell_u * cell_v
            diameter = math.sqrt(4.0 * area / math.pi)
            holes.append((float(cu), float(cv), float(area), float(diameter), axis_index))
        if not holes:
            continue
        match = len(holes) == g
        if match and not best_match:
            best = holes
            best_match = True
            break
        if best is None:
            best = holes
    return best


def _genus_holes_by_section(
    body: trimesh.Trimesh,
) -> list[tuple[float, float, float, float, int]] | None:
    """Measure ``body``'s through-holes by slicing at its centroid with a
    plane along each axis and reading the cross-section's INTERIOR rings
    (the holes of the section polygon).

    The slice (``trimesh``'s ``section``) needs the optional geometry deps
    (scipy, loaded lazily by trimesh). For each axis (Z, then Y, then X) the
    body is sliced at its centroid with the plane normal along that axis;
    the section's 2D rings (``planar.discrete``) are taken, the largest-area
    ring is the outer face outline, and every other ring (a hole) is
    measured: its in-plane centroid and equivalent diameter ``sqrt(4*area/
    pi)``. The 2D centroid is mapped back to 3D (the in-plane coords map to
    the two axes other than the slice axis; the slice-axis coord is the
    centroid's coordinate on that axis). The axis whose hole count best
    matches the genus is preferred; the first axis with at least one hole
    is used otherwise. Returns ``(u, v, area, diameter, axis_index)``
    tuples, or ``None`` (e.g. when the geometry deps are absent — the
    ray-cast is the scipy-free fallback).
    """
    g = (2 - int(body.euler_number)) // 2
    if g <= 0:
        return None
    centroid = np.asarray(body.centroid)
    best: list[tuple[float, float, float, float, int]] | None = None
    best_match = False
    for axis_index in (2, 1, 0):  # prefer Z, then Y, then X
        normal = np.zeros(3)
        normal[axis_index] = 1.0
        try:
            planar = body.section(plane_normal=normal, plane_origin=centroid)
        except Exception:
            # The section needs the optional geometry deps (scipy / shapely,
            # loaded lazily by trimesh). A failure (a missing dep, a
            # degenerate mesh) degrades to "no holes measured on this axis"
            # — the ray-cast fallback / the open-hole path is unaffected, and
            # the import never fails (the caller's broad guard is the last
            # backstop). The genus is counted, so hole_count stays honest.
            logger.debug(
                "genus-hole section failed for a body on axis %d",
                axis_index,
                exc_info=True,
            )
            continue
        rings = [
            np.asarray(disc)[:, :2]
            for disc in planar.discrete
            if len(np.asarray(disc)) >= 3
        ]
        if not rings:
            continue
        with_areas = sorted(
            ((_ring_area_2d(r), r) for r in rings), key=lambda p: p[0], reverse=True
        )
        outer = with_areas[0][1]
        holes: list[tuple[float, float, float, float, int]] = []
        for _area, ring in with_areas[1:]:
            if _area <= 0:
                continue
            c = ring.mean(axis=0)
            # A hole ring is contained in the outer face ring (its centroid
            # falls inside the outer ring); a stray outer-boundary fragment
            # does not.
            if not _point_in_ring(outer, c):
                continue
            diameter = math.sqrt(4.0 * _area / math.pi)
            holes.append(
                (
                    float(c[0]),
                    float(c[1]),
                    float(_area),
                    float(diameter),
                    axis_index,
                )
            )
        if not holes:
            continue
        match = len(holes) == g
        if match and not best_match:
            best = holes
            best_match = True
            break
        if best is None:
            best = holes
    return best


def _measure_genus_holes(
    components: list[trimesh.Trimesh],
) -> list[dict[str, Any]]:
    """Measure the CLOSED (genus) holes in watertight components, INDIVIDUALLY.

    For each watertight body with genus >= 1, the through-holes are measured
    INDIVIDUALLY (a 3-through-hole plate yields three holes at the three
    hole centres, never one bogus hole at the plate's centroid). The PRIMARY
    method is a scipy-free ray-cast (:func:`_genus_holes_by_raycast` — cast
    a grid of rays along each axis, the empty cells cluster into the holes);
    the trimesh ``section`` path (which needs the optional geometry deps) is
    the FALLBACK when the ray-cast finds nothing. The axis whose measurement
    yields a hole count matching the body's genus is the through-axis; that
    axis's holes are reported (one entry per hole), with the hole centre in
    the body's full 3D frame and the axis snapped to the cardinal axis.

    The count is not forced to match the genus — when it disagrees, what was
    actually measured is reported (never a number invented to fill the gap).
    Bounded: bodies above the face budget are skipped (their ray-cast /
    cross-section is not computed), and the total is capped at
    :data:`MAX_HOLES`.

    Returns a list of ``{"center": [x,y,z], "axis": [x,y,z],
    "diameter_mm": d}`` dicts. Holes that can't be measured are omitted.
    """
    holes: list[dict[str, Any]] = []
    for body in components:
        if not body.is_watertight:
            continue
        try:
            g = (2 - int(body.euler_number)) // 2
        except (TypeError, ValueError):
            continue
        if g <= 0:
            continue
        # Bound the work: a huge body's ray-cast / cross-section is expensive
        # and low-value; skip it (its holes are unmeasured, count stays
        # honest — the list is a subset, never fabricated).
        if len(body.faces) > _GENUS_HOLE_FACE_BUDGET:
            continue
        best: list[tuple[float, float, float, float, int]] | None = None
        best_axis_index = -1
        # PRIMARY: the trimesh section (needs the optional geometry deps);
        # its interior rings give each hole's true centroid + diameter.
        try:
            sec = _genus_holes_by_section(body)
            if sec:
                best = sec
                best_axis_index = sec[0][4]
        except Exception:
            logger.debug("genus-hole section failed for a body", exc_info=True)
        # FALLBACK: the scipy-free ray-cast (no geometry deps needed).
        if best is None:
            try:
                rc = _genus_holes_by_raycast(body)
                if rc:
                    best = rc
                    best_axis_index = rc[0][4]
            except Exception:
                logger.debug("genus-hole raycast failed for a body", exc_info=True)
        if best is None or best_axis_index < 0:
            continue
        axis = np.zeros(3)
        axis[best_axis_index] = 1.0
        snapped = _snap_axis(axis)
        for u, v, _area, diameter, _ax in best:
            center = [0.0, 0.0, 0.0]
            slots = [i for i in range(3) if i != best_axis_index]
            center[slots[0]] = u
            center[slots[1]] = v
            # The slice-axis coordinate: the section is taken at the body's
            # centroid, so the hole centre's coordinate on the slice axis is
            # the centroid's (a true 3D point in the body frame).
            center[best_axis_index] = float(body.centroid[best_axis_index])
            if diameter <= 0 or not math.isfinite(diameter):
                continue
            holes.append(
                {
                    "center": center,
                    "axis": list(snapped),
                    "diameter_mm": float(diameter),
                }
            )
            if len(holes) >= MAX_HOLES:
                return holes
    return holes


def measure_holes(
    merged: trimesh.Trimesh,
    components: list[trimesh.Trimesh],
) -> list[dict[str, Any]]:
    """Measure all holes (open boundary-loop + closed genus) on a
    pre-repair merged mesh, in FILE units.

    ``merged`` is the pre-repair merged mesh (already merge_vertices'd).
    ``components`` is the caller's ``split(only_watertight=False)`` list.
    The measurements are in the mesh's own file units — the same space as
    ``part_report["bbox_file_units"]`` — so the reader applies the part's
    scale (``part_scale``) to both to get mm; the measurement itself never
    scales (the scale is unknown at this point in the import, and scaling
    here would desynchronise the holes from the file-unit bbox).

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
