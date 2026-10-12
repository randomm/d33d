"""STL load and volume helpers shared by the mesh post-checks (issue #436).

Pure moves out of ``d33d.unchanged_mesh_check``; that module re-exports both
names so existing import paths keep working.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["load_mesh", "mesh_volume"]


def load_mesh(path: str) -> Any | None:
    """Load the STL at ``path`` as the exporter wrote it.

    Both sides of a comparison go through this same load shape
    (``trimesh.load(process=False, force="mesh")``, no ``merge_vertices``),
    so the comparison is symmetric.

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
    except Exception:
        # Missing file, unreadable bytes, or any trimesh parse failure
        # (struct.error / IndexError on a malformed STL included) —
        # any load failure → abstain, never a raise. The exception is
        # logged with its traceback (the ``genus_from_stl`` pattern) and
        # only the path's BASENAME is logged — never the full path
        # (issue #356's host-path-disclosure convention).
        logger.warning(
            "unchanged-mesh check: failed to load %r", p.name, exc_info=True
        )
        return None


def mesh_volume(mesh: Any) -> float | None:
    """The mesh's signed volume.

    ``None`` when the volume cannot be read (the caller abstains on
    the volume leg).
    """
    try:
        return float(mesh.volume)
    except (ValueError, TypeError, RuntimeError, IndexError, ZeroDivisionError):
        return None
