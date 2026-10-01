"""Shared SQLite schema for the FastAPI backend (issue #3, task-d scope).

This module owns the five tables the backend needs that are NOT the model
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
* ``project_dimensions``   — the CLARIFY-before-code confirmed dimension
                              set, persisted alongside the transcript (one
                              row per project, keyed by project_id).
                              ``params`` is the named-parameter map
                              (JSON: W/D/H in mm plus tolerance_mm / fit_type)
                              that the #3 bbox gate compares against. "Active
                              version" promotion is issue #7's concern — this
                              table only persists the confirmed set, it does
                              not manage version history.

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
import logging
import shutil
import sqlite3
import uuid
from pathlib import Path
from typing import Any

from d33d.data_dir import default_data_dir, guard_real_data_path

logger = logging.getLogger(__name__)

__all__ = [
    "SCHEMA_VERSION",
    "Connection",
    "connect",
]

SCHEMA_VERSION = 1

#: The app-scoped data dir (issue #294). ``create_app``'s lifespan records
#: the DB path's parent here at startup so the repo default resolves under
#: the app's data dir instead of a leftover ``D33D_DATA_DIR`` env var. Left
#: ``None`` for bare ``connect()`` usage (tests), where the env is the
#: source. Reset by test setups that need the env-backed default.
APP_DATA_DIR: Path | None = None

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
    current_version INTEGER,
    last_activity   TEXT    NOT NULL DEFAULT (json('{"ts": null, "version_id": null, "name": null}')),
    part_filename TEXT,
    part_format TEXT,
    part_unit TEXT,
    part_unit_status TEXT,
    part_scale REAL,
    part_report TEXT,
    part_options TEXT,
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

CREATE TABLE IF NOT EXISTS project_dimensions (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    project_id    INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
    fit_type      TEXT    NOT NULL,
    tolerance_mm  REAL    NOT NULL,
    params        TEXT    NOT NULL,
    confirmed_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at    TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(project_id)
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
        # The app-scoped data dir (issue #294): set by the app lifespan to
        # the directory of the DB path, so the repo default and the startup
        # migration steer off it at call time instead of the ``D33D_DATA_DIR``
        # env. ``None`` for bare ``connect()`` usage (tests), where the env
        # is the source.
        self.data_dir: Path | None = None
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

    @property
    def closed(self) -> bool:
        """True once ``close()`` has run (a test-owned connection that a
        later ``run_async`` lifespan closed before re-entering the app)."""
        try:
            self._conn.execute("SELECT 1")
            return False
        except sqlite3.ProgrammingError:
            return True

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
        If not supplied, a path under ``projects_dir()`` is generated so
        the row is immediately consistent (the caller is expected to
        ``git init`` that path before any commit). A caller-supplied
        ``git_repo_path`` is trusted: the caller must ensure the
        directory exists before ``git init``.
        """
        tags = tags or []
        # ``_default_git_path`` is looked up as a bare global at call time —
        # so a monkeypatched ``db_mod._default_git_path`` (the
        # versioning/conftest, test_projects and issue-163 seams) intercepts
        # it with its exact ``(name)`` signature. The unpatched path is
        # steered by the module-level ``APP_DATA_DIR`` the app lifespan
        # records (issue #294).
        git_path = git_repo_path if git_repo_path is not None else _default_git_path(name)
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
        return self._project_row(row)

    def list_projects(self) -> list[dict[str, Any]]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY id ASC").fetchall()
        return [self._project_row(r) for r in rows]

    @staticmethod
    def _project_row(row) -> dict[str, Any]:
        """Decode the JSON columns (tags, last_activity) into Python.

        ``last_activity`` is the library-card "last activity" field
        (issue #8): ``{"ts": iso-ts | null, "version_id": int | null,
        "name": str | null}`` — the ts/version of the latest version, the
        product's "when did the design last change" semantics.
        """
        out = dict(row)
        out["tags"] = json.loads(out.get("tags") or "[]")
        raw_la = out.get("last_activity")
        try:
            out["last_activity"] = json.loads(raw_la) if raw_la else None
        except (TypeError, ValueError):
            out["last_activity"] = None
            logger.warning(
                "project %s: corrupt last_activity JSON, falling back to None",
                out.get("id"),
            )
        return out

    def update_project(
        self,
        project_id: int,
        *,
        name: str | None = None,
        tags: list[str] | None = None,
        notes: str | None = None,
        source_photo_path: str | None = None,
        current_version: int | None = None,
        last_activity: tuple[int, str] | None = None,
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
        if current_version is not None:
            self._conn.execute(
                "UPDATE projects SET current_version = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
                (current_version, project_id),
            )
        if last_activity is not None:
            version_id, name = last_activity
            # Stamp the real wall-clock ts (json() has no now()).
            ts = self._conn.execute(
                "SELECT strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            ).fetchone()[0]
            la = json.dumps({"ts": ts, "version_id": version_id, "name": name})
            self._conn.execute(
                "UPDATE projects SET last_activity = ? WHERE id = ?",
                (la, project_id),
            )
        self._conn.commit()

    def delete_project(self, project_id: int) -> None:
        self._conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self._conn.commit()

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
        cur = self._conn.execute(
            "INSERT INTO provider_credentials (provider_id, model_alias, key_ciphertext)"
            " VALUES (?, ?, ?) ON CONFLICT(provider_id, model_alias)"
            " DO UPDATE SET key_ciphertext = excluded.key_ciphertext,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (provider_id, model_alias, key_ciphertext),
        )
        self._conn.commit()
        # cursor.lastrowid already carries the row id for both branches of
        # the upsert: the new id on INSERT, and the conflicting row's id on
        # the ON CONFLICT DO UPDATE branch — no need for a redundant SELECT.
        return int(cur.lastrowid or 0)

    def list_credentials(self) -> list[dict[str, Any]]:
        """Names-only view (what the HTTP list endpoint returns).

        Per the hard invariant, the ciphertext column is NEVER selected
        here. The HTTP layer (task-b) can safely serialise this list into
        a JSON response without any key material leaking. There is no
        public ``get_credential`` accessor: ciphertext access is owned by
        ``d33d.security.credentials.CredentialStore``, not the DB layer —
        an HTTP handler reaching for this class cannot leak key material.
        """
        rows = self._conn.execute(
            "SELECT provider_id, model_alias FROM provider_credentials ORDER BY provider_id ASC"
        ).fetchall()
        return [dict(r) for r in rows]

    # -- project_dimensions -------------------------------------------------

    def put_dimensions(
        self,
        *,
        project_id: int,
        fit_type: str,
        tolerance_mm: float,
        params: dict[str, float],
    ) -> int:
        """Insert or replace the confirmed dimension set for a project.

        ``params`` is the named-parameter map (W/D/H in mm plus the resolved
        FDM tolerance) — stored JSON-encoded in ``params`` so the #3 bbox
        gate has a record to compare against. ``fit_type`` / ``tolerance_mm``
        are denormalised onto the row (not buried in the JSON) so a
        query can filter on them without parsing. "Active version" promotion
        is issue #7's concern — this is the alongside-the-transcript record
        only, no version history.
        """
        self._conn.execute(
            "INSERT INTO project_dimensions (project_id, fit_type, tolerance_mm, params)"
            " VALUES (?, ?, ?, ?) ON CONFLICT(project_id)"
            " DO UPDATE SET fit_type = excluded.fit_type,"
            " tolerance_mm = excluded.tolerance_mm,"
            " params = excluded.params,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')",
            (project_id, fit_type, tolerance_mm, json.dumps(params)),
        )
        self._conn.commit()
        row = self._conn.execute(
            "SELECT id FROM project_dimensions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        return int(row["id"])

    def get_dimensions(self, project_id: int) -> dict[str, Any] | None:
        """The confirmed dimension set for a project (None if not yet
        confirmed — i.e. the CLARIFY gate has not passed for this project).

        Returns the row with ``params`` decoded back to a dict (the
        named-parameter map) so a caller can reconstruct the W/D/H triple
        and the resolved FDM tolerance without hand-parsing JSON.
        """
        row = self._conn.execute(
            "SELECT * FROM project_dimensions WHERE project_id = ?",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        out = dict(row)
        out["params"] = json.loads(out.get("params") or "{}")
        return out

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


def projects_dir(data_dir: str | Path | None = None) -> Path:
    """The project-repo base directory (``<data_dir>/projects``).

    ``data_dir`` defaults to the ``D33D_DATA_DIR`` env (``~/.d33d``),
    which ``d33d.main`` sets to its resolved data dir — the same directory
    it puts the DB in. For app usage the caller passes the app's data
    dir explicitly (``create_app`` records it on ``app.state.data_dir``
    and the repo default + startup migration read it from there at call
    time), so a test app built from an arbitrary DB path is steered by
    its DB path, not by a leftover env var. The directory is created if
    missing.
    """
    if data_dir is None:
        data_dir = default_data_dir()
    # Guard the resolved base on BOTH paths (explicit arg and the env/
    # default fallback, issue #310) — an explicit arg pointing at the
    # operator's real ``~/.d33d`` is the same failure mode the guard
    # exists to prevent, not an exemption from it.
    guard_real_data_path(data_dir)
    base = Path(data_dir) / "projects"
    base.mkdir(parents=True, exist_ok=True)
    return base


def _default_git_path(name: str) -> str:
    """Generate a unique on-disk path for the per-project git repo.

    The repo lands under the data-dir projects base
    (``projects_dir()/<12-hex-slug>/`` — default
    ``~/.d33d/projects/<slug>/``) so macOS's OS-temp cleanup can never
    delete a project's design source or photo. The slug (a 12-char hex of
    ``uuid4``) makes the path unique per project and keeps it from
    colliding across tests / worktrees. The caller is expected to
    ``git init`` this path before any commit.

    The base comes from the module-level :data:`APP_DATA_DIR` when the app
    has set it (issue #294 — the lifespan records the DB path's parent),
    else the ``D33D_DATA_DIR`` env default (bare-``connect()`` usage).
    Called from ``create_project`` through the module global at call time
    so a monkeypatched ``db_mod._default_git_path`` (the versioning/conftest,
    test_projects and issue-163 seams) intercepts it with its exact
    ``(name)`` signature.
    """
    base = projects_dir(APP_DATA_DIR) / uuid.uuid4().hex[:12]
    base.mkdir(parents=True, exist_ok=True)
    return str(base)


def migrate_project_repos(
    conn: Connection, projects_dir_path: str | Path | None = None
) -> dict[str, int]:
    """One-shot startup migration: move project repos into the data dir.

    For every project whose ``git_repo_path`` is NOT under
    ``projects_dir_path`` (the data-dir ``projects/`` base), the old
    directory is moved to ``<base>/<repo-basename>`` and the row's paths
    rewritten; if the old directory is gone, the row is left UNCHANGED
    with a single WARNING naming the project id only (never a path).
    Migration never blocks startup — a per-row failure is logged and
    skipped.

    Returns ``{"projects": N, "present": M, "missing": K}``, derived
    fresh from each row's post-migration ``git_repo_path``.
    """
    base = (
        Path(projects_dir_path) if projects_dir_path is not None else projects_dir()
    )
    base_resolved = base.resolve()
    n = 0
    for p in conn.list_projects():
        pid = p["id"]
        n += 1
        old = Path(p["git_repo_path"])
        if old == base or base in old.parents:
            continue
        new = base / old.name
        if new.exists():
            # A prior row in this run (or a prior migration) already owns this
            # basename: without the guard, ``shutil.move`` would move ``old``
            # *inside* the existing directory (``<base>/<name>/<name>``) while
            # the row points at the outer one — silent data loss. Resolve the
            # collision with a short suffix instead, and log it.
            new = base / f"{old.name}-{uuid.uuid4().hex[:4]}"
            logger.warning(
                "project %s: basename %s already exists in the projects base — "
                "moved under a collision suffix",
                pid,
                old.name,
            )
        if not old.is_dir():
            logger.warning(
                "project %s: repo directory missing — left unchanged; "
                "design source and photo are unrecoverable",
                pid,
            )
            continue
        # Containment: the basename comes from trusted DB content, but a
        # traversal (or a symlinked base) must not move the repo outside
        # ``<data_dir>/projects/``.
        if not new.is_absolute() or not new.resolve().is_relative_to(base_resolved):
            logger.warning(
                "project %s: repo move target escapes the projects base — "
                "skipped",
                pid,
            )
            continue
        new_photo = p.get("source_photo_path")
        if new_photo:
            photo_p = Path(new_photo)
            if photo_p == old or old in photo_p.parents:
                new_photo = str(photo_p if photo_p == old else new / photo_p.relative_to(old))
        # Move the repo FIRST: a failed move leaves the row unchanged
        # (repo still at the old path, row still points at the old path —
        # the next run retries the migration). A failed DB update leaves
        # the repo already moved but the row pointing at the old path —
        # the next run sees the old path missing and leaves the row
        # unchanged (the operator recovers manually either way).
        try:
            shutil.move(str(old), str(new))
        except OSError as e:
            logger.warning("project %s: could not move repo: %s", pid, e)
            continue
        conn.raw.execute(
            "UPDATE projects SET git_repo_path = ?, "
            "source_photo_path = COALESCE(?, source_photo_path) WHERE id = ?",
            (str(new), new_photo, pid),
        )
        conn.commit()
        logger.info("project %s: moved repo to projects/%s", pid, old.name)
    # Summary derived fresh from disk (never a persisted flag that could
    # go stale), logged as exactly one line — the fresh-install 0/0/0
    # case included.
    present = sum(
        1 for p in conn.list_projects() if Path(p["git_repo_path"]).is_dir()
    )
    missing = n - present
    logger.info("%d projects, %d repos present, %d missing", n, present, missing)
    return {"projects": n, "present": present, "missing": missing}
