"""Issue #414 (part 3): the legacy-report mesh load must not block the
event loop.

``stored_part_bounds_mm`` used to load the part mesh (up to 50 MB / 2 M
faces) synchronously on the EVENT-LOOP thread inside the async
``post_chat`` handler — on every hole-noun turn for a legacy project.
This module pins the fix:

- the project row + part path resolve ON the loop (sqlite is
  thread-bound);
- ONLY the mesh load goes off the loop via ``asyncio.to_thread``;
- the result is CACHED: ``bbox_bounds_file_units`` is written back into
  the stored part report (``part_report`` on the project row), so the
  mesh is loaded at most once per legacy project, not every turn.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

from d33d import fill_recut_turn as frt

# Shared helpers (issue #414, round 2, LOW).
from tests.legacy_bounds_fixtures import (
    _commit_stl,
    _legacy_report,
    _make_stl,
    _set_legacy_part,
)


def test_legacy_mesh_load_does_not_block_the_event_loop(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #414 (adversarial review): the mesh load must not block the
    EVENT LOOP. A thread + join (``threading.Event.wait``) on the calling
    thread is a FAKE fix — the load goes off the loop's thread but the
    loop's thread still waits the whole time. This test proves the loop
    STAYS RESPONSIVE: a concurrent coroutine ticks every 10 ms while a
    legacy fill-recut turn runs, and a patched ``_load_mesh_bounds``
    sleeps 0.5 s. If the loop is blocked, the ticker makes no progress
    during the load; if the load is genuinely off the loop
    (``await asyncio.to_thread`` in the async caller), the ticker keeps
    ticking through the whole 0.5 s.

    This replaces the thread-identity test as the responsiveness pin:
    thread identity alone cannot distinguish "the loop waits on a worker"
    (blocked) from "the loop awaits a future" (responsive)."""
    import asyncio
    import time

    stl = _make_stl()
    original_load = frt._load_mesh_bounds

    def _slow_load(part_path: Any, fmt: Any):
        time.sleep(0.5)  # simulate a slow (large) mesh parse
        return original_load(part_path, fmt)

    monkeypatch.setattr(frt, "_load_mesh_bounds", _slow_load)

    async def _turn():
        conn = app_with_projects.state.conn
        pid = conn.create_project(name="Ticker")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)

        ticks = 0
        load_running = False

        async def _ticker():
            nonlocal ticks
            while load_running:
                ticks += 1
                await asyncio.sleep(0.01)

        ticker = asyncio.create_task(_ticker())
        load_running = True
        await frt.fill_recut_turn_async(app_with_projects, pid, "make the center hole 8 mm")
        load_running = False
        await asyncio.sleep(0.0)  # let the ticker finish its final sleep
        ticker.cancel()
        try:
            await ticker
        except asyncio.CancelledError:
            pass
        return ticks

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            return await _turn()

    ticks = asyncio.run(_run())
    # The load slept 0.5 s; a responsive loop ticks ~every 10 ms, so the
    # ticker must make progress DURING the load (at least a few ticks).
    # A blocked loop (thread + join / Event.wait) makes ZERO progress.
    assert ticks > 3, (
        f"the event loop was blocked during the mesh load — only {ticks} "
        f"ticker ticks in ~0.5 s (a responsive loop makes ~50). The load "
        "must be awaited via asyncio.to_thread, not thread+join."
    )


def test_legacy_second_turn_does_not_reload_mesh(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After the first legacy turn the result is CACHED in the stored
    part report (``bbox_bounds_file_units`` written back), so a second
    fresh hole-noun turn must NOT load the mesh again."""
    import asyncio

    stl = _make_stl()
    load_count = 0

    original_load = frt._load_mesh_bounds

    def _spy_load(part_path: Any, fmt: Any):
        nonlocal load_count
        load_count += 1
        return original_load(part_path, fmt)

    monkeypatch.setattr(frt, "_load_mesh_bounds", _spy_load)

    async def _two_turns():
        conn = app_with_projects.state.conn
        pid = conn.create_project(name="Cached")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)

        # First turn: the legacy report has no bounds → the mesh load
        # runs and the file bounds are cached into the stored report.
        frt.fill_recut_turn(app_with_projects, pid, "make the center hole 8 mm")
        first = load_count
        # The cache write must have landed (the second turn's bounds come
        # from the report, not the mesh).
        raw = conn.raw.execute(
            "SELECT part_report FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        assert "bbox_bounds_file_units" in json.loads(raw), (
            "cache write did not land — the second turn would re-load: "
            f"{raw}"
        )
        # Clear the stored offer so the second turn re-runs the FRESH-turn
        # path (otherwise the turn is a clean "no" on the live offer and
        # never reaches the bounds code at all — the cache would be
        # untested).
        app_with_projects.state.versions.set_pending_offer(pid, None)

        # Second turn: the report now carries the cached bounds →
        # ``report_bounds_mm`` succeeds directly and the legacy fallback
        # (the mesh load) must not fire.
        frt.fill_recut_turn(app_with_projects, pid, "make the center hole 8 mm")
        return first, load_count

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            return await _two_turns()

    first, total = asyncio.run(_run())
    assert first == 1, f"the first turn must load the mesh exactly once, got {first}"
    assert total == 1, (
        f"the second turn must NOT load the mesh again (the report now has "
        f"bbox_bounds_file_units); total loads: {total}"
    )


def test_legacy_bounds_cached_into_stored_report(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """After a legacy turn, the stored ``part_report`` row carries
    ``bbox_bounds_file_units`` — the cache write landed on the project
    row (read back through the raw DB, not the in-process dict)."""
    import asyncio

    stl = _make_stl()

    async def _one_turn():
        conn = app_with_projects.state.conn
        pid = conn.create_project(name="CacheWrite")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)
        frt.fill_recut_turn(app_with_projects, pid, "make the center hole 8 mm")
        raw = conn.raw.execute(
            "SELECT part_report FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        return json.loads(raw)

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            return await _one_turn()

    report = asyncio.run(_run())
    assert "bbox_bounds_file_units" in report, (
        "the mesh-derived bounds were not written back to the stored "
        f"part report (cache miss — every turn would re-load the mesh): {report}"
    )


def test_legacy_mesh_load_type_error_degrades_to_extents_fallback(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #414 (round 2, HIGH): errors from ``load_part_geometry``
    OUTSIDE its ``PartUploadError`` wrapping — e.g. a 3MF ``Scene``
    ``to_mesh()`` raising ``TypeError``/``AttributeError`` — must NOT
    escape the loader and lose the turn: the turn degrades to the
    extents/2 fallback (a warning is logged, the turn completes with an
    offer or no-match) instead of 500-ing the chat route."""
    import asyncio

    from d33d import part_mesh

    def _raising_load(raw: bytes, fmt: str):
        raise TypeError("scene has no geometry")

    monkeypatch.setattr(part_mesh, "load_part_geometry", _raising_load)
    stl = _make_stl()

    async def _turn():
        pid = app_with_projects.state.conn.create_project(name="TypeErrorLoad")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)
        return frt.fill_recut_turn(app_with_projects, pid, "make the center hole 8 mm")

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            return await _turn()

    result = asyncio.run(_run())

    assert result is not None, (
        "the turn must complete (degraded to the extents/2 fallback), not "
        "drop out with an escaped TypeError from load_part_geometry"
    )
    assert result["kind"] == "answer"
    # The legacy report's single hole at (5, 5) sits exactly at the
    # extents/2 centre (bbox_file_units [10, 10, 6] → centre (5, 5)) —
    # the turn completes a fresh offer, never raising.
    assert result["outcome"] == "fresh_offer", result


def test_legacy_async_mesh_load_timeout_degrades_to_extents_fallback(
    app_with_projects, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Issue #414 (round 2, MEDIUM): the off-loop mesh load is BOUNDED
    — ``asyncio.wait_for(asyncio.to_thread(...), LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS)``.
    A loader slower than the (shortened) timeout degrades to the
    extents/2 fallback with a logged ``TimeoutError`` instead of
    holding the turn open indefinitely."""
    import asyncio
    import time

    stl = _make_stl()

    def _slow_load(part_path: Any, fmt: Any):
        # 1.0 s sleep — truly EXCEEDS the monkeypatched 0.2 s bound (the
        # old 0.5 s patch value was the reviewer's flake vector: with the
        # unbounded default of 20 s a 0.5 s load never timed out, so the
        # test pinned nothing; sleep(1.0) vs 0.2 s always times out).
        time.sleep(1.0)

    monkeypatch.setattr(frt, "_load_mesh_bounds", _slow_load)
    monkeypatch.setattr(frt, "LEGACY_BOUNDS_LOAD_TIMEOUT_SECONDS", 0.2)

    async def _turn():
        conn = app_with_projects.state.conn
        pid = conn.create_project(name="TimeoutLoad")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)
        result = await frt.fill_recut_turn_async(
            app_with_projects, pid, "make the center hole 8 mm"
        )
        # Read the cache state while the DB is still open (inside the
        # lifespan context).
        raw = conn.raw.execute(
            "SELECT part_report FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        return result, json.loads(raw) if raw else {}

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            return await _turn()

    with caplog.at_level("WARNING"):
        result, report = asyncio.run(_run())
    assert result is not None, (
        "the turn must degrade to the extents/2 fallback after the load "
        "timeout — not hang or raise"
    )
    assert result["outcome"] in ("fresh_offer", "no_match")
    # The timeout must be LOGGED (a silent fallback hides the pathology).
    assert any(
        "TimeoutError" in rec.message or "timed out" in rec.message
        for rec in caplog.records
        if rec.levelno >= logging.WARNING
    ), (
        "the load timeout must be logged as a warning; no such record: "
        + repr([r.getMessage() for r in caplog.records])
    )
    # No cache write landed (the load never completed): the stored
    # report must still lack ``bbox_bounds_file_units``.
    assert "bbox_bounds_file_units" not in report, (
        f"the timed-out load must not cache a result: {report}"
    )
