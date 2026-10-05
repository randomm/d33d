"""The per-mesh pymeshfix repair used by ``d33d.part_mesh.parse_and_repair``.

Extracted from ``part_mesh.py`` (issue #375, the 500-line rule): the
repair machinery is a single self-contained function with no state, so it
lives in its own module. ``part_mesh`` imports it — never the other way
round (no circular import; this module imports nothing from ``d33d``
except the ``PartUploadError`` type it must passthrough, imported
inside the function to keep the import edge one-way even in case
``part_mesh``'s module body ever needs to grow).
"""

from __future__ import annotations

import numpy as np
import trimesh


def repair_with_pmf(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """The one-call pymeshfix repair (``MeshFix.repair`` → ``fix_normals``)
    on a single mesh.

    The single-body path uses it UNCHANGED (the early branch keeps
    single-body imports byte-for-byte identical to the historical one-call
    repair); the multi-body path runs it once PER connected body so repair
    cannot drop disconnected bodies (a single ``MeshFix.repair()`` on the
    merged multi-body mesh keeps only one component — issue #375).
    Non-``PartUploadError`` failures are wrapped as ``PartUploadError(
    "repair failed: …")``; ``PartUploadError`` propagates verbatim (pinned
    by the in-repair-block propagation test)."""
    import pymeshfix as _pmf

    from d33d.part_mesh import PartUploadError  # local: keep the edge one-way

    try:
        fix = _pmf.MeshFix(
            mesh.vertices.astype(np.float64), mesh.faces.astype(np.int32)
        )
        fix.repair()
        repaired = trimesh.Trimesh(
            np.asarray(fix.points, dtype=np.float64),
            np.asarray(fix.faces, dtype=np.int32),
            process=False,
        )
    except PartUploadError:
        raise
    except Exception as e:
        raise PartUploadError(f"repair failed: {e}") from e
    trimesh.repair.fix_normals(repaired)
    return repaired


__all__ = ["repair_with_pmf"]
