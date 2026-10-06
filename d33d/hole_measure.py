"""Closed (genus) hole measurement by mid-plane cross-section.

Issue #396: a watertight body with holes of a closed genus (a plate with
three through-holes) is measured by slicing the body at its mid-plane
along each cardinal axis and reading the cross-section polygon's
INTERIOR rings — each interior ring is one hole (its centroid is the
hole's centre, its equivalent diameter ``sqrt(4 * area / pi)``).

The section is produced by :meth:`trimesh.Trimesh.section` — which
REQUIRES the runtime dependencies ``scipy`` and ``shapely`` (a hard
dependency of the project, not an optional extras). A missing
dependency therefore PROPAGATES as an :class:`ImportError` out of
:func:`measure_genus_holes` — the caller's broad ``except Exception``
in ``d33d.part_mesh.parse_and_repair`` still omits the ``holes`` list
(the import never fails, the hole_count stays honest) — but the
loud-failure contract is pinned by ``test_scipy_and_shapely_importable``
and, in practice, cannot trigger because the deps are installed.

Only GENUINE geometric failures (a degenerate mesh, a section trimesh
itself refuses) degrade: they are logged and the body's holes are
simply unmeasured. There is NO ray-cast or any other fallback — the
broken ray-cast from the first #396 pass (which reported phantom
holes) has been deleted.
"""

from __future__ import annotations

import logging
import math
from typing import Any

import numpy as np
import trimesh

logger = logging.getLogger(__name__)


def _snap_axis(axis: np.ndarray, tol_deg: float = 5.0) -> list[float]:
    """Snap a unit axis vector to the nearest of X, Y, or Z if within
    ``tol_deg`` degrees; otherwise return the raw unit vector.

    Returns the axis as a list of 3 floats (unit length). Local to this
    module (the section measurement is the only user — the open-hole
    path keeps its own copy in :mod:`d33d.part_holes`).
    """
    axis = np.asarray(axis, dtype=float)
    norm = np.linalg.norm(axis)
    if norm < 1e-10:
        return [1.0, 0.0, 0.0]
    axis = axis / norm
    for cardinal in ([1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]):
        dot = abs(float(np.dot(axis, cardinal)))
        if dot >= math.cos(math.radians(tol_deg)):
            sign = 1.0 if dot > 0 else -1.0
            return [sign * c if c != 0 else 0.0 for c in cardinal]
    return [float(v) for v in axis]


def _section_interior_rings(
    body: trimesh.Trimesh,
    axis_index: int,
) -> tuple[list[tuple[float, float, float]], list[float]]:
    """The cross-section of ``body`` at its mid-plane along
    ``axis_index``, as a list of ``(u, v, diameter)`` tuples for the
    interior rings (one per through-hole).

    The slice is taken at the body's mid-plane along that axis (halfway
    between the min and max bound — the plane that cuts every
    through-hole). The section is projected to a 2D plane via
    :meth:`trimesh.path.path.Path3D.to_planar` (the
    ``(entities, transform)`` form — the ``polygons_full`` path
    requires the ``rtree`` package, which is NOT a project dependency,
    so the contours are read from ``polygons_closed`` directly: trimesh
    hands back one closed ``shapely.geometry.Polygon`` per section
    contour, the exterior outline plus one polygon per hole).

    Returns a list of ``(u, v, diameter)`` tuples for the INTERIOR
    rings (the holes — every contour except the largest-area one,
    which is the outer face outline). The in-plane coordinates are in
    the 2D plane's frame; the caller maps them back to the body's 3D
    frame (the two in-plane coords fill the non-slice axes).

    Raises: :class:`ImportError` propagates (a missing scipy / shapely
    is a deployment error, not a geometric failure); a degenerate
    mesh (a section trimesh cannot build) is a geometric failure and
    yields an empty list.
    """
    lo, hi = body.bounds
    mid = 0.5 * (float(lo[axis_index]) + float(hi[axis_index]))
    normal = np.zeros(3)
    normal[axis_index] = 1.0
    # The plane origin is the body's centroid (the mid-plane through the
    # centroid cuts every through-hole cleanly; the 2D frame's origin is
    # this point, so the in-plane coordinates are relative to it).
    centroid = np.asarray(body.centroid, dtype=float)
    origin = centroid.copy()
    origin[axis_index] = mid
    planar = body.section(plane_normal=normal, plane_origin=origin)
    flat, _transform = planar.to_2D()
    polygons = [p for p in flat.polygons_closed if abs(p.area) > 1e-12]
    if not polygons:
        return []
    # The 2D frame's origin is the PLANE ORIGIN (the body's centre on the
    # in-plane axes, 0 on the slice axis) — the in-plane coordinates are
    # relative to it. The frame's local X axis is the transform's first
    # column, its local Y axis the second, so a 2D point (u, v) maps to
    # the 3D point ``origin + u * col0 + v * col1`` (the two in-plane
    # components are the body-frame coordinates on the two non-slice
    # axes).
    _transform = np.asarray(_transform, dtype=float)
    col0 = _transform[:3, 0]
    col1 = _transform[:3, 1]
    or3 = np.asarray(origin, dtype=float)
    slots = [i for i in range(3) if i != axis_index]
    # The largest-area contour is the outer face outline; every other
    # contour is an interior ring — one per hole.
    polygons.sort(key=lambda p: abs(p.area), reverse=True)
    rings: list[tuple[float, float, float]] = []
    for poly in polygons[1:]:
        rp = poly.representative_point()
        u, v = float(rp.x), float(rp.y)
        p3 = or3 + u * col0 + v * col1
        diameter = math.sqrt(4.0 * abs(poly.area) / math.pi)
        rings.append((float(p3[slots[0]]), float(p3[slots[1]]), diameter))
    return rings


def measure_genus_holes(
    body: trimesh.Trimesh,
) -> list[dict[str, Any]]:
    """Measure the through-holes of ONE watertight body (genus >= 1) by
    mid-plane cross-section.

    For each axis (Z, then Y, then X) the body is sliced at its
    mid-plane; the section's interior rings are the candidate holes.
    The first axis whose interior-ring count is >= 1 is used,
    preferring the axis whose count EQUALS the body's genus. Each hole
    is returned as ``{"center": [x, y, z], "axis": [x, y, z],
    "diameter_mm": d}`` with the centre in the body's 3D frame (the
    slice-axis coordinate is the mid-plane offset — the hole is a
    through-hole, so its centre sits in the mid-plane).

    :class:`ImportError` (a missing scipy / shapely) propagates
    (loud-failure contract). Geometric failures (a degenerate
    section) degrade per-axis to "no holes on this axis" — the body
    simply yields no measured holes.
    """
    try:
        genus = (2 - int(body.euler_number)) // 2
    except (TypeError, ValueError):
        return []
    if genus <= 0:
        return []
    best: list[tuple[float, float, float, int]] | None = None
    for axis_index in (2, 1, 0):  # prefer Z, then Y, then X
        try:
            rings = _section_interior_rings(body, axis_index)
        except ImportError:
            # A missing scipy / shapely is NOT a geometric failure —
            # it propagates (loud-failure contract; the tests pin the
            # import, and the caller's broad guard still omits the
            # holes list so the import itself never fails).
            raise
        except Exception:
            # A genuine geometric failure (degenerate mesh, a section
            # trimesh cannot build): degrade to "no holes on this
            # axis" — never a fabricated number, never a crash.
            logger.warning(
                "genus-hole section failed for a body on axis %d",
                axis_index,
                exc_info=True,
            )
            continue
        if not rings:
            continue
        if best is None or (len(rings) == genus and len(best) != genus):
            best = [(u, v, d, axis_index) for (u, v, d) in rings]
        if len(best) == genus:
            break
    if not best:
        return []
    # The 3D frame: the slice-axis coordinate is the mid-plane offset
    # (a through-hole's centre sits in the mid-plane); the in-plane
    # coordinates map to the two other axes via the mapping.
    holes: list[dict[str, Any]] = []
    for u, v, diameter, axis_index in best:
        if diameter <= 0 or not math.isfinite(diameter):
            continue
        _lo, _hi = body.bounds
        mid = 0.5 * (float(_lo[axis_index]) + float(_hi[axis_index]))
        slots = [i for i in range(3) if i != axis_index]
        center = [0.0, 0.0, 0.0]
        center[slots[0]] = float(u)
        center[slots[1]] = float(v)
        center[axis_index] = mid
        axis = np.zeros(3)
        axis[axis_index] = 1.0
        holes.append(
            {
                "center": center,
                "axis": _snap_axis(axis),
                "diameter_mm": float(diameter),
            }
        )
    return holes
