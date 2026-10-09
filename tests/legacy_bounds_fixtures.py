"""Shared fixtures and helpers for the two legacy-bounds test modules
(``test_fill_recut_legacy_bounds.py`` and
``test_fill_recut_legacy_bounds_offloop.py``).

The two modules share the ``app_paths`` / ``app_with_projects`` fixtures
and the ``_set_legacy_part`` / ``_commit_stl`` / ``_legacy_report``
helpers; this module is the ONE home for them (issue #414, round 2,
LOW: move the duplicated fixtures and helpers into a shared place).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
        "tmp": tmp_path,
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path]):
    """Isolated app with tmp-path repos (the test_projects pattern)."""
    import d33d.db as db_mod
    from d33d.app import create_app

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
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


def _make_stl() -> bytes:
    """A small, valid, non-empty STL (the off_centre_plate fixture — 0..120
    x 0..80 x 0..6 in file units)."""
    return (FIXTURES / "off_centre_plate.stl").read_bytes()


def _set_legacy_part(app: Any, pid: int, report: dict) -> None:
    """Set the part columns with a LEGACY report (no bounds key) directly
    on the DB."""
    conn = app.state.conn
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status='settled', part_scale=1.0, part_report=? "
        "WHERE id=?",
        (json.dumps(report), pid),
    )
    conn.commit()


def _commit_stl(app: Any, pid: int, stl_bytes: bytes) -> None:
    """Commit part.stl to the project's v1 dir (the layout
    ``_v1_part_path`` reads: ``{repo}/versions/{v1}/part.stl``). Inserts
    a v1 version row first when the project has none (legacy-row tests
    create the project but no import — the real import path writes both).

    ``stl_bytes`` is the raw STL bytes (call ``_make_stl()`` or read a
    file and pass ``.read_bytes()``)."""
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
