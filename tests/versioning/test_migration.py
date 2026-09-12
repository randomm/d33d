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
