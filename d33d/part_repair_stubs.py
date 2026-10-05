"""Top-level stubs for the issue #395 repair-process tests.

The spawn child looks up its worker in ``d33d.part_repair``'s module
globals by the qualname the parent sent (the ``_REPAIR_WORKER`` hook), so
a stub pointed at from the parent must be importable BY THE CHILD. These
stubs live in a plain module (``d33d.part_repair_stubs``) that is
importable by a fresh interpreter — no monkeypatch propagation needed.

Each stub takes ``(vertices, faces)`` (the same shape
``_repair_in_process`` does) and returns a ``trimesh.Trimesh`` — the
parent rebuilds a ``Trimesh`` from the returned mesh's arrays, so a stub
can also just return the input arrays unchanged (the parent's
``trimesh.Trimesh(verts, faces, process=False)`` rebuild is a no-op for
a valid mesh).
"""

from __future__ import annotations

import numpy as np


def _noop_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A no-op repair: returns the input arrays unchanged (the parent
    rebuilds the ``Trimesh`` from them — a no-op for a valid mesh).

    Used by the off-event-loop test: the repair seam is exercised
    (``repair_with_pmf`` → spawn child → this stub), but the stub is
    instant, so the test measures the boundary, not pymeshfix.
    """
    return vertices, faces


def _busy_repair(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """A GIL-holding stub (issue #395 off-event-loop test).

    Runs a C-level busy loop (``sum(range(200_000_000))`` — about 1.1 s on
    the CI/dev hardware) that does NOT release the GIL for its full
    duration (a pure-Python ``for`` loop does — CPython's eval loop
    switches threads every ~5 ms, so a Python-level busy loop in a thread
    does NOT starve a concurrent asyncio ticker, and a test using one
    would pass on main's ``asyncio.to_thread`` approach — exactly the bug
    class we're fixing). The stub runs INSIDE the spawn child, so the
    GIL held is the CHILD's — the parent's event loop must be unaffected
    by the same proof: with a thread-based ``to_thread`` implementation,
    the stub would run in a parent thread holding the parent's GIL and
    the concurrent request's tick gaps would balloon past 200 ms; with
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


__all__ = ["_busy_repair", "_noop_repair", "_slow_spin_repair"]
