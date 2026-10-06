"""Part-mesh topology measurement (shared by the import path and #386).

Issue #395 introduces :func:`mesh_topology` — the single helper that
measures the merged pre-repair mesh's topology for the clean-skip
predicate AND provides the measurement set that ticket #386 (through-hole
genus check on the rendered candidate mesh) reuses.

Dual use:
- ``d33d.part_mesh.parse_and_repair`` calls it to decide whether the mesh
  is "clean" (0 boundary loops, all bodies watertight, winding
  consistent) and can skip pymeshfix entirely.
- Ticket #386 will call it on the rendered candidate mesh to measure
  through-hole genus for the print gate.

The helper returns a :class:`MeshTopology` — the caller owns interpretation.
"""

from __future__ import annotations

import logging
from typing import TypedDict

import trimesh

from d33d.part_holes import _boundary_loops, watertight_genus

logger = logging.getLogger(__name__)


class MeshTopology(TypedDict):
    """The ``mesh_topology`` return: the merged pre-repair mesh's topology.

    - ``boundary_loops`` (int): the number of open-edge loops (one per gap
      opening). 0 for a fully watertight mesh; ``-1`` when the count is
      UNKNOWN (a ``_boundary_loops`` failure — the mesh is NOT clean,
      so repair is taken, and the hole count treats the gaps as 0).
    - ``bodies`` (int): the total number of components (including non-
      watertight debris shells).
    - ``watertight_bodies`` (int): the number of watertight components.
    - ``winding_consistent`` (bool): True when the merged mesh's winding
      is globally consistent — the precondition for treating a watertight
      mesh as repair-free. For an OPEN mesh (``boundary_loops != 0``) it
      is False WITHOUT being measured (the mesh is open, so winding
      consistency cannot hold); the value then means "not measured; the
      mesh is open", not a measurement result.
    - ``genus`` (int): total genus across watertight components (closed
      through-holes). 0 for a plain box; 1 for a ring/annulus.
    """

    boundary_loops: int
    bodies: int
    watertight_bodies: int
    winding_consistent: bool
    genus: int


def mesh_topology(
    merged: trimesh.Trimesh, components: list[trimesh.Trimesh]
) -> MeshTopology:
    """Measure the topology of an already-split mesh.

    ``merged`` is the pre-repair merged mesh (already merge_vertices'd).
    ``components`` is the caller's ``split(only_watertight=False)`` list —
    the caller splits ONCE and passes the same list here, so both the
    bodies count and the genus read the same split.

    Any exception inside a measurement (boundary loops, winding, genus)
    is caught and logged, and the affected field degrades to a safe
    default: boundary_loops → ``-1`` (UNKNOWN — the mesh is NOT clean,
    so repair is taken; the hole count treats the gaps as 0), genus → 0,
    winding_consistent → False. For an open mesh (``boundary_loops != 0``
    — including the ``-1`` UNKNOWN count) ``is_winding_consistent`` is NOT
    evaluated at all: the mesh cannot be clean, and trimesh's check can
    raise or hang on such meshes, so the field is set to False with the
    meaning "not measured; the mesh is open".

    The measurement never raises.
    """
    # Boundary loops: guarded (any failure → -1, UNKNOWN — the mesh is NOT
    # clean, so repair is taken; treating it as 0 would wrongly skip it).
    try:
        boundary_loops = _boundary_loops(merged)
    except Exception:
        logger.warning(
            "boundary loop computation failed, marking mesh as not clean",
            exc_info=True,
        )
        boundary_loops = -1

    watertight_bodies = [c for c in components if c.is_watertight]

    # Winding consistency: skipped entirely for an OPEN mesh (boundary
    # loops != 0, including the -1 UNKNOWN count): the mesh cannot be
    # clean, so ``_is_clean`` is False either way, and trimesh's check is
    # unreliable (it can raise or hang) on open meshes. ``False`` here
    # means "not measured; the mesh is open", not a measurement result.
    # For a watertight mesh the check runs; any failure degrades to False
    # (the mesh is NOT clean — repair is taken).
    if boundary_loops != 0:
        winding_consistent = False
    else:
        try:
            winding_consistent = bool(merged.is_winding_consistent)
        except Exception:
            logger.warning(
                "winding consistency check failed, degrading to False",
                exc_info=True,
            )
            winding_consistent = False

    # Genus: reuse the existing watertight_genus, wrapped. Any exception
    # falls back to 0 (the count degrades, never crashes — issue #351).
    try:
        genus = watertight_genus(components)
    except Exception:
        logger.warning(
            "genus computation failed, falling back to boundary-loop count",
            exc_info=True,
        )
        genus = 0

    return MeshTopology(
        boundary_loops=boundary_loops,
        bodies=len(components),
        watertight_bodies=len(watertight_bodies),
        winding_consistent=winding_consistent,
        genus=genus,
    )


__all__ = ["MeshTopology", "mesh_topology"]
