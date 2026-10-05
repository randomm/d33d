"""Top-level test stubs for the issue #395 repair-process tests.

``d33d.part_repair.repair_with_pmf`` pickles the parent-resolved
``_REPAIR_WORKER`` callable by reference to the spawn child (a fresh
interpreter), so a stub pointed at from the parent must be a top-level
function of an importable module. This module lives under ``tests/`` —
the production package ships no test-only code — and the spawn child can
import it because it inherits the parent's ``sys.path`` (pytest puts the
repository root on it).

Each stub takes ``(vertices, faces)`` (the same shape
``d33d.part_repair._repair_in_process`` does) and returns the repaired
``(vertices, faces)`` arrays — the parent rebuilds a ``Trimesh`` from
them, so a stub may return the input arrays unchanged (a no-op for a
valid mesh).
"""

from __future__ import annotations

import numpy as np
import trimesh


def _noop_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A no-op repair: returns the input arrays unchanged (the parent
    rebuilds the ``Trimesh`` from them — a no-op for a valid mesh).

    The repair seam is exercised (``repair_with_pmf`` → spawn child →
    this stub), but the stub is instant, so the test measures the
    boundary, not pymeshfix.
    """
    return vertices, faces


def _busy_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A GIL-holding stub (issue #395 off-event-loop test).

    Runs a C-level busy loop (``sum(range(200_000_000))`` — about 1.1 s
    on the CI/dev hardware) that does NOT release the GIL for its full
    duration (a pure-Python ``for`` loop does — CPython's eval loop
    switches threads every ~5 ms, so a Python-level busy loop in a thread
    does NOT starve a concurrent asyncio ticker, and a test using one
    would pass on main's ``asyncio.to_thread`` approach — exactly the bug
    class being fixed). The stub runs INSIDE the spawn child, so the GIL
    held is the CHILD's — the parent's event loop must be unaffected by
    the same proof: with a thread-based ``to_thread`` implementation, the
    stub would run in a parent thread holding the parent's GIL and the
    concurrent request's tick gaps would balloon past 200 ms; with
    process isolation, the parent's GIL is never held by the stub and the
    ticker's gaps stay small.
    """
    sum(range(200_000_000))  # ~1.1 s, GIL held for the whole duration
    return vertices, faces


def _slow_spin_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A long-running stub for the real-timeout test.

    Spins (a C-level busy loop, sized to run longer than the injected
    ``timeout=1``) so the real process boundary's ``poll(timeout)`` fires
    and the parent kills the child. The returned arrays are never reached
    — the child is killed first — but the shape matches the worker
    contract so the same stub could be reused for a non-timeout case.
    """
    sum(range(1_000_000_000))  # ~5 s, well past the injected 1 s timeout
    return vertices, faces


def _raise_error_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A child-side failure stub (issue #395 lens round 2: sanitized child
    errors). Raises a ``RuntimeError`` whose message carries a marker the
    test asserts does NOT leak into the parent's wrapped
    ``PartUploadError`` (which must carry only the exception's type
    name). The full payload is logged in the parent instead.
    """
    raise RuntimeError("child-side failure: LEAK_MARKER must not appear in the 422")


def _bodies_worker(bodies):
    """A batched worker for ``repair_bodies_with_pmf`` (the multi-body
    seam): ``bodies`` is a list of ``(vertices, faces)`` raw-array pairs
    (already decimated in the parent); each is repaired in order (the
    per-body MeshFix + fix_normals semantics of issue #375) and the
    repaired ``(vertices, faces)`` list is returned.
    """
    import pymeshfix as _pmf

    repaired: list[tuple] = []
    for verts, faces in bodies:
        fix = _pmf.MeshFix(verts, faces)
        fix.repair()
        rep_verts = np.asarray(fix.points, dtype=np.float64)
        rep_faces = np.asarray(fix.faces, dtype=np.int32)
        m = trimesh.Trimesh(rep_verts, rep_faces, process=False)
        trimesh.repair.fix_normals(m)
        repaired.append((m.vertices, m.faces))
    return repaired


__all__ = ["_bodies_worker", "_busy_repair", "_noop_repair", "_raise_error_repair", "_slow_spin_repair"]
