"""Issue #414 (part 2): the legacy-report fallback.

Reports stored BEFORE this change lack ``bbox_bounds_file_units``, so
``report_bounds_mm`` returns ``None`` and ``fill_recut_turn`` fell back to
extents/2 — wrong for every off-origin part ALREADY imported. This module
pins the fallback: when the stored report has no real bounds, the part's
stored part mesh (the committed ``part.stl`` under
``{repo}/versions/{v1}/``) is loaded and ``mesh.bounds`` (scaled by
``part_scale``) supplies the centre reference.
"""

from __future__ import annotations

import asyncio
import itertools
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop (the test_projects
    pattern)."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


def _legacy_report_with_holes():
    """The QA v36 shape WITHOUT ``bbox_bounds_file_units``: the pre-#414
    (and pre-this-fallback) report — extents only, holes in file units."""
    return {
        "hole_count": 3,
        "holes": [
            {"center": [8.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
            {"center": [112.0, 8.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
            {"center": [112.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        ],
        "bbox_file_units": [120.0, 80.0, 6.0],
    }


def _set_legacy_part(app: Any, pid: int, report: dict) -> None:
    """Set the part columns with a LEGACY report (no bounds key) directly
    on the DB."""
    import json

    conn = app.state.conn
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status='settled', part_scale=1.0, part_report=? "
        "WHERE id=?",
        (json.dumps(report), pid),
    )
    conn.commit()


def _commit_stl(app: Any, pid: int, path: Path) -> None:
    """Commit a part.stl to the project's v1 directory (the layout
    ``_v1_part_path`` reads: ``{repo}/versions/{v1}/part.stl``). Inserts
    a v1 version row first when the project has none (legacy-row tests
    create the project but no import — the real import path writes both).
    """
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
    v1_id = v1["id"]
    repo = Path(
        conn.raw.execute(
            "SELECT git_repo_path FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
    )
    target_dir = repo / "versions" / str(v1_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    (target_dir / "part.stl").write_bytes(path.read_bytes())


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    """Isolated app with tmp-path repos (the test_projects pattern)."""
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
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
    }


def test_legacy_report_off_origin_plate_center_hole_is_no_match(app_with_projects) -> None:
    """Issue #414: a LEGACY report (no ``bbox_bounds_file_units``) on an
    OFF-ORIGIN part (the committed off_centre_plate.stl fixture, 0..120 x
    0..80, holes only at the corners) + "make the center hole 38 mm" must
    NOT pick a hole by extents/2 (60, 40) — the true centre of this plate
    IS (60, 40) but the nearest hole is 61 mm away, beyond the 30 mm
    threshold, so the correct answer is no_match (the list of measured
    holes), the same as the post-#414 report.

    Pre-fix: extents/2 (60, 40) happened to coincide with the true centre
    for THIS fixture; the stronger case is a translated part where the
    fallback would compute the centre from extents alone. The off-centre
    plate's holes are corner holes, so no_match is the pinned answer."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "LegacyOffOrigin"})
        pid = r.json()["id"]
        _set_legacy_part(app_with_projects, pid, _legacy_report_with_holes())
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the center hole 38 mm"}
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        assert source is not None
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        svc = app_with_projects.state.versions
        return r2.status_code, frames, svc.get_pending_offer(pid)

    status, frames, offer = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    msg = done[0].get("message", "")
    # Corner-only holes: no measured hole is within the 30 mm threshold of
    # the true centre (60, 40) — the reply must be the no-match list, not
    # a silent pick.
    assert "I don't see a hole" in msg, (
        f"expected no-match reply for corner-only holes on a legacy "
        f"off-origin report, got: {msg}"
    )
    # No offer is stored for a no-match.
    assert offer is None, f"no-match must not store an offer: {offer}"


def test_legacy_report_translated_part_center_hole_selects_true_center(app_with_projects) -> None:
    """Issue #414: a LEGACY report on a TRANSLATED part — the plate sits at
    (100, 100)..(220, 180) in its own coordinates, with a hole at its true
    centre (160, 140) and two corner holes — + "make the center hole 38 mm"
    must select the TRUE-centre hole (160, 140), NOT extents/2 (60, 40),
    which is 113 mm from the real centre and would no-match or mispick.

    The stored part.stl is a translated copy of the off_centre_plate
    fixture (generated once below — no trimesh booleans at test time)."""

    import trimesh

    plate = trimesh.load(str(FIXTURES / "off_centre_plate.stl"))
    plate.apply_translation([100.0, 100.0, 0.0])
    lo, hi = plate.bounds
    assert (float(lo[0]), float(lo[1])) == (100.0, 100.0)
    assert (float(hi[0]), float(hi[1])) == (220.0, 180.0)
    stl_bytes = plate.export(file_type="stl")

    # Report: legacy (no bounds key), holes in the part's own coordinates —
    # a hole at the TRUE centre (160, 140) plus the two translated corner
    # holes (108, 172) and (212, 108), all Ø4.
    report = {
        "hole_count": 3,
        "holes": [
            {"center": [160.0, 140.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
            {"center": [108.0, 172.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
            {"center": [212.0, 108.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        ],
        "bbox_file_units": [120.0, 80.0, 6.0],
    }

    from d33d.hole_select import select_measured_hole

    # Sanity: the selection itself must pick the true-centre hole from the
    # mesh-derived bounds — the bug is in the fallback's bounds wiring, not
    # the selector.
    holes = [
        {"center": [160.0, 140.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [108.0, 172.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        {"center": [212.0, 108.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
    ]
    picked = select_measured_hole(holes, "make the center hole 38 mm", [120.0, 80.0], 38.0, [[100.0, 100.0], [220.0, 180.0]])
    assert picked is not None and picked["center"][0] == 160.0, (
        f"sanity: the selector must pick the true-centre hole, got: {picked}"
    )

    # Commit the translated STL to the v1 dir, then run the chat turn.
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        f.write(stl_bytes)
        tmp_stl = Path(f.name)
    try:

        async def _call(client):
            r = await client.post("/api/projects", json={"name": "LegacyTranslated2"})
            pid = r.json()["id"]
            _set_legacy_part(app_with_projects, pid, report)
            _commit_stl(app_with_projects, pid, tmp_stl)
            r2 = await client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "make the center hole 38 mm"},
            )
            source = app_with_projects.state.event_sources.get(pid)
            frames = []
            assert source is not None
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
            svc = app_with_projects.state.versions
            return r2.status_code, frames, svc.get_pending_offer(pid)

        status, frames, offer = _run_async(app_with_projects, _call)
    finally:
        tmp_stl.unlink(missing_ok=True)

    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # The TRUE-centre hole (160, 140) is selected — the offer carries it.
    assert offer is not None, f"the true-centre hole must be offered: {frames}"
    assert list(offer.get("center", [])[:2]) == [160.0, 140.0], (
        f"expected the true-centre hole (160, 140), got: {offer}"
    )
    assert offer.get("diameter_mm") == 4.0, offer

def test_legacy_report_empty_mesh_degrades_to_extents_fallback(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #414 (adversarial review): a legacy report whose stored mesh
    loads with NO geometry (``mesh.bounds is None`` — the documented
    empty-mesh case, e.g. an empty Scene from a degenerate STL) must
    degrade to the extents/2 fallback — NOT crash the chat turn with an
    uncaught ``TypeError`` from the ``lo, hi = mesh.bounds`` unpack.

    The origin-anchored #396 shape (bbox 0..40 × 0..40, centre (20, 20))
    keeps its extents/2 pick: the (10, 0) hole at 22.4 mm from the centre,
    within the 30 mm match threshold."""
    import struct

    from d33d import part_mesh

    def _empty_mesh(raw: bytes, fmt: str):
        """A loadable mesh with ``bounds is None`` — the documented
        empty-mesh case the ``_load_mesh_bounds`` docstring names."""

        class _NoBounds:
            bounds = None

        return _NoBounds()

    monkeypatch.setattr(part_mesh, "load_part_geometry", _empty_mesh)
    # A 0-triangle binary STL (a real, non-empty file on disk — the
    # fallback's mesh load is the only thing the test intercepts).
    empty_stl = b"H" * 20 + struct.pack("<I", 0)
    tmp = Path("/tmp") / f"legacy_empty_stl_{next(itertools.count())}.stl"
    tmp.write_bytes(empty_stl)
    try:
        return _legacy_extents_fallback_case(app_with_projects, tmp)
    finally:
        tmp.unlink(missing_ok=True)


def _legacy_extents_fallback_case(app: Any, part_stl: Path):
    """Shared body for the extents/2-fallback legacy cases: legacy report
    (origin-anchored #396 shape) + committed part.stl → the extents/2
    pick (the (10, 0) Ø30 hole, 22.4 mm from centre)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "LegacyEmptyMesh"})
        pid = r.json()["id"]
        legacy_report = {
            "hole_count": 3,
            "holes": [
                {"center": [0.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
                {"center": [-10.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 20.0},
                {"center": [10.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 30.0},
            ],
            "bbox_file_units": [40.0, 40.0, 10.0],
        }
        _set_legacy_part(app, pid, legacy_report)
        _commit_stl(app, pid, part_stl)
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the center hole 38 mm"}
        )
        source = app.state.event_sources.get(pid)
        frames = []
        assert source is not None
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        svc = app.state.versions
        return r2.status_code, frames, svc.get_pending_offer(pid)

    status, frames, offer = _run_async(app, _call)
    assert status == 202, status
    # extents/2 (20, 20): the (10, 0) Ø30 hole is nearest (22.4 mm) —
    # the extents/2 fallback pick must be made, not a crash or a
    # no-match.
    assert offer is not None, f"extents/2 fallback must still pick: {frames}"
    assert list(offer.get("center", [])[:2]) == [10.0, 0.0], (
        f"expected the extents/2 pick (10, 0), got: {offer}"
    )
    assert offer.get("diameter_mm") == 30.0, offer


def test_legacy_report_missing_mesh_keeps_extents_fallback(app_with_projects) -> None:
    """Issue #414: a legacy report whose stored mesh is UNAVAILABLE (no
    part.stl on disk — e.g. a 3MF import's committed part.3mf is not
    loadable by this fallback) keeps the old extents/2 behaviour: the
    origin-anchored #396 shape (bbox 0..40 x 0..40, centre (20, 20))
    selects the nearest hole (22.4 mm from centre, within threshold)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "LegacyNoMesh"})
        pid = r.json()["id"]
        # Origin-anchored shape (the #396 test's part), legacy report.
        legacy_report = {
            "hole_count": 3,
            "holes": [
                {"center": [0.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
                {"center": [-10.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 20.0},
                {"center": [10.0, 0.0, 5.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 30.0},
            ],
            "bbox_file_units": [40.0, 40.0, 10.0],
        }
        _set_legacy_part(app_with_projects, pid, legacy_report)
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the center hole 38 mm"}
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        assert source is not None
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        svc = app_with_projects.state.versions
        return r2.status_code, frames, svc.get_pending_offer(pid)

    status, frames, offer = _run_async(app_with_projects, _call)
    assert status == 202, status
    # extents/2 (20, 20): distances (0,0)→28.3, (-10,0)→36.1, (10,0)→22.4 —
    # the (10, 0) Ø30 hole wins (within the 30 mm threshold).
    assert offer is not None, f"extents/2 fallback must still pick: {frames}"
    assert list(offer.get("center", [])[:2]) == [10.0, 0.0], (
        f"expected the extents/2 pick (10, 0), got: {offer}"
    )
    assert offer.get("diameter_mm") == 30.0, offer
