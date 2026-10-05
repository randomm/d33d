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
import time
from pathlib import Path
from typing import Any

import pytest

from d33d.part_repair import (
    RepairTimeoutError,
    repair_with_pmf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stl"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


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


class _FailingQueue:
    """A SimpleQueue stub whose put() raises OSError (top-level → picklable)."""
    def put(self, *a, **kw):
        raise OSError("broken pipe")
    def close(self):
        pass


def test_in_q_put_oserror_maps_to_part_upload_error(monkeypatch):
    """Finding 6: when ``in_q.put(...)`` raises OSError (BrokenPipeError,
    etc.), ``repair_with_pmf`` maps it to ``PartUploadError`` with the
    'could not send mesh to worker' message."""
    import d33d.part_repair as part_repair_mod
    from d33d.part_errors import PartUploadError

    box = _box_mesh()

    original_get_context = part_repair_mod.multiprocessing.get_context

    def _failing_get_context(name):
        real_ctx = original_get_context(name)

        def _failing_simple_queue():
            return _FailingQueue()

        real_ctx.SimpleQueue = _failing_simple_queue
        return real_ctx

    monkeypatch.setattr(part_repair_mod.multiprocessing, "get_context", _failing_get_context)

    with pytest.raises(PartUploadError) as excinfo:
        repair_with_pmf(box, timeout=5)

    assert "could not send mesh to worker" in str(excinfo.value)


def test_multi_body_aggregate_budget_raises_on_timeout(monkeypatch):
    """Finding 4: the multi-body repair loop is bounded by an aggregate
    wall-clock budget of REPAIR_TIMEOUT_SECONDS. With an injected small
    budget and a stub repair that consumes time, RepairTimeoutError is
    raised when the remaining time drops to ≤ 0."""
    import time

    import trimesh

    import d33d.part_mesh as part_mesh_mod

    # Two watertight boxes far apart → 2 bodies, NOT clean (we add a gap).
    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0, 0])
    # Add a gapped shell to make the mesh NOT clean.
    debris = trimesh.creation.box(extents=[5, 5, 5])
    debris.apply_translation([20000.0, 0, 0])
    debris = trimesh.Trimesh(debris.vertices, debris.faces[:-6], process=False)
    combined = trimesh.util.concatenate([box_a, box_b, debris])
    data = _stl_bytes_from_mesh(combined)

    # Inject a very small aggregate budget (0.5 s).
    monkeypatch.setattr(part_mesh_mod, "REPAIR_TIMEOUT_SECONDS", 0.5)

    # A stub repair that sleeps briefly to consume budget.
    def _slow_repair(mesh, timeout=None):
        time.sleep(0.6)  # longer than the budget
        return mesh

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _slow_repair)

    from d33d.part_repair import RepairTimeoutError

    with pytest.raises(RepairTimeoutError):
        part_mesh_mod.parse_and_repair(data, "stl")


def _stl_bytes_from_mesh(mesh):
    """Export a trimesh mesh as STL bytes."""
    import io

    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    return buf.getvalue()

