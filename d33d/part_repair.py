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
resolved at call time. The reply is carried back over a one-way pipe and
VALIDATED in the parent before use (a 3-tuple with kind in ``{"ok", "err"}``;
a malformed reply is a 422, never a 500). Tests point the hook at a test
stub (a top-level function in ``tests._repair_stubs`` — importable by the
child because the spawn child inherits the parent's ``sys.path``) via
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


def _as_arrays(
    vertices: Any, faces: Any
) -> tuple[np.ndarray, np.ndarray]:
    """``(vertices, faces)`` as ``float64`` / ``int32`` numpy arrays — the
    wire dtype for the process boundary (the pickled payload and the
    repaired reply both use it).

    A matching dtype (a ``float64`` / ``int32`` ndarray — trimesh's vertex
    dtype, pymeshfix's output dtype) is returned AS-IS: ``np.asarray`` is
    a no-op copy for a matching dtype (a ``np.shares_memory`` check
    confirms it), so the conversion is free. A differing dtype (trimesh's
    ``int64`` face arrays) copies — which the caller would otherwise do
    anyway at the Trimesh rebuild, so the copy is moved, not added.
    """
    v = np.asarray(vertices)
    if v.dtype != np.float64:
        v = v.astype(np.float64)
    f = np.asarray(faces)
    if f.dtype != np.int32:
        f = f.astype(np.int32)
    return v, f


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


def _repair_in_process(
    vertices: np.ndarray, faces: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
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


def _repair_bodies_in_process(
    bodies: list[tuple[np.ndarray, np.ndarray]]
) -> list[tuple[np.ndarray, np.ndarray]]:
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


class _RepairWorkerProcess(multiprocessing.Process):
    """A ``Process`` subclass that calls the parent-resolved worker in
    ``run()`` (overriding the default ``target/args`` dispatch). The
    instance is pickled to the spawn child (carrying the resolved worker,
    mode, and payload), and the child's ``run()`` calls the worker
    directly — no re-import of the module attribute, so a test
    monkeypatch of ``_REPAIR_WORKER`` is visible to the child.

    The ``mode`` dispatch: ``"single"``
    unpacks the payload as ``(verts, faces)``; ``"batch"`` passes the
    list as one arg. The reply is ``(kind, name, payload)`` over the
    pipe."""

    def __init__(self, send_conn: Any, worker: Any, mode: str, payload: Any):
        super().__init__(daemon=True)
        self._send_conn = send_conn
        self._worker = worker
        self._mode = mode
        self._payload = payload

    def run(self) -> None:
        try:
            if self._mode == "batch":
                result = self._worker(self._payload)
            else:
                verts, faces = self._payload
                result = self._worker(verts, faces)
            self._send_conn.send(("ok", "", result))
        except Exception as e:
            logging.getLogger(__name__).exception("repair worker failed")
            self._send_conn.send(("err", type(e).__name__, str(e)))


def _run_in_worker(
    mode: str, payload: Any, timeout: float, label: str
) -> Any:
    """The single worker-process lifecycle, shared by the single-body and
    batched repair paths (issue #395 lens round 3: the two call sites used
    to duplicate the spawn / send / poll / recv / cleanup logic — now
    extracted here, stated once in the module docstring).

    ``mode`` is ``"single"`` (the payload is ``(verts, faces)`` passed to
    the worker as two args) or ``"batch"`` (the payload is a list of
    ``(verts, faces)`` pairs passed as one list arg). ``label`` names the
    call in the timeout / error messages (e.g. ``"single body"`` or
    ``"2 bodies"``).

    The worker is the parent-resolved ``_REPAIR_WORKER`` (single) or
    ``_repair_bodies_in_process`` (batch), carried to the spawn child via
    the ``_RepairWorkerProcess`` instance (the child's ``run()`` calls it
    directly — no re-import of the module attribute, so a test monkeypatch
    of ``_REPAIR_WORKER`` is visible to the child). The reply is VALIDATED:
    a 3-tuple with kind in ``{"ok", "err"}`` (a malformed reply →
    ``PartUploadError``). On success the ``ok`` payload is returned; on
    ``err`` the worker-side ``PartUploadError`` propagates verbatim and any
    other exception is logged and raised as
    ``PartUploadError(f"repair failed: {name}")``. The child is always
    cleaned up (close, kill-if-alive, bounded join).
    """
    worker = _REPAIR_WORKER if mode == "single" else _repair_bodies_in_process

    ctx = multiprocessing.get_context("spawn")
    pipe_parent, pipe_child = ctx.Pipe(duplex=False)
    proc = _RepairWorkerProcess(pipe_child, worker, mode, payload)
    proc.start()

    try:
        # Wait for the reply with a bounded, interruptible poll: a short
        # ``poll`` interval (100 ms) in a loop, checking both the pipe
        # (for data) and the child's liveness (for a dead child). This
        # detects a dead child within ~100 ms of its exit, not after the
        # full ``timeout`` budget (a ``poll(timeout)`` would block for the
        # full budget even if the child died at t=0.2 s, because macOS does
        # not always signal pipe EOF through ``poll``).
        #
        # The deadline contract: a reply readable by the last poll that
        # STARTED before the deadline is accepted; otherwise
        # ``RepairTimeoutError``. The deadline is checked BEFORE every
        # poll slice and each slice is ``min(0.1, remaining)``, so the
        # wait never overshoots the budget by more than one ``poll(0)``
        import time as _time

        _POLL_INTERVAL = 0.1  # 100 ms — responsive dead-child detection
        _deadline = _time.monotonic() + timeout
        _child_dead = False
        while _time.monotonic() < _deadline:
            _remaining = _deadline - _time.monotonic()
            if _remaining <= 0:
                break  # deadline reached — the budget is spent
            if pipe_parent.poll(min(_POLL_INTERVAL, _remaining)):
                break  # data available → a reply is ready
            if not proc.is_alive():
                _child_dead = True
                break
        if _child_dead:
            # The child died without sending a reply — the 422. A ``recv``
            # on the drained pipe would hang on some platforms (macOS does
            # not always signal EOF through ``poll``), so we detect the dead
            # child directly and raise immediately. The child is already
            # gone; there is no reply to wait for.
            raise PartUploadError(
                "repair failed: worker child exited without replying"
            )
        if not pipe_parent.poll(0):
            # The timeout budget is spent and no reply is in the pipe (a
            # genuinely slow repair) — the 422 timeout; the finally block
            # kills the child. A reply in the pipe here would mean the
            # deadline check above was missed; that is unreachable because
            # the deadline is checked before every poll slice and no slice
            # can overshoot the deadline by a full slice.
            raise RepairTimeoutError(
                f"repair timed out: exceeded {timeout:.0f}s ({label})"
            )
        try:
            reply = pipe_parent.recv()
        except Exception as e:
            # A recv failure (pipe closed, EOF, a truncated pickle) is a
            # malformed reply — the 422, never a raw 500.
            raise PartUploadError(
                f"repair failed: malformed worker reply ({type(e).__name__})"
            ) from e
        # Validate the reply: a 3-tuple (kind, name, payload) with kind in
        # {"ok", "err"}. Anything else is malformed (a corrupted pickled
        # reply, a 2-tuple, a bare value) — the 422.
        if (
            not isinstance(reply, tuple)
            or len(reply) != 3
            or reply[0] not in ("ok", "err")
        ):
            raise PartUploadError("repair failed: malformed worker reply")
        _kind, name, payload_result = reply
        if _kind == "ok":
            return payload_result
        # kind == "err": classify by the exception's TYPE (not a message
        # match): PartUploadError → verbatim, anything else → wrapped.
        # The full payload is logged in the parent (the child's message can
        # carry arbitrary text — it must not leak into the 422 response).
        if name == "PartUploadError":
            raise PartUploadError(payload_result)
        # The child's own message (payload_result, bounded — the child's
        # message can be arbitrary text, never the INPUT payload) is what
        # gets logged: the parent's wrapped 422 carries only the type name.
        logger.error(
            "repair worker failed in child: %s: %s",
            name,
            str(payload_result)[:500],
        )
        raise PartUploadError(f"repair failed: {name}")
    finally:
        # Success / failure / timeout: never leave the child behind.
        try:
            pipe_parent.close()
        except (BrokenPipeError, OSError):
            pass
        if proc.is_alive():
            proc.kill()
        # Bounded join: never block indefinitely.
        proc.join(timeout=5)
        if proc.is_alive():
            # The post-kill still-alive case: a stable-prefix error (the
            # bounded join could not reap the child — a real, if rare,
            # failure, not a warning).
            logger.error(
                "repair worker orphaned: process %d still alive after "
                "kill() and join(timeout=5); leaving it (daemon=True, "
                "will be reaped on exit)",
                proc.pid,
            )


def repair_with_pmf(
    mesh: trimesh.Trimesh, timeout: float | None = None
) -> trimesh.Trimesh:
    """The one-call pymeshfix repair (``MeshFix.repair`` → ``fix_normals``)
    on a single mesh, in a separate, per-call process (the lifecycle lives
    in ``_run_in_worker``; the rationale is in the module docstring).

    The single-body path uses it UNCHANGED (the early branch keeps
    single-body imports byte-for-byte identical to the historical one-call
    repair); the multi-body path runs ``repair_bodies_with_pmf`` once for
    the whole set so repair cannot drop disconnected bodies (issue #375).

    ``timeout``: override the default timeout (seconds). Useful in tests
    where a very short timeout can be injected.
    """
    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    payload = _as_arrays(mesh.vertices, mesh.faces)

    result = _run_in_worker("single", payload, timeout, "single body")
    repaired_verts, repaired_faces = result
    return trimesh.Trimesh(
        *(_as_arrays(repaired_verts, repaired_faces)),
        process=False,
    )


def repair_bodies_with_pmf(
    meshes: list[trimesh.Trimesh], timeout: float | None = None
) -> list[trimesh.Trimesh]:
    """Repair ALL bodies in ONE spawned child process (the lifecycle lives
    in ``_run_in_worker``; the rationale is in the module docstring).

    The multi-body import path uses this (instead of calling
    ``repair_with_pmf`` per body) so the spawn overhead is paid ONCE, not
    once per body. The whole call is bounded by one timeout (default
    ``REPAIR_TIMEOUT_SECONDS``).

    ``timeout``: override the default timeout (seconds).
    """
    if timeout is None:
        timeout = REPAIR_TIMEOUT_SECONDS

    # Prepare arrays (the process boundary pickles numpy arrays, not
    # trimesh objects — simpler and avoids trimesh pickle overhead).
    payload = [_as_arrays(m.vertices, m.faces) for m in meshes]

    result = _run_in_worker("batch", payload, timeout, f"{len(meshes)} bodies")
    return [
        trimesh.Trimesh(
            *(_as_arrays(v, f)),
            process=False,
        )
        for v, f in result
    ]


__all__ = [
    "REPAIR_FACE_BUDGET",
    "REPAIR_TIMEOUT_SECONDS",
    "RepairTimeoutError",
    "_decimate",
    "repair_bodies_with_pmf",
    "repair_with_pmf",
]
