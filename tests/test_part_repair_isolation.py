"""Issue #395: per-call isolated repair process, typed timeout, GIL-holding test.

Regression tests for the four #395 defects:

1. **Per-import isolation** — two concurrent ``repair_with_pmf`` calls
   (one stuck, one real) must not affect each other: the stuck one times
   out, the real one succeeds.
2. **Typed timeout signal** — ``RepairTimeoutError`` (a
   ``PartUploadError`` subclass) is the signal the route checks with
   ``isinstance``.
3. **GIL-holding off-event-loop test** — the off-event-loop test uses a
   stub that HOLDS THE GIL (a C-level ``sum(range(N))``), runs it inside
   the real repair seam (the spawn child), and asserts a concurrent
   request's tick gaps stay under 200 ms. A pure-Python busy loop would
   NOT hold the GIL continuously (the interpreter switches every ~5 ms),
   so this test would pass on main's ``asyncio.to_thread`` approach —
   exactly the bug class we're fixing.
4. **Real timeout test** — the real process boundary with an injected
   ``timeout=1``: ``RepairTimeoutError`` raised in ~1–3 s, the worker
   process dead afterwards, and a subsequent real repair (``holey.stl``)
   succeeds.
"""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import time
from pathlib import Path
from typing import Any

import pytest

from d33d.part_repair import (
    RepairTimeoutError,
    repair_bodies_with_pmf,
    repair_with_pmf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stl"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _stl_bytes_from_mesh(mesh):
    """Export a trimesh mesh as STL bytes."""
    import io

    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    return buf.getvalue()


class _FailingPipe:
    """A pipe stub whose recv() raises OSError (top-level → picklable)."""
    def send(self, *a, **kw):
        pass
    def recv(self):
        raise OSError("broken pipe")
    def poll(self, *a, **kw):
        return True
    def close(self):
        pass


def _failing_pipe_factory(*args, **kw):
    """Module-level factory (top-level → picklable for the spawn child).
    Returns a 2-tuple of the same stub (``ctx.Pipe`` returns
    ``(parent_conn, child_conn)`` — both must be the failing stub so the
    parent's ``recv`` raises OSError)."""
    return _FailingPipe(), _FailingPipe()


def _box_mesh():
    """A small watertight box (in-memory — no fixture file needed)."""
    import trimesh

    box = trimesh.creation.box(extents=[5, 5, 5])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    return box


def _hole_mesh():
    """The holey.stl fixture (4 boundary loops — repair is needed)."""
    import trimesh

    data = (FIXTURES / "holey.stl").read_bytes()
    import io

    mesh = trimesh.load(io.BytesIO(data), file_type="stl")
    return mesh


def _run_async(coro_factory) -> Any:
    """Run ``coro_factory(client)`` in a fresh event loop (mirrors
    ``test_part_import._run_async``'s shape without the app fixture)."""

    async def _run(client):
        return await coro_factory(client)

    return asyncio.run(_run(None))


# ---------------------------------------------------------------------------
# 2. Typed timeout signal
# ---------------------------------------------------------------------------


def test_repair_timeout_error_is_a_part_upload_error_subclass():
    """``RepairTimeoutError`` subclasses ``PartUploadError`` (the route's
    ``except PartUploadError`` catches it) — and the timeout is detected
    by TYPE, not by message: a ``PartUploadError`` whose message happens
    to contain ``"timed out"`` is NOT a repair timeout."""
    from d33d.part_errors import PartUploadError

    e = RepairTimeoutError("repair timed out: exceeded 120s")
    assert isinstance(e, PartUploadError)
    # A plain PartUploadError with "timed out" in the message is NOT a
    # repair timeout (the type, not the message, is the signal).
    plain = PartUploadError("the worker said: timed out")
    assert not isinstance(plain, RepairTimeoutError)
    assert isinstance(plain, PartUploadError)


# ---------------------------------------------------------------------------
# 4. Real timeout test (the real process boundary)
# ---------------------------------------------------------------------------


def test_real_repair_timeout_kills_worker_and_next_repair_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive the REAL process boundary with an injected ``timeout=1``:
    the worker target is a top-level stub (``_slow_spin_repair``) that
    spins longer than the timeout. Asserts:
    - ``RepairTimeoutError`` is raised in ~1–3 s (not 120 s);
    - the worker process is dead afterwards (``active_children()`` empty
      after the call returns — the ``finally`` block joined the child);
    - a subsequent real repair (``holey.stl`` through the default worker)
      succeeds.
    """
    import multiprocessing

    import d33d.part_repair as part_repair_mod
    from tests._repair_stubs import _slow_spin_repair

    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        _slow_spin_repair,
    )

    mesh = _box_mesh()
    t0 = time.monotonic()
    with pytest.raises(RepairTimeoutError) as excinfo:
        repair_with_pmf(mesh, timeout=1)
    elapsed = time.monotonic() - t0

    assert 0.5 <= elapsed <= 3.5, (
        f"timeout fired after {elapsed:.2f}s — expected ~1-3s "
        f"(spawn startup ~0.5-1s + the 1s budget)"
    )
    assert "timed out" in str(excinfo.value)

    # The worker process must be dead (no zombie/orphan). ``join`` runs in
    # the ``finally`` block before the exception propagates, so
    # ``active_children()`` must be empty by the time we get here.
    children = multiprocessing.active_children()
    assert children == [], f"leftover child processes: {children}"

    # A subsequent REAL repair (the default worker, ``holey.stl``) must
    # succeed — the killed child did not poison any shared state.
    monkeypatch.undo()
    holey = _hole_mesh()
    repaired = repair_with_pmf(holey)
    assert len(repaired.faces) > 0


def test_two_concurrent_repairs_stuck_and_real_both_behave(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two concurrent ``repair_with_pmf`` calls (in two threads): one
    stuck (``_slow_spin_repair`` with ``timeout=1``), one real
    (``holey.stl`` with the default worker). The per-call isolation means
    the stuck one's timeout KILLs only its own child — the real one
    succeeds. (The old shared single-worker pool would SIGKILL the
    in-flight neighbour → ``BrokenProcessPool`` → "repair failed".)"""
    import threading

    import d33d.part_repair as part_repair_mod
    from tests._repair_stubs import _slow_spin_repair

    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        _slow_spin_repair,
    )

    box = _box_mesh()
    holey = _hole_mesh()

    stuck_result: dict[str, Any] = {}
    real_result: dict[str, Any] = {}

    def _stuck_call():
        try:
            repair_with_pmf(box, timeout=1)
            stuck_result["status"] = "no-raise"
        except RepairTimeoutError as e:
            stuck_result["status"] = "timeout"
            stuck_result["msg"] = str(e)
        except Exception as e:  # record any failure shape (test harness)
            logger.exception("stuck call failed unexpectedly")
            stuck_result["status"] = f"error: {type(e).__name__}: {e}"

    def _real_call():
        try:
            m = repair_with_pmf(holey)
            real_result["status"] = "ok"
            real_result["faces"] = len(m.faces)
        except Exception as e:  # record any failure shape (test harness)
            logger.exception("real call failed unexpectedly")
            real_result["status"] = f"error: {type(e).__name__}: {e}"

    t_stuck = threading.Thread(target=_stuck_call)
    t_real = threading.Thread(target=_real_call)
    t_stuck.start()
    t_real.start()
    t_stuck.join(timeout=15)
    t_real.join(timeout=30)
    assert not t_stuck.is_alive(), "stuck call did not finish in 15s"
    assert not t_real.is_alive(), "real call did not finish in 30s"

    assert stuck_result.get("status") == "timeout", (
        f"stuck call: {stuck_result}"
    )
    assert real_result.get("status") == "ok", (
        f"real call (should succeed — per-call isolation): {real_result}"
    )
    assert real_result.get("faces", 0) > 0


# ---------------------------------------------------------------------------
# 3. GIL-holding off-event-loop test (the spawn-child repair seam)
# ---------------------------------------------------------------------------


def test_repair_seam_gil_holding_stub_does_not_starve_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #395 (GIL test): the off-event-loop test uses a stub that
    HOLDS THE GIL and runs it INSIDE the repair seam that is now
    process-isolated.

    The stub (``tests._repair_stubs._busy_repair``) runs
    ``sum(range(200_000_000))`` — a C-level busy loop that does NOT
    release the GIL for its full duration (a pure-Python ``for`` loop
    would — CPython's eval loop switches threads every ~5 ms, so a
    Python-level busy loop in a thread does NOT starve a concurrent
    asyncio ticker, and a test using one would pass on main's
    ``asyncio.to_thread`` approach — exactly the bug class we're fixing).

    The stub is pointed at via the ``_REPAIR_WORKER`` hook (resolved in
    the parent, the child looks it up in ``d33d.part_repair``'s globals
    by the qualname the parent sent — no monkeypatch propagation through
    pickle needed). It runs in the SPAWN CHILD, so the GIL it holds is
    the child's, not the parent's — the parent's event loop must be
    unaffected. The test asserts an event-loop ticker (a concurrent
    ``GET /api/projects`` every 10 ms) keeps responding: max tick gap
    < 200 ms while the ~1.1 s stub runs.

    This FAILS against main's ``asyncio.to_thread`` approach: there, the
    stub would run in a parent thread holding the parent's GIL, and the
    ticker's gaps would balloon past 200 ms (the C-level loop never
    releases the GIL, so the event loop cannot tick).
    """
    import d33d.part_repair as part_repair_mod
    from tests._repair_stubs import _busy_repair

    # Point the repair seam at the GIL-holding stub (resolved in the
    # parent and pickled by reference — the child receives the function
    # object, never a dotted string).
    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        _busy_repair,
    )

    # Use holey.stl (NOT clean → repair is attempted → the seam is hit).
    data = (FIXTURES / "holey.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "GILTest"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}

        gaps: list[float] = []
        stop = asyncio.Event()

        async def _ticker():
            last = time.monotonic()
            while not stop.is_set():
                await asyncio.sleep(0.01)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        ticker_task = asyncio.ensure_future(_ticker())

        # The upload: the decode (parse_and_repair → repair_with_pmf →
        # spawn child running _busy_repair) runs off the event loop.
        upload_resp = await client.post(
            f"/api/projects/{pid}/part", files=files
        )

        stop.set()
        await ticker_task
        return upload_resp, gaps

    async def _run(client):
        return await _call(client)

    # Run via the app's event loop (the same shape as
    # test_part_import._run_async — the app fixture provides the ASGI
    # client).
    import httpx
    from httpx import ASGITransport

    def _make_app():
        import tempfile
        from pathlib import Path

        import d33d.app as app_mod

        tmp = Path(tempfile.mkdtemp(prefix="d33d-gil-test-"))
        return app_mod.create_app(
            tmp / "d33d.sqlite3",
            master_key_path=tmp / "master.key",
            catalogue_path=tmp / "models.yaml",
        )

    app = _make_app()

    async def _with_client():
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            return await _call(client)

    upload_r, gaps = asyncio.run(_with_client())
    assert upload_r.status_code == 201, upload_r.text
    # The ticker must have ticked (it ran for the ~1.1 s the stub ran).
    assert len(gaps) >= 5, f"ticker only ticked {len(gaps)} times"
    max_gap = max(gaps)
    assert max_gap < 0.2, (
        f"event loop starved: max tick gap {max_gap * 1000:.0f} ms "
        f"(> 200 ms) while the GIL-holding repair stub ran — the repair "
        f"must be process-isolated, not thread-isolated"
    )


# ---------------------------------------------------------------------------
# New tests: winding check degrades to False on AttributeError, genus
# fallback logs, in_q.put OSError → PartUploadError, multi-body aggregate
# budget
# ---------------------------------------------------------------------------


def test_winding_check_degrades_to_false_on_attribute_error(monkeypatch):
    """The documented 'measurement never raises' contract: when
    ``is_winding_consistent`` raises AttributeError, ``mesh_topology``
    returns ``winding_consistent=False`` and does NOT raise."""
    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    box = trimesh.creation.box(extents=[5, 5, 5])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    components = box.split(only_watertight=False)

    # Monkeypatch trimesh's is_winding_consistent property to raise
    # AttributeError (simulating trimesh API drift).

    class _RaisingProp:
        def __get__(self, obj, objtype=None):
            raise AttributeError("intentional: simulate trimesh drift")

    monkeypatch.setattr(
        trimesh.Trimesh, "is_winding_consistent", _RaisingProp()
    )
    try:
        topo = mesh_topology(box, components)
    finally:
        monkeypatch.undo()
    assert topo["winding_consistent"] is False


def test_genus_fallback_logs_warning(caplog):
    """The genus fallback: when ``watertight_genus`` raises, ``mesh_topology``
    logs a warning (exc_info=True) and returns genus=0."""
    from unittest.mock import patch

    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    box = trimesh.creation.box(extents=[5, 5, 5])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    components = box.split(only_watertight=False)

    def _boom(c):
        raise RuntimeError("genus computation failed")

    with patch("d33d.part_mesh_topology.watertight_genus", side_effect=_boom), caplog.at_level("WARNING"):
        topo = mesh_topology(box, components)

    assert topo["genus"] == 0
    assert any("genus" in r.message for r in caplog.records), (
        f"expected a warning about genus failure, got: {[r.message for r in caplog.records]}"
    )


def test_in_q_put_oserror_maps_to_part_upload_error(monkeypatch):
    """When the pipe's recv() raises OSError (BrokenPipeError, etc.),
    ``repair_with_pmf`` maps it to ``PartUploadError`` with the
    'malformed worker reply' message (the pipe is the send channel in the
    new ``_RepairWorkerProcess`` design — the payload is passed via the
    Process instance, not a queue)."""
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError

    box = _box_mesh()

    # The cached spawn context is shared across calls; patch Pipe on
    # it via monkeypatch (auto-restored at teardown — no leftover state
    # for other tests that patch the same cached context).
    spawn_ctx = part_repair_mod.multiprocessing.get_context("spawn")
    monkeypatch.setattr(spawn_ctx, "Pipe", _failing_pipe_factory)

    with pytest.raises(PartUploadError) as excinfo:
        repair_with_pmf(box, timeout=5)

    # The pipe's recv() raises OSError → the parent maps it to a
    # PartUploadError with 'malformed worker reply'.
    assert "malformed worker reply" in str(excinfo.value)


class _DeadWorkerProcess(multiprocessing.Process):
    """A top-level ``Process`` subclass whose ``run()`` exits immediately
    WITHOUT sending a reply over the pipe.

    Models a worker child that dies before it can ship ``("ok", ...)`` —
    an interpreter crash, an uncaught error in module-import/setup code, or
    anything else that stops the child before ``send`` runs. The parent
    must detect this FAST (the 100 ms poll loop sees ``is_alive()`` go
    False → the 422) and map it to ``PartUploadError`` in under a few
    seconds, never waiting out the full 120 s budget.

    Top-level in an importable module (the spawn child inherits the parent's
    ``sys.path``), so the child can resolve the class by its import path.
    """

    def run(self) -> None:
        import os

        os._exit(0)


def test_dead_child_exits_without_reply_maps_quickly(monkeypatch):
    """A worker child that exits immediately WITHOUT sending a reply must be
    mapped to ``PartUploadError`` FAST — in under 5 s, NOT after the full
    ``timeout`` budget.

    This is the ``is_alive()`` early-exit in ``_run_in_worker``: a dead
    child (the stub calls ``os._exit(1)``) is detected within ~100 ms by
    the short-interval poll loop, and the parent raises ``PartUploadError``
    immediately — it does NOT wait for the (arbitrarily large) timeout to
    expire first.

    The test points the real repair seam at a no-op worker AND overrides the
    process with ``_DeadWorkerProcess`` (which calls ``os._exit(1)``
    without sending a reply). The ``timeout=120`` budget is the production
    default: if the fast path were broken, the call would wait out the full
    120 s budget. The < 5 s assertion proves the fast path fired.
    """
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError
    from tests._repair_stubs import _noop_repair

    monkeypatch.setattr(part_repair_mod, "_REPAIR_WORKER", _noop_repair)

    # Override the process with a thin factory returning the dead-child
    # process (the payload carries the box's arrays — the dead child ignores
    # them and exits without sending a reply).
    def _dead_factory(pipe_child, worker, mode, payload):
        return _DeadWorkerProcess()

    monkeypatch.setattr(part_repair_mod, "_RepairWorkerProcess", _dead_factory)

    box = _box_mesh()
    t0 = time.monotonic()
    with pytest.raises(PartUploadError) as excinfo:
        # timeout=120 is the production default (REPAIR_TIMEOUT_SECONDS).
        # If the dead-child fast path were broken, this call would wait out
        # the full 120 s budget — the < 5 s assertion proves the fast path
        # fired instead.
        repair_with_pmf(box, timeout=120)
    elapsed = time.monotonic() - t0

    # Fast: the dead child is detected without waiting the 120 s budget.
    assert elapsed < 5.0, (
        f"dead child was not detected quickly: {elapsed:.2f}s elapsed "
        f"(expected < 5s via the is_alive() early-exit)"
    )
    # Mapped to PartUploadError (the 422 signal), not a raw crash or timeout.
    assert isinstance(excinfo.value, PartUploadError)
    # The error message identifies the dead child, not a generic failure.
    assert "exited without" in str(excinfo.value).lower(), (
        f"error message does not name the dead child: {excinfo.value}"
    )
    # The child was cleaned up (no zombie/orphan left behind).
    import multiprocessing as _mp

    assert _mp.active_children() == [], (
        f"leftover child processes: {_mp.active_children()}"
    )


class _MalformedReplyProcess(multiprocessing.Process):
    """A top-level ``Process`` subclass whose ``run()`` sends a MALFORMED
    reply (not the 3-tuple ``(kind, name, payload)`` the parent expects)
    over the real pipe, then exits.

    Two malformed shapes (parametrised):
    - ``"short"``  → a 2-tuple ``("ok", None)`` (wrong length); the parent
      must reject it as malformed (not a 3-tuple).
    - ``"kind"``   → a 3-tuple ``("weird", "x", "y")`` (wrong kind); the
      parent must reject it as malformed (kind not in {"ok", "err"}).

    This drives the REAL ``_run_in_worker`` validation (no monkeypatch of
    the function under test) — the only thing injected is the process
    class (the same seam ``_DeadWorkerProcess`` uses). The instance is
    pickled to the spawn child (carrying the reply), and the child's
    ``run()`` sends it over the real pipe. Top-level in an importable
    module (the spawn child inherits the parent's ``sys.path``), so the
    child can resolve the class by its import path."""

    def __init__(
        self, send_conn, worker, mode, payload, malformed_kind: str = "short"
    ):
        super().__init__(daemon=True)
        self._send_conn = send_conn
        self._malformed_kind = malformed_kind

    def run(self) -> None:
        import sys

        try:
            if self._malformed_kind == "short":
                self._send_conn.send(("ok", None))
            else:  # "kind"
                self._send_conn.send(("weird", "x", "y"))
        except (OSError, BrokenPipeError):
            # The send can fail (pipe closed, child killed); the process
            # exits regardless.
            pass
        finally:
            sys.exit(0)


@pytest.mark.parametrize("malformed_kind", ["short", "kind"])
def test_malformed_worker_reply_is_part_upload_error(
    monkeypatch, malformed_kind: str
) -> None:
    """A malformed reply (wrong length, or a kind not in {"ok","err"}) must
    become ``PartUploadError("repair failed: malformed worker reply")`` —
    never a crash or a 500.

    The test exercises the REAL ``_run_in_worker`` reply-shape validation
    (no monkeypatch of the function under test — the earlier version
    patched ``_run_in_worker`` itself, which made the test tautological).
    It injects only the process class (``_MalformedReplyProcess``, the same
    seam ``_DeadWorkerProcess`` uses), whose ``run()`` sends a malformed
    reply over the real pipe; ``repair_with_pmf`` then drives the real
    ``_run_in_worker``, which must reject it.

    Parametrised over two malformed shapes: a wrong length (2-tuple
    ``("ok", None)``) and an unknown kind (3-tuple ``("weird", "x", "y")``).
    """
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError
    from tests._repair_stubs import _noop_repair

    # Point the seam at a no-op worker (the stub is pickled to the child
    # but never runs — the process is overridden below).
    monkeypatch.setattr(part_repair_mod, "_REPAIR_WORKER", _noop_repair)

    # Override the process class with the malformed-reply stub (the same
    # seam the dead-child test uses). The payload carries the box's arrays
    # — the stub ignores them and sends the malformed reply instead.
    def _malformed_factory(pipe_child, worker, mode, payload):
        return _MalformedReplyProcess(
            pipe_child, worker, mode, payload, malformed_kind
        )

    monkeypatch.setattr(
        part_repair_mod, "_RepairWorkerProcess", _malformed_factory
    )

    box = _box_mesh()
    t0 = time.monotonic()
    with pytest.raises(PartUploadError) as excinfo:
        repair_with_pmf(box, timeout=120)
    elapsed = time.monotonic() - t0

    # The malformed reply is detected quickly (the real reply-shape
    # validation in _run_in_worker), not after the full 120 s budget.
    assert elapsed < 5.0, (
        f"malformed reply not rejected quickly: {elapsed:.2f}s elapsed "
        f"(expected < 5s via the real _run_in_worker validation)"
    )
    # Mapped to PartUploadError (the 422 signal), never a raw crash.
    assert isinstance(excinfo.value, PartUploadError)
    assert "malformed" in str(excinfo.value), (
        f"malformed reply must map to PartUploadError naming it, "
        f"got: {excinfo.value!r}"
    )
    # The child was cleaned up (no zombie/orphan left behind).
    assert multiprocessing.active_children() == [], (
        f"leftover child processes: {multiprocessing.active_children()}"
    )


def test_multi_body_aggregate_budget_raises_on_timeout(monkeypatch: pytest.MonkeyPatch):
    """The multi-body repair is bounded by an aggregate wall-clock budget
    (``REPAIR_TIMEOUT_SECONDS`` in production; a short injected timeout
    here) — exercised at the REAL process boundary through the batch
    path (``mode="batch"``, one child for all bodies).

    The worker is a GUARANTEED-EXCEEDING stub (``_slow_batch_repair`` —
    a ~5 s C-level spin), so the outcome never races pymeshfix's real
    startup cost (an earlier version raced a real two-box pymeshfix batch
    against a 50 ms timeout and failed on CI when the child finished
    first). With ``timeout=0.5`` the deadline fires ~0.5 s after the
    spawn, the child is killed, and ``RepairTimeoutError`` (the 422
    signal) is raised within 3 s with no active children afterwards.
    """
    import trimesh

    import d33d.part_repair as part_repair_mod
    from tests._repair_stubs import _slow_batch_repair

    monkeypatch.setattr(part_repair_mod, "_repair_bodies_in_process", _slow_batch_repair)

    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0, 0])

    t0 = time.monotonic()
    with pytest.raises(RepairTimeoutError):
        repair_bodies_with_pmf([box_a, box_b], timeout=0.5)
    elapsed = time.monotonic() - t0

    assert elapsed < 3.0, (
        f"batch timeout fired after {elapsed:.2f}s — expected < 3s "
        f"(spawn startup ~0.5–1s + the 0.5s budget)"
    )
    # The worker child must be dead (no zombie/orphan) — the ``finally``
    # block kills and joins it before the exception propagates.
    assert multiprocessing.active_children() == [], (
        f"leftover child processes: {multiprocessing.active_children()}"
    )


# ---------------------------------------------------------------------------
# Lens round 2: ONE repair process per import (multi-body), sanitized child
# errors, REPAIR_TIMEOUT_DETAIL home in part_errors, _decimate moved to
# part_repair
# ---------------------------------------------------------------------------


def _two_boxes_plus_debris_stl() -> bytes:
    """Two watertight boxes far apart + one gapped debris shell → a NOT-clean
    two-body mesh (the debris shell makes bodies > watertight_bodies)."""
    import trimesh

    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0, 0])
    debris = trimesh.creation.box(extents=[5, 5, 5])
    debris.apply_translation([20000.0, 0, 0])
    debris = trimesh.Trimesh(debris.vertices, debris.faces[:-6], process=False)
    combined = trimesh.util.concatenate([box_a, box_b, debris])
    return _stl_bytes_from_mesh(combined)


def test_multi_body_repairs_in_single_process(monkeypatch: pytest.MonkeyPatch):
    """Lens round 2 (performance): a multi-body import repairs ALL watertight
    bodies in ONE spawned child process — the per-body repair loop used to
    spawn a fresh process per body (0.5–1 s of spawn overhead each; fifty
    small bodies cost tens of seconds and could hit the 120 s aggregate
    budget). A wrapper around ``repair_bodies_with_pmf`` asserts exactly
    ONE repair call for a six-body import (one child, not one per body)."""
    # Six watertight boxes 10000 mm apart (the float32 STL round-trip
    # separation that keeps bodies distinct) + one gapped debris shell
    # (makes the mesh NOT clean → the multi-body repair branch runs).
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    boxes = []
    for i in range(6):
        b = trimesh.creation.box(extents=[10, 10, 10])
        b.apply_translation([10000.0 * (i + 1), 0, 0])
        boxes.append(b)
    debris = trimesh.creation.box(extents=[5, 5, 5])
    debris.apply_translation([10000.0 * 7, 0, 0])
    debris = trimesh.Trimesh(debris.vertices, debris.faces[:-6], process=False)
    combined = trimesh.util.concatenate(boxes + [debris])
    data = _stl_bytes_from_mesh(combined)

    # Count the repair calls: patch the ``_REPAIR_WORKER`` hook (the seam
    # the single-body path uses) — but the multi-body path uses
    # ``repair_bodies_with_pmf`` directly, so instead we patch the module
    # function with a wrapper that counts and delegates. The count is
    # observed in the PARENT (the wrapper runs in the parent, the real
    # repair runs in the child).
    real_repair = part_mesh_mod.repair_bodies_with_pmf
    call_counts = {"n": 0}

    def _counting_repair(meshes, timeout=None):
        call_counts["n"] += 1
        return real_repair(meshes, timeout=timeout)

    monkeypatch.setattr(part_mesh_mod, "repair_bodies_with_pmf", _counting_repair)

    mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    assert report["bodies"] == 6, f"six-body import must keep 6 bodies: {report}"
    # Exactly ONE repair call for a six-body import (one child process,
    # not one per body).
    assert call_counts["n"] == 1, (
        f"multi-body repair must make exactly ONE repair call, "
        f"got {call_counts['n']}"
    )
    # The single call handled all six bodies — per-body semantics kept
    # (every body present in the stored mesh, the real pymeshfix ran).
    comps = mesh.split(only_watertight=False)
    watertight = [c for c in comps if c.is_watertight]
    assert len(watertight) == 6, f"stored mesh must keep all 6 bodies: {report}"


def test_multi_body_two_body_fixture_keeps_two_bodies():
    """Lens round 2: the two-body fixture (two_body_multisolid.stl) still
    keeps BOTH watertight bodies after the real (one-process) repair —
    per-body repair semantics (issue #375) are preserved by the batched
    repair."""

    import d33d.part_mesh as part_mesh_mod

    data = (FIXTURES / "two_body_multisolid.stl").read_bytes()
    mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    assert report["bodies"] == 2, f"two-body fixture must keep 2 bodies: {report}"
    comps = mesh.split(only_watertight=False)
    watertight = [c for c in comps if c.is_watertight]
    assert len(watertight) == 2, f"stored mesh must keep 2 bodies: {report}"


def test_child_error_carries_type_name_only(monkeypatch: pytest.MonkeyPatch):
    """Lens round 2 (security): a non-PartUploadError raised in the repair
    child is wrapped as ``PartUploadError(f"repair failed: {name}")`` where
    ``name`` is the exception's TYPE — the child's message (``payload``)
    must NOT appear in the wrapped error (the payload can echo arbitrary
    child-side text into the 422 response). The full payload is logged in
    the parent instead."""
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError
    from tests._repair_stubs import _raise_error_repair

    mesh = _box_mesh()
    # Force the child-side failure through the real process boundary:
    # point the worker at a top-level stub (importable by the spawn
    # child) that raises a RuntimeError whose message must not leak into
    # the wrapped error.
    monkeypatch.setattr(part_repair_mod, "_REPAIR_WORKER", _raise_error_repair)

    with pytest.raises(PartUploadError) as excinfo:
        repair_with_pmf(mesh, timeout=10)

    msg = str(excinfo.value)
    assert msg == "repair failed: RuntimeError", (
        f"wrapped child error must carry only the type name, got: {msg!r}"
    )
    assert "LEAK_MARKER" not in msg


def test_child_error_logs_child_message_not_payload(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
):
    """Lens round 3 (bug fix): the parent's log for a child-side failure
    carries the CHILD's message (``payload_result``), not the INPUT mesh
    payload. The old code logged ``payload`` — the pickled ``(verts, faces)``
    input arrays — so a large mesh's array repr landed in the server log
    while the actual child-side error text was discarded. The log must
    show the child's message text and no array repr."""
    import logging

    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError
    from tests._repair_stubs import _raise_error_repair

    # A small mesh whose array repr is recognizable: the payload's verts
    # array repr must NOT appear in the log (the old bug logged the
    # payload, not the child's message).
    mesh = _box_mesh()

    monkeypatch.setattr(part_repair_mod, "_REPAIR_WORKER", _raise_error_repair)

    with caplog.at_level(
        logging.ERROR, logger="d33d.part_repair"
    ), pytest.raises(PartUploadError):
        repair_with_pmf(mesh, timeout=10)

    joined = "\n".join(caplog.text.splitlines())
    # The child's own message text is logged (bounded to 500 chars).
    assert "LEAK_MARKER" in joined, (
        f"the child's message must appear in the log, got:\n{joined}"
    )
    # The INPUT mesh arrays (the payload) must NOT be logged.
    verts_repr = str(mesh.vertices)
    faces_repr = str(mesh.faces)
    assert verts_repr not in joined, "the input vertices array must not be logged"
    assert faces_repr not in joined, "the input faces array must not be logged"
    # The line is bounded: no unbounded array repr (a 500-char cap on the
    # child message; the payload arrays are gone entirely).
    assert "array(" not in joined or "LEAK_MARKER" in joined.split("array(")[0]


def test_repair_timeout_detail_lives_in_part_errors():
    """Lens round 2: ``REPAIR_TIMEOUT_DETAIL`` is defined in
    ``d33d.part_errors`` (the leaf module) — part_import and the tests
    import it from there; part_repair no longer re-exports it."""
    from d33d.part_errors import REPAIR_TIMEOUT_DETAIL as detail

    assert "simplifying" in detail
    import d33d.part_repair as part_repair_mod

    assert not hasattr(part_repair_mod, "REPAIR_TIMEOUT_DETAIL"), (
        "part_repair must no longer carry REPAIR_TIMEOUT_DETAIL"
    )


def test_decimate_and_budget_moved_to_part_repair(monkeypatch: pytest.MonkeyPatch):
    """Lens round 2: ``_decimate`` and ``REPAIR_FACE_BUDGET`` live in
    ``d33d.part_repair`` now (part_mesh stays lean); part_mesh re-exports
    them so the existing monkeypatch targets keep working."""
    import d33d.part_mesh as part_mesh_mod
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError

    assert hasattr(part_repair_mod, "_decimate"), "_decimate must live in part_repair"
    assert hasattr(part_repair_mod, "REPAIR_FACE_BUDGET"), (
        "REPAIR_FACE_BUDGET must live in part_repair"
    )
    # part_mesh re-exports both (the tests monkeypatch them there).
    assert part_mesh_mod.REPAIR_FACE_BUDGET == part_repair_mod.REPAIR_FACE_BUDGET

    import trimesh

    box = trimesh.creation.box(extents=[10, 10, 10])  # 12 faces > 5

    def _raise_oserror(self, face_count=None, **kw):
        raise OSError("C library exploded")

    monkeypatch.setattr(trimesh.Trimesh, "simplify_quadric_decimation", _raise_oserror)

    with pytest.raises(PartUploadError) as excinfo:
        part_repair_mod._decimate(box, 5)

    assert "quadric decimation failed" in str(excinfo.value)
    assert "OSError" in str(excinfo.value)

    # The part_mesh re-export still works on the moved implementation.
    small = trimesh.creation.box(extents=[10, 10, 10])
    assert part_mesh_mod._decimate(small, 100) is small  # below target → unchanged

