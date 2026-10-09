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
import threading
from pathlib import Path
from typing import Any

import pytest

from d33d import fill_recut_turn as frt

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


def _make_stl() -> bytes:
    """A small, valid, non-empty STL (the off_centre_plate fixture — 0..120
    x 0..80 x 0..6 in file units)."""
    return (FIXTURES / "off_centre_plate.stl").read_bytes()


def _set_legacy_part(app: Any, pid: int, report: dict) -> None:
    conn = app.state.conn
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status='settled', part_scale=1.0, part_report=? "
        "WHERE id=?",
        (json.dumps(report), pid),
    )
    conn.commit()


def _commit_stl(app: Any, pid: int, stl_bytes: bytes) -> None:
    """Commit part.stl to the project's v1 dir (mirrors the helper in
    test_fill_recut_legacy_bounds.py)."""
    conn = app.state.conn
    v1 = conn.raw.execute(
        "SELECT * FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (pid,),
    ).fetchone()
    if v1 is None:
        conn.raw.execute(
            "INSERT INTO versions (project_id, name, params, param_meta) "
            "VALUES (?, ?, ?, NULL)",
            (pid, "v1", "{}"),
        )
        conn.commit()
        v1 = conn.raw.execute(
            "SELECT * FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
    repo = Path(
        conn.raw.execute(
            "SELECT git_repo_path FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
    )
    target_dir = repo / "versions" / str(v1["id"])
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "part.stl").write_bytes(stl_bytes)


def _legacy_report() -> dict:
    return {
        "hole_count": 1,
        "holes": [
            {"center": [5.0, 5.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        ],
        "bbox_file_units": [10.0, 10.0, 6.0],
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path]):
    import d33d.db as db_mod
    from d33d.app import create_app

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        base = app_paths["tmp"] / "repos" / uuid.uuid4().hex[:12]
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
        "tmp": tmp_path,
    }


def test_legacy_mesh_load_runs_off_the_loop_thread(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The mesh load must NOT run on the event-loop thread.

    A spy on ``_load_mesh_bounds`` records ``threading.current_thread()``
    at call time; the caller records its own thread before invoking
    ``fill_recut_turn``. The two must be DIFFERENT threads — the load
    went through ``asyncio.to_thread`` (the default executor), not
    straight onto the loop."""
    import asyncio

    stl = _make_stl()
    original_load = frt._load_mesh_bounds
    worker_threads: list[threading.Thread] = []
    loop_thread: list[threading.Thread] = []

    def _spy_load(part_path: Any, fmt: Any):
        worker_threads.append(threading.current_thread())
        return original_load(part_path, fmt)

    monkeypatch.setattr(frt, "_load_mesh_bounds", _spy_load)

    async def _turn():
        conn = app_with_projects.state.conn
        pid = conn.create_project(name="OffLoop")
        _set_legacy_part(app_with_projects, pid, _legacy_report())
        _commit_stl(app_with_projects, pid, stl)
        loop_thread.append(threading.current_thread())
        frt.fill_recut_turn(app_with_projects, pid, "make the center hole 8 mm")

    async def _run():
        async with app_with_projects.router.lifespan_context(app_with_projects):
            await _turn()

    asyncio.run(_run())
    assert worker_threads, "the mesh load did not run"
    assert loop_thread, "the caller thread was not recorded"
    assert any(
        w is not lt for w, lt in zip(worker_threads, loop_thread)
    ), (
        "the mesh load ran on the event-loop (caller) thread — it must be "
        f"dispatched via asyncio.to_thread. worker={worker_threads!r} "
        f"loop={loop_thread!r}"
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
    # Patch ``report_bounds_mm`` where ``fill_recut_turn`` LOOKS it up
    # (module attribute) and count direct calls — the second turn must
    # hit the cached bounds directly, so the fallback (and therefore
    # the mesh load) must not fire again.
    from d33d import hole_select

    direct_bounds_calls = 0
    original_rbm = hole_select.report_bounds_mm

    def _counting_rbm(report, scale):
        nonlocal direct_bounds_calls
        result = original_rbm(report, scale)
        if result is not None:
            direct_bounds_calls += 1
        return result

    import d33d.fill_recut_turn as frt_mod

    monkeypatch.setattr(frt_mod, "report_bounds_mm", _counting_rbm)

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
