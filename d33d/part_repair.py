"""The per-mesh pymeshfix repair used by ``d33d.part_mesh.parse_and_repair``.

Extracted from ``part_mesh.py`` (issue #375, the 500-line rule): the
repair machinery is a single self-contained function with no state, so it
lives in its own module. ``part_mesh`` imports it — never the other way
round (no circular import; the error classes are imported from the leaf
module ``d33d.part_errors`` at the top level — no lazy resolution, no
``sys.modules`` mutation).

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

The worker is the module-level ``_REPAIR_WORKER`` callable (default
``_repair_in_process``), resolved in the PARENT at call time and PICKLED
to the child with the call's arguments (a top-level function of an
importable module — a fresh spawn interpreter re-imports ``d33d``
normally, so the pickled reference resolves there). The parent executes
NO dotted-string input: only a parent-settable, module-level callable is
ever run in the child, and the pickled reference pins it to the function
resolved at call time. Tests point the hook at a test stub (a top-level
function in ``tests._repair_stubs`` — importable by the child because the
spawn child inherits the parent's ``sys.path``) via
``monkeypatch.setattr("d33d.part_repair._REPAIR_WORKER", <callable>)``.

Spawn startup costs about 0.5–1 s, which is acceptable: repair runs only
for unclean meshes (clean ones skip repair entirely), and the 120 s
budget dominates.

Multi-body repair (issue #395 lens round 2): ``repair_bodies_with_pmf``
repairs ALL watertight bodies in ONE spawned child process — the per-body
loop used to spawn a fresh process per body (0.5–1 s of spawn overhead
each; fifty small bodies cost tens of seconds and could hit the 120 s
aggregate budget). The child repairs each body in order (the per-body
MeshFix + fix_normals semantics of issue #375) and returns the list; the
whole call is bounded by one timeout.
"""

from __future__ import annotations

import logging
import multiprocessing
from multiprocessing import SimpleQueue
from typing import Any

logger = logging.getLogger(__name__)

import numpy as np
import trimesh

from d33d.part_errors import PartUploadError, RepairTimeoutError

#: The repair timeout in seconds. A mesh that pymeshfix genuinely cannot
#: repair in this time will hit the timeout path → 422, never a hang.
REPAIR_TIMEOUT_SECONDS = 120

#: The named face budget for decimation-before-repair (ticket #3: decimate
#: BEFORE repair, fix_normals AFTER). A mesh above this budget is decimated
#: to approximately this number of faces before repair is attempted. A
#: clean mesh (0 boundary loops, all bodies watertight, winding consistent)
#: below this budget is NOT decimated — it skips repair entirely and the
#: stored mesh is the merged mesh unchanged. A clean mesh above this budget
#: is decimated to this budget even though it skips repair (render safety).
REPAIR_FACE_BUDGET = 500_000


def _decimate(mesh: trimesh.Trimesh, target_faces: int) -> trimesh.Trimesh:
    """Decimate the mesh to approximately ``target_faces`` faces.

    Ticket #3: decimate BEFORE repair. Uses trimesh's quadric decimation
    (``simplify_quadric_decimation(face_count=...)`` — backed by the
    ``fast_simplification`` package, a declared runtime dependency).

    ``fast_simplification`` is a hard dependency: a missing install is a
    loud import-time failure (pinned by the ``test_fast_simplification_importable``
    test in ``tests/test_part_import.py``), never a silent no-op. If the
    mesh is already at or below the target, it is returned unchanged (no
    work). A genuine decimation failure (degenerate input, arithmetic
    breakdown in the C library) still raises as a ``PartUploadError`` —
    an oversized mesh that cannot be simplified must 422, not proceed
    to repair at its full size.
    """
    if len(mesh.faces) <= target_faces:
        return mesh
    try:
        return mesh.simplify_quadric_decimation(face_count=target_faces)
    except PartUploadError:
        raise
    except ImportError:
        # ``fast_simplification`` is declared in pyproject.toml — an
        # ImportError here is a broken install, not a runtime choice.
        raise PartUploadError(
            "parse_and_repair: decimation failed — fast_simplification is "
            "installed as a runtime dependency; its import failed, so the "
            f"{len(mesh.faces)}-face mesh above the {target_faces}-face "
            "budget cannot be simplified. Repair the install and retry."
        )
    except Exception as e:
        # A broad ``except Exception`` (a decimation failure from the C
        # library — arithmetic, memory, or anything else it raises) is
        # logged and re-raised as a ``PartUploadError`` (the 422): an
        # oversized mesh that cannot be simplified must 422, never
        # proceed to repair at its full size.
        logger.exception(
            "quadric decimation failed for the %d-face mesh above the "
            "%d-face budget", len(mesh.faces), target_faces,
        )
        raise PartUploadError(
            f"parse_and_repair: quadric decimation failed: {type(e).__name__}: {e}"
        ) from e


def _repair_in_process(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """The pymeshfix repair, run in the worker process (top-level → picklable).

    Receives raw numpy arrays (picklable — not trimesh objects) and
    returns the repaired ``(vertices, faces)`` raw-arrays tuple (the
    parent rebuilds the ``Trimesh``). The worker imports pymeshfix here
    (not at module top) so the parent process doesn't pay the import
    cost.
    """
    import pymeshfix as _pmf

    fix = _pmf.MeshFix(vertices, faces)
    fix.repair()
    repaired_verts = np.asarray(fix.points, dtype=np.float64)
    repaired_faces = np.asarray(fix.faces, dtype=np.int32)
    repaired = trimesh.Trimesh(repaired_verts, repaired_faces, process=False)
    trimesh.repair.fix_normals(repaired)
    return repaired.vertices, repaired.faces


def _repair_bodies_in_process(bodies: list) -> list:
    """The batched pymeshfix repair for multi-body imports, run in the
    worker process (top-level → picklable).

    Receives a list of ``(vertices, faces)`` raw-array pairs (already
    decimated in the parent, if above the budget) and returns the list of
    repaired ``(vertices, faces)`` pairs. Each body is repaired in order
    with the per-body MeshFix + fix_normals semantics (issue #375: every
    body kept, the same per-body repair chain). The whole call is bounded
    by one timeout (the parent's ``poll`` on the pipe).
    """
    import pymeshfix as _pmf

    repaired: list = []
    for verts, faces in bodies:
        fix = _pmf.MeshFix(verts, faces)
        fix.repair()
        rep_verts = np.asarray(fix.points, dtype=np.float64)
        rep_faces = np.asarray(fix.faces, dtype=np.int32)
        m = trimesh.Trimesh(rep_verts, rep_faces, process=False)
        trimesh.repair.fix_normals(m)
        repaired.append((m.vertices, m.faces))
    return repaired


# The worker hook (issue #395 GIL test). A module-level CALLABLE, resolved
# in the PARENT at call time and pickled to the spawn child with the call
# arguments (a top-level function of an importable module). The parent
# never executes a dotted-string input: only this callable is what the
# child runs, and the pickled reference pins it to the function resolved
# at call time. Tests point it at a test stub (a top-level function in
# ``tests._repair_stubs`` — importable by the child, which inherits the
# parent's ``sys.path``) via monkeypatch.
_REPAIR_WORKER = _repair_in_process


def _child_main(
    queue: SimpleQueue, send_conn: Any, worker: Any
) -> None:
    """The spawn child's entry point (top-level → picklable).

    ``worker`` is the parent-resolved callable (pickled by reference with
    this call's arguments — never a string the child parses). The child
    pulls the input off the queue, runs the worker, and ships
    ``(kind, exception-name, payload)`` back over the pipe (the exception's
    TYPE name is sent so the parent classifies by type, not by message
    text).

    The input shape depends on the worker:
    - ``_REPAIR_WORKER`` (single-body): ``(vertices, faces)`` arrays →
      ``(vertices, faces)`` result.
    - ``_repair_bodies_in_process`` (multi-body): list of
      ``(vertices, faces)`` pairs → list of repaired pairs.
    """
    data = queue.get()
    try:
        # The single-body workers (``_REPAIR_WORKER``) take two args
        # ``(vertices, faces)``; the multi-body worker
        # (``_repair_bodies_in_process``) takes one arg (a list of pairs).
        if worker is _repair_bodies_in_process:
            result = worker(data)
        else:
            verts, faces = data
            result = worker(verts, faces)
        send_conn.send(("ok", "", result))
    except Exception as e:
        # Log the full traceback in the child and ship the type name
        # back so the parent classifies by type, not by message text.
        logging.getLogger(__name__).exception("repair worker failed")
        send_conn.send(("err", type(e).__name__, str(e)))


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
    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    verts = np.asarray(mesh.vertices, dtype=np.float64)
    faces = np.asarray(mesh.faces, dtype=np.int32)

    # The worker is a module-level callable (the hook, possibly a test
    # stub) resolved in the PARENT and pickled by reference to the child —
    # no dotted-string resolution happens anywhere.
    worker = _REPAIR_WORKER

    ctx = multiprocessing.get_context("spawn")
    in_q: SimpleQueue = ctx.SimpleQueue()
    pipe_parent, pipe_child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_main, args=(in_q, pipe_child, worker),
        daemon=True,
    )
    proc.start()

    try:
        try:
            in_q.put((verts, faces))
        except OSError as e:
            # The send-side pipe broke (child died before consuming input,
            # or the pipe buffer was full). Map to PartUploadError (the 422).
            raise PartUploadError(
                f"repair failed: could not send mesh to worker: {type(e).__name__}: {e}"
            ) from e
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
        # The full payload is logged in the parent (the child's message can
        # carry arbitrary text — it must not leak into the 422 response).
        if name == "PartUploadError":
            raise PartUploadError(payload)
        logger.warning("repair worker failed in child: %s", payload)
        raise PartUploadError(f"repair failed: {name}")
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
        # Bounded join: never block indefinitely.
        proc.join(timeout=5)
        if proc.is_alive():
            logger.warning(
                "repair worker process %d still alive after join(timeout=5); "
                "leaving it (daemon=True, will be reaped on exit)",
                proc.pid,
            )


def repair_bodies_with_pmf(
    meshes: list[trimesh.Trimesh], timeout: float | None = None
) -> list[trimesh.Trimesh]:
    """Repair ALL watertight bodies in ONE spawned child process.

    The multi-body import path uses this (instead of calling
    ``repair_with_pmf`` per body) so the spawn overhead is paid ONCE, not
    once per body. The parent sends the list of ``(vertices, faces)``
    arrays (already decimated, if above the budget) to one child; the
    child repairs each body in order (the per-body MeshFix + fix_normals
    semantics of issue #375) and returns the list. The whole call is
    bounded by one timeout (default ``REPAIR_TIMEOUT_SECONDS``).

    ``timeout``: override the default timeout (seconds).
    """
    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    bodies = [
        (
            np.asarray(m.vertices, dtype=np.float64),
            np.asarray(m.faces, dtype=np.int32),
        )
        for m in meshes
    ]

    ctx = multiprocessing.get_context("spawn")
    in_q: SimpleQueue = ctx.SimpleQueue()
    pipe_parent, pipe_child = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=_child_main,
        args=(in_q, pipe_child, _repair_bodies_in_process),
        daemon=True,
    )
    proc.start()

    try:
        try:
            in_q.put(bodies)
        except OSError as e:
            raise PartUploadError(
                f"repair failed: could not send bodies to worker: {type(e).__name__}: {e}"
            ) from e
        if not pipe_parent.poll(timeout):
            raise RepairTimeoutError(
                f"repair timed out: exceeded {timeout:.0f}s across {len(meshes)} bodies"
            )
        _kind, name, payload = pipe_parent.recv()
        if _kind == "ok":
            return [
                trimesh.Trimesh(v, f, process=False) for v, f in payload
            ]
        if name == "PartUploadError":
            raise PartUploadError(payload)
        logger.warning("repair worker failed in child: %s", payload)
        raise PartUploadError(f"repair failed: {name}")
    finally:
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
        proc.join(timeout=5)
        if proc.is_alive():
            logger.warning(
                "repair worker process %d still alive after join(timeout=5); "
                "leaving it (daemon=True, will be reaped on exit)",
                proc.pid,
            )


__all__ = [
    "REPAIR_FACE_BUDGET",
    "REPAIR_TIMEOUT_SECONDS",
    "RepairTimeoutError",
    "_decimate",
    "repair_bodies_with_pmf",
    "repair_with_pmf",
]
