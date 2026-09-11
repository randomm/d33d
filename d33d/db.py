"""Shared SQLite schema for the FastAPI backend (issue #3, task-d scope).

This module owns the four tables the backend needs that are NOT the model
catalogue (the catalogue lives in YAML — see task-a):

* ``projects``             — project metadata. The per-project git repo
                              lives on the file system; this row only
                              carries the path and the #7 data-model
                              fields (name, tags, notes, source-photo ref).
* ``transcripts``          — one row per message, in canonical schema.
                              Per-message model logging (alias, resolved
                              id, provider) lives here so #7 can restore /
                              audit which model produced each message.
                              The stored transcript is the canonical
                              schema and is NEVER rewritten on model
                              switch — sanitisation happens on the outgoing
                              request only (task-b owns the sanitiser).
* ``provider_credentials`` — Fernet ciphertext per (provider, model alias).
                              The *list* view returned by the HTTP layer
                              exposes provider/model names only; the raw
                              ciphertext column is never selected by the
                              names-only view. Fernet encryption itself is
                              task-b; this table is just the storage home.
* ``request_logs``         — observability row per LLM call. ``prompt_hash``
                              is the A10 diff key (task-d's prompt_hash.py):
                              two rows called on the same prompt through
                              different models share the hash and can be
                              diffed purely from the logs.

Design choices:
  - SQLite in WAL mode so a single FastAPI writer does not lock out a
    read-only consumer (e.g. the structured-JSON-logs pipeline).
  - ``PRAGMA foreign_keys=ON`` is set per connection (SQLite's default is
    OFF) and pinned by test.
  - ``connect()`` is idempotent: opening an existing file re-creates the
    schema via ``CREATE TABLE IF NOT EXISTS`` and is safe to call on every
    process start.
  - ``Connection`` is a thin wrapper over ``sqlite3.Connection`` that
    exposes the four table groups as methods so the FastAPI layer (task-e)
    never has to spell the DDL.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import uuid
from pathlib import Path
from typing import Any

__all__ = [
    "SCHEMA_VERSION",
    "Connection",
    "connect",
]

SCHEMA_VERSION = 1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_info (
    version INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    name            TEXT    NOT NULL,
    git_repo_path   TEXT    NOT NULL,
    tags            TEXT    NOT NULL DEFAULT '[]',
    notes           TEXT    NOT NULL DEFAULT '',
    source_photo_path TEXT,
    created_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS transcripts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    role        TEXT    NOT NULL,
    content     TEXT    NOT NULL,
    model_alias TEXT,
    model_id    TEXT,
    provider    TEXT,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_transcripts_project_id
    ON transcripts(project_id, id);

CREATE TABLE IF NOT EXISTS provider_credentials (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_id   TEXT NOT NULL,
    model_alias   TEXT NOT NULL,
    key_ciphertext BLOB NOT NULL,
    updated_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(provider_id, model_alias)
);

CREATE TABLE IF NOT EXISTS request_logs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id        INTEGER REFERENCES projects(id) ON DELETE CASCADE,
    model_alias       TEXT NOT NULL,
    model_id          TEXT NOT NULL,
    provider          TEXT NOT NULL,
    role              TEXT NOT NULL,
    status            TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL,
    completion_tokens INTEGER NOT NULL,
    latency_ms        INTEGER NOT NULL,
    prompt_hash       TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_request_logs_prompt_hash
    ON request_logs(prompt_hash);
CREATE INDEX IF NOT EXISTS idx_request_logs_project_id
    ON request_logs(project_id);
"""


class Connection:
    """Thin typed wrapper over ``sqlite3.Connection`` for the four tables.

    The wrapper exists so the FastAPI layer (task-e) can call
    ``conn.create_project(...)`` instead of spelling DML, and so the
    ``sqlite3.Row`` row-factory behaviour (``row["col"]`` dict access)
    is pinned in one place rather than sprinkled through route handlers.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        # WAL only makes sense for file-backed DBs; :memory: ignores it.
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(_SCHEMA)
        self._conn.execute(
            "INSERT INTO schema_info (version) VALUES (?)", (SCHEMA_VERSION,)
        )
        self._conn.commit()

    # -- raw passthrough ----------------------------------------------------

    @property
    def raw(self) -> sqlite3.Connection:
        return self._conn

    def execute(self, *args: Any, **kwargs: Any) -> sqlite3.Cursor:
        return self._conn.execute(*args, **kwargs)

    def commit(self) -> None:
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()

    # -- projects -----------------------------------------------------------

    def create_project(
        self,
        *,
        name: str,
        tags: list[str] | None = None,
        notes: str = "",
        source_photo_path: str | None = None,
        git_repo_path: str | None = None,
    ) -> int:
        """Insert a project row; returns the new ``id``.

        ``git_repo_path`` is the on-disk path of the per-project git repo.
        If not supplied, a temp-dir path is generated so the row is
        immediately consistent (the caller is expected to ``git init``
        that path before any commit).
        """
        tags = tags or []
        git_path = git_repo_path or _default_git_path(name)
        cur = self._conn.execute(
            "INSERT INTO projects (name, git_repo_path, tags, notes, source_photo_path)"
            " VALUES (?, ?, ?, ?, ?)",
            (name, git_path, json.dumps(tags), notes, source_photo_path),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["tags"] = json.loads(out.get("tags") or "[]")
        return out

    def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
        source_photo_path: str | None = None,
    ) -> None:
        if name is not None:
            self._conn.execute(
                "UPDATE projects SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (name, project_id),
            )
        if tags is not None:
            self._conn.execute(
                "UPDATE projects SET tags = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (json.dumps(tags), project_id),
            )
        if notes is not None:
            self._conn.execute(
                "UPDATE projects SET notes = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (notes, project_id),
            )
        if source_photo_path is not None:
            self._conn.execute(
                "UPDATE projects SET source_photo_path = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (source_photo_path, project_id),
            )
        self._conn.commit()

    def delete_project(self, project_id: int) -> None:
        self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["tags"] = json.loads(d.get("tags") or "[]")
            out.append(d)
        return out

    # -- transcripts --------------------------------------------------------

    def append_message(
        self,
        *,
        project_id: int,
        role: str,
        content: str,
        model_alias: str | None = None,
        model_id: str | None = None,
        provider: str | None = None,
    ) -> int:
        """Append one message to the canonical transcript.

        ``content`` is stored verbatim — the byte-identical invariant
        (acceptance #4) lives here. Sanitisation happens on the outgoing
        request, never on the stored row (task-b owns the sanitiser).
        """
        cur = self._conn.execute(
            "INSERT INTO transcripts (project_id, role, content, model_alias, model_id, provider)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (project_id, role, content, model_alias, model_id, provider),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_transcript(self, project_id: int) -> list[dict[str, Any]]:
        rows = self._conn.execute(
            "SELECT * FROM transcripts WHERE project_id = ? ORDER BY id ASC",
            (project_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    # -- provider_credentials ----------------------------------------------

    def put_credential(
        self,
        *,
        provider_id: str,
        model_alias: str,
        key_ciphertext: bytes,
    ) -> int:
        """Insert or replace the Fernet ciphertext for (provider, alias).

        ``key_ciphertext`` is the Fernet-encrypted key (task-b encrypts
        under MASTER_KEY). This table is the *storage home*; the encryption
        itself is task-b's job.
        """
        self._conn.execute(
            "INSERT INTO provider_credentials (provider_id, model_alias, key_ciphertext)"
            " VALUES (?, ?, ?) ON CONFLICT(provider_id, model_alias)"
            " DO UPDATE SET key_ciphertext = excluded.key_ciphertext,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (provider_id, model_alias, key_ciphertext),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM provider_credentials WHERE provider_id = ? AND model_alias = ?",
            (provider_id, model_alias),
        ).fetchone()
        return int(row["id"])

    def get_credential(self, credential_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM provider_credentials WHERE id = ?", (credential_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def list_credentials(self) -> list[dict[str, Any]]:
        """Names-only view (what the HTTP list endpoint returns).

        Per the hard invariant, the ciphertext column is NEVER selected
        here. The HTTP layer (task-b) can safely serialise this list into
        a JSON response without any key material leaking.
        """
        rows = self._conn.execute(
            "SELECT id, provider_id, model_alias FROM provider_credentials ORDER BY id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- request_logs -------------------------------------------------------

    def log_request(
        self,
        *,
        project_id: int | None,
        model_alias: str,
        model_id: str,
        provider: str,
        role: str,
        status: str,
        prompt_tokens: int,
        completion_tokens: int,
        latency_ms: int,
        prompt_hash: str,
    ) -> int:
        """Insert one observability row. ``prompt_hash`` is the A10 diff
        key (see ``d33d.prompt_hash.canonical_hash``) — the stable join key
        that lets two models be compared on the same prompt from the logs."""
        cur = self._conn.execute(
            "INSERT INTO request_logs"
            " (project_id, model_alias, model_id, provider, role, status,"
            "  prompt_tokens, completion_tokens, latency_ms, prompt_hash)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                model_alias,
                model_id,
                provider,
                role,
                status,
                prompt_tokens,
                completion_tokens,
                latency_ms,
                prompt_hash,
            ),
        )
        self._conn.commit()
        return int(cur.lastrowid or 0)

    def get_request_log(self, log_id: int) -> dict[str, Any] | None:
        row = self._conn.execute(
            "SELECT * FROM request_logs WHERE id = ?", (log_id,)
        ).fetchone()
        return dict(row) if row is not None else None

    def requests_by_prompt_hash(self, prompt_hash: str) -> list[dict[str, Any]]:
        """All log rows sharing a prompt hash — the A10 diff surface."""
        rows = self._conn.execute(
            "SELECT * FROM request_logs WHERE prompt_hash = ? ORDER BY id ASC",
            (prompt_hash,),
        ).fetchall()
        return [dict(r) for r in rows]


def connect(path: str | Path) -> Connection:
    """Open (or create) the SQLite DB at ``path`` and apply the schema.

    Idempotent: ``CREATE TABLE IF NOT EXISTS`` makes repeated calls safe.
    ``path`` may be ``":memory:"`` for tests, or a ``Path``/str file path.
    """
    return Connection(path)


def _default_git_path(name: str) -> str:
    """Generate a unique on-disk path for the per-project git repo.

    The caller is expected to ``git init`` this path. We use a temp-dir
    base (``/tmp/d33d-projects/<uuid>/``) so the path is unique per project
    and does not collide across tests / worktrees.
    """
    slug = uuid.uuid4().hex[:12]
    base = Path(tempfile.gettempdir()) / "d33d-projects" / f"{slug}"
    base.mkdir(parents=True, exist_ok=True)
    return str(base)
