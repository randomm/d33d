"""The per-mesh pymeshfix repair used by ``d33d.part_mesh.parse_and_repair``.

Extracted from ``part_mesh.py`` (issue #375, the 500-line rule): the
repair machinery is a single self-contained function with no state, so it
lives in its own module. ``part_mesh`` imports it — never the other way
round (no circular import; this module imports nothing from ``d33d``
except the ``PartUploadError`` type it must passthrough, imported
inside the function to keep the import edge one-way even in case
``part_mesh``'s module body ever needs to grow).

Issue #395: the repair runs in a **separate process** with a timeout, so
pymeshfix's GIL-holding C work can never block the event loop or other
requests. Each ``repair_with_pmf`` call gets its OWN one-shot worker
process (``multiprocessing.get_context("spawn")`` — no shared pool):

- the timeout applies only to THAT call's own work — a second concurrent
  import runs in its own process and never queues behind the first;
- a timed-out worker is ``kill()``-ed (SIGKILL) and ``join()``-ed, which
  cannot touch any other call's worker (a shared single-worker pool made
  a timeout SIGKILL an in-flight neighbour's worker → ``BrokenProcessPool``
  → "repair failed" for the innocent import);
- on success, on failure, and on timeout the child is always ``join()``-ed
  (after a ``kill()`` on timeout), so no zombie or orphan process is
  left behind in any of the three cases.

The worker target is resolved in the PARENT (from the module-level
``_REPAIR_WORKER`` hook — default ``_repair_in_process``) and its
``__qualname__`` is sent to the child, which looks the function up in
this module's globals at startup. Tests can point the hook at a stub
(e.g. a GIL-holding one for the off-event-loop test) via
``monkeypatch.setattr("d33d.part_repair._REPAIR_WORKER", <qualname>)`` —
no monkeypatch propagation through pickle needed, because the child
receives the resolved name, not a patched reference.

Spawn startup costs about 0.5–1 s, which is acceptable: repair runs only
for unclean meshes (clean ones skip repair entirely), and the 120 s
budget dominates.
"""

from __future__ import annotations

import multiprocessing
from multiprocessing import SimpleQueue
from typing import Any

import numpy as np
import trimesh

#: The repair timeout in seconds. A mesh that pymeshfix genuinely cannot
#: repair in this time will hit the timeout path → 422, never a hang.
REPAIR_TIMEOUT_SECONDS = 120

#: The 422 detail for a repair timeout (issue #395). Distinct from the
#: unparseable detail: the mesh is NOT broken, it's just too slow to repair.
#: The copy.ts key ``partUpload.repairTimeout`` must match this string
#: exactly (parity pinned in the import-stl-contract test).
REPAIR_TIMEOUT_DETAIL = (
    "The file is too complex to repair in time. Try simplifying the mesh."
)

# The worker hook (issue #395 GIL test). Resolved in the PARENT at call
# time; the resolved dotted qualname is what the spawn child runs (it
# imports the named module and looks the function up there — see
# ``_resolve_worker``). Tests point it at a stub by setting this
# attribute to the stub's dotted qualname — the stub must be a top-level
# function in a module importable by the child.
_REPAIR_WORKER = "d33d.part_repair._repair_in_process"


class PartUploadError(ValueError):
    """The import-decode error.

    At first use this placeholder is re-bound to
    ``d33d.part_mesh.PartUploadError`` (the same class the rest of the
    package raises and catches) so ``isinstance`` checks work across the
    boundary. The re-bind is lazy (``_resolve_part_upload_error``) because
    ``part_mesh`` imports this module at its top level — an import-time
    re-bind would hit a partially-initialised module.
    """


class RepairTimeoutError(PartUploadError):
    """A typed repair-timeout signal (issue #395). The route checks
    ``isinstance(error, RepairTimeoutError)`` instead of matching the
    string ``"timed out"`` in the message."""


_real_part_upload_error: type | None = None


def _resolve_part_upload_error() -> type:
    """The real ``d33d.part_mesh.PartUploadError`` (lazy-resolved once).

    Re-binds this module's ``PartUploadError`` placeholder to the real
    class and re-bases ``RepairTimeoutError`` onto it, so
    ``isinstance(e, PartUploadError)`` works against the class the rest of
    the package raises. Called from :func:`repair_with_pmf`.
    """
    global _real_part_upload_error
    if _real_part_upload_error is None:
        import sys

        from d33d import part_mesh

        _real_part_upload_error = part_mesh.PartUploadError
        sys.modules[__name__].PartUploadError = _real_part_upload_error
        RepairTimeoutError.__bases__ = (_real_part_upload_error,)
    return _real_part_upload_error


def is_repair_timeout(error: BaseException) -> bool:
    """True if ``error`` is a :class:`RepairTimeoutError` — the typed
    signal the route checks with ``isinstance`` (issue #395: replaces
    string-matching ``"timed out" in str(error)``)."""
    return isinstance(error, RepairTimeoutError)


# ---------------------------------------------------------------------------
# Process-boundary worker function (must be top-level for pickling)
# ---------------------------------------------------------------------------


def _repair_in_process(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """The pymeshfix repair, run in the worker process.

    Receives raw numpy arrays (picklable — not trimesh objects) and
    returns the repaired ``(vertices, faces)`` raw-arrays tuple (the
    parent rebuilds the ``Trimesh``). The worker imports pymeshfix here
    (not at module top) so the parent process doesn't pay the import
    cost.

    Must be a top-level (picklable) function for the spawn child.
    """
    import pymeshfix as _pmf

    fix = _pmf.MeshFix(vertices, faces)
    fix.repair()
    repaired_verts = np.asarray(fix.points, dtype=np.float64)
    repaired_faces = np.asarray(fix.faces, dtype=np.int32)
    repaired = trimesh.Trimesh(repaired_verts, repaired_faces, process=False)
    trimesh.repair.fix_normals(repaired)
    return repaired.vertices, repaired.faces


def _child_main(
    queue: SimpleQueue, send_conn: Any, worker_qualname: str
) -> None:
    """The spawn child's entry point (top-level → picklable).

    Resolves the worker from the parent-sent dotted qualname (the
    parent-resolved ``_REPAIR_WORKER`` hook value — e.g. ``"d33d.
    part_repair_stubs._busy_repair"`` for a test stub), pulls the
    (vertices, faces) arrays off the queue, runs the worker, and ships
    ``(kind, exception-name, payload)`` back over the pipe (the
    exception's TYPE name is sent so the parent classifies by type, not
    by message text).
    """
    worker = _resolve_worker(worker_qualname)
    verts, faces = queue.get()
    try:
        result = worker(verts, faces)
        send_conn.send(("ok", "", result))
    except BaseException as e:  # noqa: BLE001 — ship the type back verbatim
        send_conn.send(("err", type(e).__name__, str(e)))


def _resolve_worker(worker_qualname: str):
    """Resolve a dotted qualname (``"pkg.mod.func"``) to the function.

    The parent sends the ``_REPAIR_WORKER`` hook's value (a dotted
    qualname); the child (a fresh spawn interpreter) imports the named
    module and looks the function up there. The hook is resolved in the
    PARENT (a module attribute read at call time), so no monkeypatch
    propagation through pickle is needed — the child just needs the module
    to be importable (it is: a fresh spawn interpreter re-imports
    ``d33d`` normally).
    """
    import importlib

    parts = worker_qualname.rsplit(".", 1)
    if len(parts) != 2:
        # Not dotted — fall back to this module's globals (the default).
        return globals()[worker_qualname]
    mod_name, func_name = parts
    return getattr(importlib.import_module(mod_name), func_name)


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

    Issue #395: the repair runs in a **separate, per-call process** with a
    timeout (default ``REPAIR_TIMEOUT_SECONDS``), so pymeshfix's
    GIL-holding C code can never block the event loop. The worker is a
    fresh ``spawn`` ``Process`` per call: the timeout bounds only THIS
    call's work, and the timeout's ``kill()`` + ``join()`` cannot affect
    any other call's worker. On timeout a ``RepairTimeoutError`` is
    raised; on success, failure, and timeout the child is always
    ``join()``-ed (no zombie/orphan in any of the three cases).

    Non-``PartUploadError`` failures are wrapped as ``PartUploadError(
    "repair failed: …")``; a worker-side ``PartUploadError`` propagates
    verbatim (pinned by the in-repair-block propagation test).

    ``timeout``: override the default timeout (seconds). Useful in tests
    where a very short timeout can be injected.
    """
    _resolve_part_upload_error()
    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int32)

    # Resolve the worker in the PARENT (the hook is a module attribute
    # here; the child receives the resolved qualname and looks the
    # function up in this same module's globals — see the docstring).
    worker_qualname = _REPAIR_WORKER

    ctx = multiprocessing.get_context("spawn")
    in_q: SimpleQueue = ctx.SimpleQueue()
    pipe_parent, pipe_child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_main, args=(in_q, pipe_child, worker_qualname),
        daemon=True,
    )
    proc.start()

    try:
        in_q.put((verts, faces))
        if not pipe_parent.poll(timeout):
            # The timeout bounds only THIS call's own work (the child is
            # per-call). ``poll`` returning False means the child has not
            # finished within the budget — the finally block kills it;
            # no other call's worker is touched.
            raise RepairTimeoutError(
                f"repair timed out: exceeded {timeout:.0f}s"
            )
        _kind, name, payload = pipe_parent.recv()
        if _kind == "ok":
            repaired_verts, repaired_faces = payload
            return trimesh.Trimesh(
                repaired_verts, repaired_faces, process=False
            )
        # kind == "err": classify by the exception's TYPE (not a message
        # match): PartUploadError → verbatim, anything else → wrapped.
        if name == "PartUploadError":
            raise PartUploadError(payload)
        raise PartUploadError(f"repair failed: {name}: {payload}")
    finally:
        # Success / failure / timeout: never leave the child behind.
        try:
            pipe_parent.close()
        except (BrokenPipeError, OSError):
            pass
        try:
            in_q.close()
        except OSError:
            pass
        if proc.is_alive():
            proc.kill()
        proc.join()


__all__ = [
    "REPAIR_TIMEOUT_DETAIL",
    "REPAIR_TIMEOUT_SECONDS",
    "RepairTimeoutError",
    "is_repair_timeout",
    "repair_with_pmf",
]
