"""The per-mesh pymeshfix repair used by ``d33d.part_mesh.parse_and_repair``.

Extracted from ``part_mesh.py`` (issue #375, the 500-line rule): the
repair machinery is a single self-contained function with no state, so it
lives in its own module. ``part_mesh`` imports it — never the other way
round (no circular import; this module imports nothing from ``d33d``
except the ``PartUploadError`` type it must passthrough, imported
inside the function to keep the import edge one-way even in case
``part_mesh``'s module body ever needs to grow).

Issue #395: the repair now runs in a **separate process** (ProcessPool-
Executor) with a timeout, so pymeshfix's GIL-holding C work can never
block the event loop or other requests. The module-level
``repair_with_pmf`` seam is preserved (monkeypatchable in tests). The
process boundary is a single-worker pool, created lazily on first use.
"""

from __future__ import annotations

import logging
from concurrent.futures import ProcessPoolExecutor

import numpy as np
import trimesh

logger = logging.getLogger(__name__)

#: The repair timeout in seconds. A mesh that pymeshfix genuinely cannot
#: repair in this time will hit the timeout path → 422, never a hang.
REPAIR_TIMEOUT_SECONDS = 120

# The ProcessPoolExecutor is created lazily (single worker) so the import
# of this module is cheap and test environments that monkeypatch
# repair_with_pmf never spawn a subprocess.
_executor: ProcessPoolExecutor | None = None


def _get_executor() -> ProcessPoolExecutor:
    """Lazily create (or return) the repair ProcessPoolExecutor."""
    global _executor
    if _executor is None:
        _executor = ProcessPoolExecutor(max_workers=1)
    return _executor


def _shutdown_executor() -> None:
    """Shut down the executor and reset it to ``None``.

    Called on repair timeout (the stuck worker is killed so the next
    ``_get_executor`` creates a fresh pool) and at test teardown.
    """
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=False)
        _executor = None


# ---------------------------------------------------------------------------
# Process-boundary worker function (must be top-level for pickling)
# ---------------------------------------------------------------------------


def _repair_in_process(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """The pymeshfix repair, run in a worker process.

    Receives raw numpy arrays (picklable — not trimesh objects) and
    returns the repaired (vertices, faces) tuple. The worker imports
    pymeshfix here (not at module top) so the parent process doesn't pay
    the import cost and so the monkeypatch seam in tests remains
    effective (tests monkeypatch ``d33d.part_repair.repair_with_pmf``,
    not this internal worker).

    Must be a top-level (picklable) function for ProcessPoolExecutor.
    """
    import pymeshfix as _pmf

    fix = _pmf.MeshFix(vertices, faces)
    fix.repair()
    repaired_verts = np.asarray(fix.points, dtype=np.float64)
    repaired_faces = np.asarray(fix.faces, dtype=np.int32)
    repaired = trimesh.Trimesh(repaired_verts, repaired_faces, process=False)
    trimesh.repair.fix_normals(repaired)
    return repaired.vertices, repaired.faces


def repair_with_pmf(
    mesh: trimesh.Trimesh, timeout: float | None = None
) -> trimesh.Trimesh:
    """The one-call pymeshfix repair (``MeshFix.repair`` → ``fix_normals``)
    on a single mesh.

    The single-body path uses it UNCHANGED (the early branch keeps
    single-body imports byte-for-byte identical to the historical one-call
    repair); the multi-body path runs it once PER connected body so repair
    cannot drop disconnected bodies (a single ``MeshFix.repair()`` on the
    merged multi-body mesh keeps only one component — issue #375).

    Issue #395: the repair runs in a **separate process** with a timeout
    (default ``REPAIR_TIMEOUT_SECONDS``), so pymeshfix's GIL-holding C
    code can never block the event loop. On timeout, a
    ``PartUploadError("repair timed out: …")`` is raised.

    Non-``PartUploadError`` failures are wrapped as ``PartUploadError(
    "repair failed: …")``; ``PartUploadError`` propagates verbatim (pinned
    by the in-repair-block propagation test).

    ``timeout``: override the default timeout (seconds). Useful in tests
    where a very short timeout can be injected.
    """
    from concurrent.futures import Future
    from concurrent.futures import TimeoutError as FutTimeoutError

    from d33d.part_mesh import PartUploadError  # local: keep the edge one-way

    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int32)

    pool = _get_executor()
    future: Future = pool.submit(_repair_in_process, verts, faces)

    try:
        repaired_verts, repaired_faces = future.result(timeout=timeout)
    except FutTimeoutError:
        # The worker process is stuck (pymeshfix on a pathological mesh).
        # ``future.cancel()`` only works if the task hasn't started; once
        # running it has no effect. We must kill the pool so the stuck
        # worker doesn't block all subsequent repairs (a single-worker
        # pool with one busy worker = every future repair queues behind
        # it and also times out). ``_shutdown_executor`` calls
        # ``shutdown(wait=False)`` which tears down the worker process;
        # the next ``_get_executor`` call creates a fresh, healthy pool.
        future.cancel()
        _shutdown_executor()
        raise PartUploadError(
            f"repair timed out: exceeded {timeout:.0f}s"
        ) from None
    except PartUploadError:
        # Passthrough: the worker raised a PartUploadError (propagates
        # verbatim through the future).
        raise
    except Exception as e:
        raise PartUploadError(f"repair failed: {e}") from e

    return trimesh.Trimesh(repaired_verts, repaired_faces, process=False)


__all__ = ["REPAIR_TIMEOUT_SECONDS", "repair_with_pmf"]
