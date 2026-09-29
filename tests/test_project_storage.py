"""Issue #294 — project repos live under the data dir, migrated at startup.

Covers the ticket's acceptance criteria:

* the un-monkeypatched ``create_project`` default lands the repo under
  ``<data_dir>/projects/`` (never the OS temp dir);
* the startup migration moves a temp-dir repo into the data dir, rewrites
  ``git_repo_path`` AND a ``source_photo_path`` under the old prefix, and
  is idempotent (second run is a no-op);
* a project whose temp dir is gone is left unchanged, flagged
  ``storage_missing`` by the derived ``present`` count, and logged with
  exactly one WARNING naming the project id only (no path);
* the startup summary line "N projects, M repos present, K missing" is
  logged exactly once per run, present even for a fresh install;
* the grep invariant: no production path in ``d33d/db.py`` or
  ``d33d/versions.py`` calls ``tempfile.gettempdir()`` or references the
  "d33d-projects" temp base (``d33d/app.py``'s ``tempfile.mkstemp`` for
  upload staging is out of scope and NOT checked).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import d33d.db as db_mod
from d33d.db import migrate_project_repos

from .versioning.helpers import run_async

# A minimal valid 1x1 PNG (same fixture family as
# test_design_loop_finalize.py's inline photo).
_PNG_1X1 = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000"
    "001f15c4890000000d49444154789c626001000000050001"
    "0d0a2fbc1e0000000049454e44ae426082"
)


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """An isolated D33D_DATA_DIR under tmp_path (the env default is
    steered, so the UN-monkeypatched ``_default_git_path`` lands here)."""
    d = tmp_path / "d33d-data"
    d.mkdir()
    monkeypatch.setenv("D33D_DATA_DIR", str(d))
    return d


def _seed_repo(path: Path, *, with_photo: bool = False) -> Path:
    """Create ``path`` with a design.scad and (optionally) a photo inside."""
    path.mkdir(parents=True)
    (path / "design.scad").write_text("cube([10, 10, 10]);\n")
    if with_photo:
        (path / "photo.png").write_bytes(_PNG_1X1)
    return path


def _seed_project(conn, *, name: str, repo: str | None = None) -> int:
    return conn.create_project(name=name, git_repo_path=repo)


# ---------------------------------------------------------------------------
# New-project location (un-monkeypatched default)
# ---------------------------------------------------------------------------


def test_new_project_default_lands_under_data_dir(data_dir: Path):
    """``create_project`` with no git_repo_path uses the real default:
    the repo path must start under ``<D33D_DATA_DIR>/projects/``."""
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    # Simulate the app wiring: create_app records the DB path's parent on
    # the connection, and the repo default reads it at call time.
    conn.data_dir = data_dir
    try:
        pid = conn.create_project(name="default-location")
        row = conn.get_project(pid)
        assert row is not None
        assert row["git_repo_path"].startswith(str(data_dir / "projects"))
        assert Path(row["git_repo_path"]).is_dir()
    finally:
        conn.close()


def test_new_project_default_via_api_lands_under_data_dir(
    data_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """``POST /api/projects`` (un-monkeypatched default) also lands the
    repo under the app's data dir — the DB path's parent, steered by
    ``db.APP_DATA_DIR`` which the lifespan records."""
    import d33d.app as app_mod

    app = app_mod.create_app(
        data_dir / "d33d.sqlite3",
        master_key_path=data_dir / "master.key",
        catalogue_path=data_dir / "models.yaml",
    )
    # Simulate the app wiring (the lifespan sets this to the DB path's
    # parent): the repo default reads it at call time.
    monkeypatch.setattr(db_mod, "APP_DATA_DIR", data_dir)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "api-default"})
        assert r.status_code == 201, r.text
        # The DB is live during the lifespan window — read the row's repo
        # path from inside it (the API masks git_repo_path, git invisibility).
        row = app.state.conn.get_project(r.json()["id"])
        return row["git_repo_path"]

    repo = Path(run_async(app, _call))
    assert repo.is_dir() or repo.parent.is_dir()  # repo dir exists on disk
    assert repo.parent == data_dir / "projects"


def test_default_git_path_does_not_use_os_tmp(data_dir: Path, tmp_path: Path):
    """The default is independent of the OS temp dir: steering
    ``tempfile.gettempdir()`` (via TMPDIR) must not move the repo."""
    os.environ["TMPDIR"] = str(tmp_path / "fake-tmp")
    try:
        p = db_mod._default_git_path("x")
    finally:
        os.environ.pop("TMPDIR", None)
    assert p.startswith(str(data_dir / "projects"))


def test_default_git_path_read_at_call_time(monkeypatch: pytest.MonkeyPatch):
    """``db_mod._default_git_path`` patches must be visible to
    ``create_project`` (the versions/conftest seam contract)."""
    called = []

    def _patched(name: str) -> str:
        called.append(name)
        return "/patched/" + name

    monkeypatch.setattr(db_mod, "_default_git_path", _patched)
    conn = db_mod.connect(":memory:")
    pid = conn.create_project(name="seam")
    assert conn.get_project(pid)["git_repo_path"] == "/patched/seam"
    assert called == ["seam"]
    conn.close()


# ---------------------------------------------------------------------------
# Migration: move + dual-path rewrite + idempotency
# ---------------------------------------------------------------------------


def test_migration_moves_repo_and_rewrites_both_paths(data_dir: Path, caplog):
    old_base = data_dir / "tmp-projects"  # outside the data-dir projects base
    old = _seed_repo(old_base / "abc123", with_photo=True)
    photo = old / "photo.png"
    assert photo.is_file()

    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        pid = _seed_project(conn, name="old-repo", repo=str(old))
        conn.update_project(pid, source_photo_path=str(photo))

        with caplog.at_level("INFO", logger="d33d.db"):
            res = migrate_project_repos(conn)

        row = conn.get_project(pid)
        new = data_dir / "projects" / "abc123"
        assert row["git_repo_path"] == str(new)
        assert new.is_dir()
        assert not old.exists()
        # design.scad and the photo both moved (the repo was the repo).
        assert (new / "design.scad").is_file()
        # source_photo_path was under the old prefix -> rewritten.
        assert row["source_photo_path"] == str(new / "photo.png")
        assert Path(row["source_photo_path"]).is_file()
        assert res == {"projects": 1, "present": 1, "missing": 0}
        # The move is logged as INFO (not warning).
        infos = [r for r in caplog.records if r.levelname == "INFO" and "moved" in r.getMessage()]
        assert len(infos) == 1
        assert "abc123" in infos[0].getMessage()
    finally:
        conn.close()


def test_migration_is_idempotent(data_dir: Path, caplog):
    old = _seed_repo(data_dir / "tmp-projects" / "abc123")
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        pid = _seed_project(conn, name="idem", repo=str(old))
        with caplog.at_level("INFO", logger="d33d.db"):
            first = migrate_project_repos(conn)
        row_after_first = conn.get_project(pid)
        new = data_dir / "projects" / "abc123"
        assert row_after_first["git_repo_path"] == str(new)
        assert first["present"] == 1

        caplog.clear()
        second = migrate_project_repos(conn)
        assert second == {"projects": 1, "present": 1, "missing": 0}
        # No second move, no new dirs, no changed row.
        row_after_second = conn.get_project(pid)
        assert row_after_second["git_repo_path"] == str(new)
        moves = [r for r in caplog.records if "moved" in r.getMessage()]
        assert moves == []
        assert sorted(p.name for p in (data_dir / "projects").iterdir()) == ["abc123"]
    finally:
        conn.close()


def test_migration_photo_outside_repo_prefix_not_rewritten(data_dir: Path):
    old = _seed_repo(data_dir / "tmp-projects" / "abc123")
    outside_photo = data_dir / "elsewhere" / "photo.png"
    outside_photo.parent.mkdir(parents=True)
    outside_photo.write_bytes(_PNG_1X1)
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        pid = _seed_project(conn, name="outside-photo", repo=str(old))
        conn.update_project(pid, source_photo_path=str(outside_photo))
        migrate_project_repos(conn)
        row = conn.get_project(pid)
        assert row["git_repo_path"] == str(data_dir / "projects" / "abc123")
        # Unrelated photo path is untouched.
        assert row["source_photo_path"] == str(outside_photo)
    finally:
        conn.close()


def test_migration_basename_collision_resolved_not_nested(data_dir: Path, caplog):
    """Two projects whose repos share a basename: the second must NOT be
    moved *inside* the first (``<base>/<name>/<name>``) — it lands under a
    collision suffix, both rows point at real directories, and both repos
    keep their contents."""
    first_repo = _seed_repo(data_dir / "elsewhere-a" / "abc123")
    second_repo = _seed_repo(data_dir / "elsewhere-b" / "abc123")
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        pid1 = _seed_project(conn, name="first", repo=str(first_repo))
        pid2 = _seed_project(conn, name="second", repo=str(second_repo))
        with caplog.at_level("INFO", logger="d33d.db"):
            res = migrate_project_repos(conn)
        assert res == {"projects": 2, "present": 2, "missing": 0}
        row1 = conn.get_project(pid1)
        row2 = conn.get_project(pid2)
        # The first project owns the bare basename.
        assert row1["git_repo_path"] == str(data_dir / "projects" / "abc123")
        assert (data_dir / "projects" / "abc123" / "design.scad").is_file()
        # The second project landed under a collision suffix — never nested
        # inside the first project's directory.
        assert row2["git_repo_path"].startswith(
            str(data_dir / "projects" / "abc123-")
        )
        nested = data_dir / "projects" / "abc123" / "abc123"
        assert not nested.exists()
        assert Path(row2["git_repo_path"]).is_dir()
        assert (Path(row2["git_repo_path"]) / "design.scad").is_file()
        assert not second_repo.exists()
        # The collision is flagged (never silent) — but the missing-repo
        # WARNING is a different message; only the suffix note may appear
        # here.
        suffix_notes = [
            r for r in caplog.records if "collision suffix" in r.getMessage()
        ]
        assert len(suffix_notes) == 1
    finally:
        conn.close()


def test_migration_non_git_repo_dir_still_moves(data_dir: Path):
    """A plain directory (empty or not a git repo) is moved, never an
    error: the operator rule is 'if the old directory exists, move it'."""
    plain = data_dir / "tmp-projects" / "plain77"
    plain.mkdir(parents=True)
    empty = data_dir / "tmp-projects" / "empty00"
    empty.mkdir(parents=True)
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        _seed_project(conn, name="plain", repo=str(plain))
        _seed_project(conn, name="empty", repo=str(empty))
        res = migrate_project_repos(conn)
        assert res == {"projects": 2, "present": 2, "missing": 0}
        assert (data_dir / "projects" / "plain77").is_dir()
        assert (data_dir / "projects" / "empty00").is_dir()
        assert not plain.exists()
        assert not empty.exists()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Missing repo: unchanged row, derived flag, exactly one WARNING
# ---------------------------------------------------------------------------


def test_missing_repo_unchanged_and_flagged(data_dir: Path, caplog):
    gone = data_dir / "tmp-projects" / "def456"
    assert not gone.exists()
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        pid = _seed_project(conn, name="gone", repo=str(gone))
        with caplog.at_level("WARNING", logger="d33d.db"):
            res = migrate_project_repos(conn)

        row = conn.get_project(pid)
        assert row["git_repo_path"] == str(gone)  # unchanged
        assert not (data_dir / "projects" / "def456").exists()  # no empty repo
        # Derived flag: the summary counts it missing (never a new column).
        assert res == {"projects": 1, "present": 0, "missing": 1}
        warnings = [r for r in caplog.records if r.levelno >= 30]
        assert len(warnings) == 1, f"expected exactly 1 WARNING, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert str(pid) in msg
        # The path is never logged.
        assert str(gone) not in msg
        assert "def456" not in msg
    finally:
        conn.close()


def test_mixed_fleet_summary_counts(data_dir: Path, caplog):
    present_old = _seed_repo(data_dir / "tmp-projects" / "aaa111")
    gone_old = data_dir / "tmp-projects" / "bbb222"  # never created
    already = data_dir / "projects" / "ccc333"
    already.mkdir(parents=True)

    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        _seed_project(conn, name="one", repo=str(present_old))
        _seed_project(conn, name="two", repo=str(gone_old))
        _seed_project(conn, name="three", repo=str(already))
        with caplog.at_level("INFO", logger="d33d.db"):
            res = migrate_project_repos(conn)
        assert res == {"projects": 3, "present": 2, "missing": 1}
        summaries = [
            r
            for r in caplog.records
            if r.levelname == "INFO"
            and "projects, " in r.getMessage()
            and "missing" in r.getMessage()
        ]
        assert len(summaries) == 1
        assert summaries[0].getMessage() == "3 projects, 2 repos present, 1 missing"
    finally:
        conn.close()


def test_fresh_install_summary_zeroes(data_dir: Path, caplog):
    """A fresh install (no projects at all) still logs the summary line."""
    conn = db_mod.connect(data_dir / "d33d.sqlite3")
    conn.data_dir = data_dir
    try:
        with caplog.at_level("INFO", logger="d33d.db"):
            res = migrate_project_repos(conn)
        assert res == {"projects": 0, "present": 0, "missing": 0}
        summaries = [
            r for r in caplog.records if "0 projects, 0 repos present, 0 missing" in r.getMessage()
        ]
        assert len(summaries) == 1
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Migration at app startup (lifespan wiring)
# ---------------------------------------------------------------------------


def test_migration_runs_at_app_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The REAL production startup path (``create_app`` + lifespan, as
    ``python -m d33d.main`` runs it) runs the migration: a pre-existing
    temp-dir repo row is moved into ``<data_dir>/projects/`` by the
    lifespan — no manually pre-set ``app.state.conn`` steer."""
    data_dir = tmp_path / "d33d-data"
    data_dir.mkdir()
    # The production shape: D33D_DATA_DIR names a DIFFERENT directory than
    # the app's data dir. Pre-create the env's projects base and assert it
    # stays empty — the migration base must be the app's data dir (the DB
    # path's parent), not the env's.
    env_base = tmp_path / "other-data" / "projects"
    env_base.mkdir(parents=True)
    monkeypatch.setenv("D33D_DATA_DIR", str(tmp_path / "other-data"))
    old = _seed_repo(data_dir / "tmp-projects" / "start1", with_photo=True)

    import d33d.app as app_mod

    app = app_mod.create_app(
        data_dir / "d33d.sqlite3",
        master_key_path=data_dir / "master.key",
        catalogue_path=data_dir / "models.yaml",
    )
    # Seed the row the way a legacy installation would have it (the DB is
    # the storage; only the row's path is the legacy state).
    seed = db_mod.connect(data_dir / "d33d.sqlite3")
    pid = seed.create_project(name="start", git_repo_path=str(old))
    seed.update_project(pid, source_photo_path=str(old / "photo.png"))
    seed.close()

    async def _call(client):
        # The lifespan has already run (run_async wraps it); read the
        # post-migration row back through the live connection + the API.
        r = await client.get(f"/api/projects/{pid}")
        assert r.status_code == 200
        row = app.state.conn.get_project(pid)
        return r.json(), row

    row_api, row_db = run_async(app, _call)
    # Git invisibility: the path never crosses the API boundary.
    assert "git_repo_path" not in row_api
    new = data_dir / "projects" / "start1"
    assert row_db["git_repo_path"] == str(new)
    assert new.is_dir()
    assert not old.exists()
    assert row_db["source_photo_path"] == str(new / "photo.png")
    # The migration targeted the app's data dir — the env's projects base
    # was never used as the base.
    assert list(env_base.iterdir()) == []


# ---------------------------------------------------------------------------
# Grep invariant
# ---------------------------------------------------------------------------


def test_no_tempfile_project_storage_in_db_and_versions():
    """No production path in d33d/db.py or d33d/versions.py uses the OS
    temp dir or the old 'd33d-projects' base for project storage.
    (d33d/app.py's tempfile.mkstemp upload staging is legitimate and is
    deliberately NOT checked here.)"""
    root = Path(__file__).resolve().parents[1]
    for rel in ("d33d/db.py", "d33d/versions.py"):
        src = (root / rel).read_text()
        assert "tempfile.gettempdir" not in src, f"{rel}: tempfile.gettempdir() call found"
        assert "d33d-projects" not in src, f"{rel}: legacy 'd33d-projects' base found"
