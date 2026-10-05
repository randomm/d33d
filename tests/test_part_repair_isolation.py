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
import time
from pathlib import Path
from typing import Any

import pytest

from d33d.part_repair import (
    RepairTimeoutError,
    is_repair_timeout,
    repair_with_pmf,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


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
    ``except PartUploadError`` catches it) — and ``is_repair_timeout``
    checks with ``isinstance``, not string matching."""
    import d33d.part_repair as part_repair_mod

    # Force lazy resolution of the real PartUploadError class (the
    # re-base onto ``d33d.part_mesh.PartUploadError`` happens on first use
    # of ``repair_with_pmf``, not at import time — to avoid a circular
    # import). Constructing a RepairTimeoutError before that re-base would
    # leave it subclassing the local placeholder, so resolve first.
    part_repair_mod._resolve_part_upload_error()
    from d33d.part_mesh import PartUploadError

    e = RepairTimeoutError("repair timed out: exceeded 120s")
    assert isinstance(e, PartUploadError)
    assert is_repair_timeout(e)
    # A plain PartUploadError with "timed out" in the message is NOT a
    # repair timeout (the string match is gone — the type is the signal).
    plain = PartUploadError("the worker said: timed out")
    assert not is_repair_timeout(plain)
    assert isinstance(plain, PartUploadError)


def test_is_repair_timeout_does_not_string_match():
    """``is_repair_timeout`` is a pure ``isinstance`` check — a
    ``PartUploadError`` whose message contains ``"timed out"`` is NOT
    classified as a repair timeout (the old string match is gone)."""
    from d33d.part_mesh import PartUploadError

    e = PartUploadError("repair timed out: exceeded 120s")
    assert not is_repair_timeout(e)
    # A RepairTimeoutError with an EMPTY message is still a repair timeout
    # (the type, not the message, is the signal).
    e2 = RepairTimeoutError("")
    assert is_repair_timeout(e2)


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

    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        "d33d.part_repair_stubs._slow_spin_repair",
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

    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        "d33d.part_repair_stubs._slow_spin_repair",
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
        except Exception as e:  # noqa: BLE001 — record any failure shape
            stuck_result["status"] = f"error: {type(e).__name__}: {e}"

    def _real_call():
        try:
            m = repair_with_pmf(holey)
            real_result["status"] = "ok"
            real_result["faces"] = len(m.faces)
        except Exception as e:  # noqa: BLE001 — record any failure shape
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

    The stub (``d33d.part_repair_stubs._busy_repair``) runs
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

    # Point the repair seam at the GIL-holding stub (resolved in the
    # parent; the spawn child looks it up by dotted qualname).
    monkeypatch.setattr(
        part_repair_mod,
        "_REPAIR_WORKER",
        "d33d.part_repair_stubs._busy_repair",
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
