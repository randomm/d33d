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

The helper returns a small flat dict — the caller owns interpretation.
"""

from __future__ import annotations

from typing import Any

import trimesh

from d33d.part_holes import _boundary_loops, watertight_genus


def mesh_topology(
    merged: trimesh.Trimesh, components: list[trimesh.Trimesh]
) -> dict[str, Any]:
    """Measure the topology of an already-split mesh.

    ``merged`` is the pre-repair merged mesh (already merge_vertices'd).
    ``components`` is the caller's ``split(only_watertight=False)`` list —
    the caller splits ONCE and passes the same list here, so both the
    bodies count and the genus read the same split.

    Returns a dict with:

    - ``boundary_loops`` (int): the number of open-edge loops (one per gap
      opening). 0 for a fully watertight mesh.
    - ``bodies`` (int): the total number of components (including non-
      watertight debris shells).
    - ``watertight_bodies`` (int): the number of watertight components.
    - ``winding_consistent`` (bool): True when the merged mesh's winding
      (vertex ordering) is globally consistent — the precondition for
      treating a watertight mesh as repair-free.
    - ``genus`` (int): total genus across watertight components (closed
      through-holes). 0 for a plain box; 1 for a ring/annulus.

    The winding check uses trimesh's ``is_winding_consistent`` which
    verifies all faces have the same normal-orientation parity. A mesh
    with inconsistent winding is NOT clean even if boundary-loop-free —
    pymeshfix would be needed to fix the winding.

    Any exception inside the genus or winding check is caught and the
    affected field degrades to a safe default (genus → 0, winding_consistent
    → False) — the measurement never raises.
    """
    boundary_loops = _boundary_loops(merged)

    watertight_bodies = [c for c in components if c.is_watertight]

    # Winding consistency: trimesh's check returns a bool.
    try:
        winding_consistent = bool(merged.is_winding_consistent)
    except Exception:
        winding_consistent = False

    # Genus: reuse the existing watertight_genus, wrapped.
    try:
        genus = watertight_genus(components)
    except Exception:
        genus = 0

    return {
        "boundary_loops": boundary_loops,
        "bodies": len(components),
        "watertight_bodies": len(watertight_bodies),
        "winding_consistent": winding_consistent,
        "genus": genus,
    }


__all__ = ["mesh_topology"]
