"""Migration tests (issue #8): the pre-existing-database path.

``migrate()`` runs on every app start, including databases created BEFORE
issue #8 (whose ``projects`` table lacks ``current_version`` and
``last_activity``). The regression: the original implementation added
``last_activity`` via ``ALTER TABLE ... ADD COLUMN ... DEFAULT (json(...))``
— SQLite rejects a function-call (non-constant) default in ALTER TABLE ADD
COLUMN, so every pre-existing DB failed to start with
``OperationalError: Cannot add a column with non-constant default``.
"""

from __future__ import annotations

from pathlib import Path

from d33d import db as db_mod
from d33d.versions import migrate


def _pre_issue8_db(path: Path) -> db_mod.Connection:
    """A database as issue #8 found one: the projects table WITHOUT the
    current_version / last_activity columns (the pre-#8 DDL). The raw
    sqlite3 connection is used because ``db_mod.Connection.__init__``
    applies the current (post-#8) schema, which already has the columns."""
    import sqlite3

    raw = sqlite3.connect(str(path))
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.execute(
        """
        CREATE TABLE projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            git_repo_path   TEXT    NOT NULL,
            tags            TEXT    NOT NULL DEFAULT '[]',
            notes           TEXT    NOT NULL DEFAULT '',
            source_photo_path TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    raw.commit()
    conn = db_mod.Connection.__new__(db_mod.Connection)
    conn._conn = raw
    return conn


def _column_names(conn: db_mod.Connection, table: str) -> set[str]:
    rows = conn.raw.execute(f"PRAGMA table_info({table})").fetchall()
    return {r[1] for r in rows}


def test_migrate_adds_missing_project_columns_to_pre_existing_db(tmp_path: Path):
    """The regression: a pre-#8 database migrates (no OperationalError) and
    gains both new columns, with pre-existing rows backfilled to the null
    last-activity JSON (the function-call default is legal only in the
    fresh-DDL, never in an ALTER TABLE)."""
    conn = _pre_issue8_db(tmp_path / "legacy.sqlite3")
    # A pre-existing project row (the row that must be backfilled).
    conn.raw.execute(
        "INSERT INTO projects (name, git_repo_path) VALUES ('legacy box', '/tmp/x')"
    )
    conn.commit()

    migrate(conn)  # must not raise

    names = _column_names(conn, "projects")
    assert "current_version" in names
    assert "last_activity" in names

    # The pre-existing row is backfilled with the null JSON literal —
    # the same shape fresh DDL rows get.
    row = conn.raw.execute("SELECT last_activity FROM projects").fetchone()[0]
    assert row == '{"ts": null, "version_id": null, "name": null}'


def test_migrate_is_idempotent_on_fresh_db(tmp_path: Path):
    """A fresh DB (projects already has both columns from the DDL) survives
    migrate() twice — the pragma guard makes the ALTERs no-ops."""
    conn = db_mod.Connection(tmp_path / "fresh.sqlite3")
    migrate(conn)
    migrate(conn)  # second call: no error, no change

    assert "current_version" in _column_names(conn, "projects")
    assert "last_activity" in _column_names(conn, "projects")
    names = _column_names(conn, "versions")
    assert "params" in names and "restored_from" in names
    # Issue #325 — the part-import columns and source_kind are present.
    part_cols = _column_names(conn, "projects")
    for col in (
        "part_filename",
        "part_format",
        "part_unit",
        "part_unit_status",
        "part_scale",
        "part_report",
        "part_options",
    ):
        assert col in part_cols, f"projects.{col} missing after migrate"
    assert "source_kind" in _column_names(conn, "versions")


def test_migrate_adds_part_columns_to_pre_issue325_db(tmp_path: Path):
    """A pre-#325 database (projects has all pre-#325 columns, versions has
    all pre-#325 columns, but NONE of the #325 part columns or source_kind)
    migrates without error and gains the new columns. Pre-existing rows are
    NOT backfilled — a project without a part has all part columns NULL,
    and a version without an import source has source_kind NULL.
    """
    import sqlite3

    raw = sqlite3.connect(str(tmp_path / "pre325.sqlite3"))
    raw.row_factory = sqlite3.Row
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    # Projects table as it looked just before #325 (all pre-#325 columns).
    raw.execute(
        """
        CREATE TABLE projects (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT    NOT NULL,
            git_repo_path   TEXT    NOT NULL,
            tags            TEXT    NOT NULL DEFAULT '[]',
            notes           TEXT    NOT NULL DEFAULT '',
            source_photo_path TEXT,
            current_version INTEGER,
            last_activity   TEXT    NOT NULL DEFAULT (json('{"ts": null, "version_id": null, "name": null}')),
            pending_offer   TEXT,
            carried_stated_dims TEXT,
            created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    # Versions table as it looked just before #325 (no source_kind).
    raw.execute(
        """
        CREATE TABLE versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            params      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            created_by_message TEXT NOT NULL DEFAULT '',
            parent      INTEGER,
            restored_from INTEGER,
            forked_from TEXT,
            pinned      INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            thumbnail   TEXT,
            bbox        TEXT,
            exported_at TEXT,
            render_artifact_dir TEXT,
            stated_dims TEXT,
            param_meta  TEXT,
            confirmed_params TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    raw.execute(
        "CREATE INDEX idx_versions_project_id ON versions(project_id, id)"
    )
    # Pre-existing rows: a project and a version that must survive migration.
    raw.execute(
        "INSERT INTO projects (name, git_repo_path) VALUES ('legacy box', '/tmp/x')"
    )
    raw.execute(
        "INSERT INTO versions (project_id, params, name, created_by_message)"
        " VALUES (1, '{\"W\": 20.0}', 'First design', '')"
    )
    raw.commit()

    conn = db_mod.Connection.__new__(db_mod.Connection)
    conn._conn = raw

    migrate(conn)  # must not raise

    # All seven new project columns are present.
    proj_names = _column_names(conn, "projects")
    for col in (
        "part_filename",
        "part_format",
        "part_unit",
        "part_unit_status",
        "part_scale",
        "part_report",
        "part_options",
    ):
        assert col in proj_names, f"projects.{col} missing after migrate"

    # versions.source_kind is present.
    assert "source_kind" in _column_names(conn, "versions")

    # Pre-existing project row: all part columns are NULL (no backfill —
    # an absent part abstains, never a fabricated value).
    row = conn.raw.execute(
        "SELECT part_filename, part_format, part_unit, part_unit_status,"
        " part_scale, part_report, part_options FROM projects WHERE id = 1"
    ).fetchone()
    assert row["part_filename"] is None
    assert row["part_format"] is None
    assert row["part_unit"] is None
    assert row["part_unit_status"] is None
    assert row["part_scale"] is None
    assert row["part_report"] is None
    assert row["part_options"] is None

    # Pre-existing version row: source_kind is NULL (not an import).
    vrow = conn.raw.execute(
        "SELECT source_kind FROM versions WHERE id = 1"
    ).fetchone()
    assert vrow["source_kind"] is None
