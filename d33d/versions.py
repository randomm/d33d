"""Version management: project history as versions (issue #8).

A version is a NAMED COMPLETE PARAMETER SET, stored as a full snapshot (never
a delta) and committed to the per-project git repo as one file:
``versions/{id}/params.json``. The git commit is the content spine — the
database row carries only the id, the display name, the message provenance
and the flags. Git is NEVER shown: nothing here returns a commit hash,
branch name, or raw git output; a test asserts the invariant.

Settled semantics (issue #8 gap-gate findings, per operator-approved
defaults stored in project memory):

* **Non-destructive restore.** Restoring from version N creates a NEW
  forward version whose params equal N's snapshot and whose ``parent`` is
  the CURRENT latest version — never a git reset. Restoring the current
  latest version is a no-op (dedupe). Provenance is the dedicated
  ``restored_from`` field — the user-editable ``name`` never carries it.
* **One version per accepted change.** The design loop's FINALIZE result
  (``run_design_loop`` status "pass") is the sole version-creation trigger
  on the HTTP boundary (see ``d33d.app.finalize_design``); no other agent
  event versions.
* **Forks are variant cards, not a git graph.** Branch-from creates a NEW
  PROJECT with its own git repo; its first version is seeded from the
  source version's full snapshot, tagged ``forked_from``. The new repo's
  history contains no source-project commit.
* **Set as main.** Re-points ``current_version`` in place AND writes a
  no-change marker commit (``versions/main_{seq}.json``) so the git
  history records the switch without rewriting anything.
* **Serialization.** One writer per project (``asyncio.Lock`` per project
  id) so the chain stays linear: parent pointers are read under the lock,
  so a concurrent create always observes the true latest — plain creates
  never collide (each wins in turn, appended to the chain head). 409s are
  reserved for the stale-target races (a restore whose target became the
  latest, or a fork seed into a non-empty project).
* **Auto-name.** First 40 chars of the triggering message, sanitized to
  ``[a-z0-9 -]`` (spaces kept, all else dropped); on collision a numeric
  suffix is appended. User-editable, but renaming never touches params,
  parent, or the git commit.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from d33d import db as db_mod
from d33d.projects import _sanitize_commit_message

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Max display name length (auto-names are derived to this).
NAME_MAX_LEN = 40

#: ``Version.params`` values are scalars only (number | string | bool).
ParamValue = float | str | bool

#: ``versions/{id}/params.json`` — the full snapshot committed to git.
PARAMS_FILENAME = "params.json"

#: ``versions/main_{seq}.json`` — set-as-main marker (no change).
MAIN_MARKER_PREFIX = "main_"

_GIT_USER_EMAIL = "d33d@local"
_GIT_USER_NAME = "d33d"

_NAME_SANITIZE_RE = re.compile(r"[^a-z0-9 \-]")

#: Git hash / git-command leak detection for the "git is never shown"
#: contract tests. A 40-hex string is a commit hash by construction
#: (uuid4 hex is 32 chars, so 40 is hash-specific).
_HASH_RE = re.compile(r"\b[0-9a-f]{40}\b")
_GIT_RE = re.compile(r"\bgit(?!hub|lab)")


def derive_auto_name(message: str, existing_names: set[str] | None = None) -> str:
    """Auto-name from a triggering user message.

    First :data:`NAME_MAX_LEN` chars, lowercased, reduced to
    ``[a-z0-9 -]`` (spaces kept — readable names, not slugs). On collision
    with ``existing_names`` a numeric suffix (``-2``, ``-3``, ...) is
    appended. Deterministic for a given (message, existing) pair.
    """
    base = _NAME_SANITIZE_RE.sub("", message.lower())[:NAME_MAX_LEN].strip()
    if not base:
        base = "version"
    candidate = base
    n = 2
    while existing_names is not None and candidate in existing_names:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def diff_params(
    a: dict[str, ParamValue], b: dict[str, ParamValue]
) -> tuple[list[str], list[str], list[str]]:
    """Diff two full parameter snapshots → (added, removed, changed).

    ``added`` = keys in ``b`` not in ``a``; ``removed`` = the inverse;
    ``changed`` = keys in both with differing values. All lists are
    sorted for deterministic output.
    """
    added = sorted(k for k in b if k not in a)
    removed = sorted(k for k in a if k not in b)
    changed = sorted(k for k in a if k in b and a[k] != b[k])
    return added, removed, changed


def _valid_param_value(value: Any) -> bool:
    """``params`` is a ``{name: scalar}`` map — number | string | bool."""
    if isinstance(value, bool):
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return True
    return isinstance(value, str)


def validate_params(params: dict[str, Any]) -> list[str]:
    """Keys (sorted) whose values are not scalars — empty means valid."""
    return sorted(k for k, v in params.items() if not _valid_param_value(v))


# ---------------------------------------------------------------------------
# Git helpers (local only, no network — same discipline as d33d.projects)
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``repo_dir``. Raises on non-zero exit."""
    cmd = ["git", "-C", str(repo_dir), *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result


def init_git_repo(repo_dir: Path) -> None:
    """``git init`` + per-repo identity. Idempotent."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _git(repo_dir, "init", "-q")
        _git(repo_dir, "config", "user.email", _GIT_USER_EMAIL)
        _git(repo_dir, "config", "user.name", _GIT_USER_NAME)


def _commit_versions_file(
    repo_dir: Path, versions_subdir: Path, rel_name: str, message: str
) -> None:
    """Write ``versions_subdir / rel_name`` (already on disk) and commit."""
    _git(repo_dir, "add", "-A")
    status = _git(repo_dir, "status", "--porcelain")
    if status.stdout.strip():
        _git(repo_dir, "commit", "-q", "-m", message)


def install_text_file_atomic(path: Path, content: str) -> None:
    """Atomically write ``content`` to ``path`` (temp file + ``os.replace``,
    same discipline as the catalogue write in ``d33d.app``).

    ``os.replace`` is atomic on POSIX, so a reader (``GET`` of the same
    path) never observes a half-written file. Callers that commit the file
    to git afterwards should ``unlink`` the path on commit failure —
    undoing the replace restores the pre-write state (the prior committed
    content, or nothing)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), suffix=".tmp", prefix=path.name + "."
    )
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Version operations (the DB + git write paths — one writer per project)
# ---------------------------------------------------------------------------


class VersionService:
    """Owns the version write path for one application (one DB connection).

    Reads the shared ``db.Connection`` at construction. All mutations go
    through :meth:`_with_project_lock` so a per-project ``asyncio.Lock``
    serializes writes (concurrent creates queue and each observes the true
    latest — the chain stays linear) and parent pointers can never branch
    or point at a stale latest version. Stale-target mutations (restore
    whose target became latest, fork seed into a non-empty project) 409
    instead of corrupting the chain.
    """

    def __init__(self, conn: db_mod.Connection) -> None:
        self.conn = conn
        self._locks: dict[int, asyncio.Lock] = {}
        self._write_lock = asyncio.Lock()

    # -- lock ---------------------------------------------------------------

    def _lock_for(self, project_id: int) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    # -- queries ------------------------------------------------------------

    def list_versions(self, project_id: int) -> list[dict[str, Any]]:
        """All versions of a project, oldest first (id order)."""
        rows = self.conn.raw.execute(
            "SELECT * FROM versions WHERE project_id = ? ORDER BY id ASC",
            (project_id,),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, project_id: int, version_id: int) -> dict[str, Any] | None:
        row = self.conn.raw.execute(
            "SELECT * FROM versions WHERE project_id = ? AND id = ?",
            (project_id, version_id),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_version(row)

    def latest_version(self, project_id: int) -> dict[str, Any] | None:
        row = self.conn.raw.execute(
            "SELECT * FROM versions WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (project_id,),
        ).fetchone()
        if row is None:
            return None
        return self._row_to_version(row)

    def get_project(self, project_id: int) -> dict[str, Any] | None:
        return self.conn.get_project(project_id)

    def list_pinned(self, project_id: int) -> list[dict[str, Any]]:
        """Pinned (not archived) versions, oldest first — the gallery."""
        return [
            v
            for v in self.list_versions(project_id)
            if v["pinned"] and not v["archived"]
        ]

    def get_compare(self, project_id: int, a_id: int, b_id: int) -> dict[str, Any]:
        """Compare two versions: full param sets, diff table, and the
        shared-rotation contract (identical mm units, no geometry payload)."""
        va = self.get_version(project_id, a_id)
        vb = self.get_version(project_id, b_id)
        if va is None or vb is None:
            raise LookupError("version not found")
        added, removed, changed = diff_params(va["params"], vb["params"])
        return {
            "project_id": project_id,
            "a": self._version_public(va),
            "b": self._version_public(vb),
            "diff": {
                "added": added,
                "removed": removed,
                "changed": changed,
                "count": len(added) + len(removed) + len(changed),
            },
            # Shared-rotation contract: both sides reference the SAME unit
            # system and axis convention, so the two client viewports can
            # share one camera/rotation state. No geometry is returned.
            "shared_rotation": {
                "units": "mm",
                "axis_convention": "z-up",
                "identical_convention": True,
            },
        }

    def library_cards(self) -> list[dict[str, Any]]:
        """Project library grid: one card per project with name, tags, notes,
        current_version, last_activity, and the latest version's thumbnail.
        (Single query per project for the latest version; the N+1 is
        acceptable at personal-app scale — the index on (project_id, id)
        keeps each lookup O(log n).)"""
        out = []
        for row in self.conn.list_projects():
            latest = self.latest_version(row["id"])
            card = {
                "id": row["id"],
                "name": row["name"],
                "tags": row["tags"],
                "notes": row["notes"],
                "current_version": row.get("current_version"),
                "last_activity": row.get("last_activity"),
                "thumbnail": (latest or {}).get("thumbnail"),
            }
            out.append(card)
        return out

    # -- serialization helper ----------------------------------------------

    async def _with_project_lock(
        self, project_id: int, fn: Callable[[], Any]
    ) -> Any:
        """Serialize version writes for one project (AND across projects,
        since git + a shared SQLite write path must be single-writer).

        ``fn`` must be a zero-arg callable that returns a coroutine (an
        ``async def`` body) or a plain sync result. The coroutine is
        created when ``fn()`` is called — inside the locked region — so
        the lock is held for the full body.
        """
        async with self._write_lock, self._lock_for(project_id):
            result = fn()
            if hasattr(result, "__await__"):
                return await result
            return result

    # -- mutations ----------------------------------------------------------

    async def create_version(
        self,
        project_id: int,
        params: dict[str, ParamValue],
        *,
        name: str | None = None,
        message: str = "",
        restored_from: int | None = None,
        forked_from: tuple[int, int] | None = None,
        thumbnail: str | None = None,
    ) -> dict[str, Any]:
        """Public create: serialize (per project), then run the create
        body. The body lives in ``_run_create`` so nested callers (restore,
        fork) can reuse it without re-entering the write lock.

        Serialized per project: concurrent calls to the same project run
        one at a time; a plain create always succeeds (parent is read
        under the lock, so each create appends to the chain head). A
        caller that pinned a stale ``restored_from`` target that vanished
        underneath it, or a ``forked_from`` seed arriving at a project
        that is no longer empty, gets a ``VersionConflictError`` instead
        of forking the chain.
        """
        return await self._with_project_lock(
            project_id,
            lambda: self._run_create(
                project_id,
                params,
                name=name,
                message=message,
                restored_from=restored_from,
                forked_from=forked_from,
                thumbnail=thumbnail,
            ),
        )

    async def _run_create(
        self,
        project_id: int,
        params: dict[str, ParamValue],
        *,
        name: str | None = None,
        message: str = "",
        restored_from: int | None = None,
        forked_from: tuple[int, int] | None = None,
        thumbnail: str | None = None,
    ) -> dict[str, Any]:
        """The create body (call under the write lock)."""
        project = self.conn.get_project(project_id)
        if project is None:
            raise LookupError(f"project {project_id} not found")
        bad = validate_params(params)
        if bad:
            raise ValueError(f"params must be scalars; bad keys: {bad}")

        existing = {v["name"] for v in self.list_versions(project_id)}
        clean_name = name.strip() if name else None
        if clean_name is not None and not clean_name:
            clean_name = None
        auto = derive_auto_name(message, existing)
        version_name = clean_name if clean_name is not None else auto
        latest = self.latest_version(project_id)
        parent = latest["id"] if latest is not None else None

        # Provenance consistency (race window): if the caller pinned a
        # restored_from target it must still exist — restore_version's
        # latest-check runs under the lock just above, and the row is not
        # deleted in this flow, so existence is the only check here (the
        # "target is already latest" no-op case is handled by
        # restore_version, which 409s it).
        if restored_from is not None:
            target = self.get_version(project_id, restored_from)
            if target is None:
                raise VersionConflictError(
                    "restore target vanished while creating a restore"
                )
        if forked_from is not None and parent is not None:
            raise VersionConflictError("fork seed into non-empty project")

        version_id = self._insert_version(
            project_id,
            params,
            name=version_name,
            created_by_message=message,
            parent=parent,
            restored_from=restored_from,
            forked_from=forked_from,
            thumbnail=thumbnail,
        )

        # Commit the full snapshot to the project's git repo.
        repo_dir = Path(project["git_repo_path"])
        versions_dir = repo_dir / "versions"
        vdir = versions_dir / str(version_id)
        snapshot_path = vdir / PARAMS_FILENAME
        try:
            install_text_file_atomic(
                snapshot_path, json.dumps(params, indent=2, sort_keys=True) + "\n"
            )
            commit_subject = (
                f"version: {_sanitize_commit_message(version_name)}"
                if restored_from is None
                else f"restored from version {restored_from}: "
                f"{_sanitize_commit_message(version_name)}"
            )
            _commit_versions_file(
                repo_dir,
                versions_dir,
                f"{version_id}/{PARAMS_FILENAME}",
                commit_subject,
            )
        except (RuntimeError, OSError) as e:
            # Keep DB, git, and filesystem consistent: roll back the row
            # and clean up the snapshot file if the write succeeded.
            snapshot_path.unlink(missing_ok=True)
            self.conn.raw.execute("DELETE FROM versions WHERE id = ?", (version_id,))
            self.conn.commit()
            raise RuntimeError(f"version commit failed: {e}") from e

        # Advance the project pointer + activity (inside the write
        # lock, same writer as the version row above).
        self.conn.update_project(
            project_id,
            current_version=version_id,
            last_activity=(version_id, version_name),
        )
        row = self.get_version(project_id, version_id)
        if row is None:
            raise LookupError(f"version {version_id} not found")
        return row

    async def restore_version(self, project_id: int, version_id: int) -> dict[str, Any]:
        """Non-destructive restore: a NEW forward version with the target's
        full snapshot; parent = current latest. No-op (409-style) when the
        target IS the current latest — dedupe, no spurious entry."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")

        async def _restore() -> dict[str, Any]:
            latest = self.latest_version(project_id)
            if latest is not None and latest["id"] == target["id"]:
                raise VersionConflictError(
                    "restore target is already the latest version (no-op)"
                )
            return await self._run_create(
                project_id,
                dict(target["params"]),
                name=None,  # auto-named from the provenance below
                message=f"restored from version {version_id}",
                restored_from=version_id,
            )

        return await self._with_project_lock(project_id, _restore)

    async def set_as_main(self, project_id: int, version_id: int) -> dict[str, Any]:
        """Set as main: re-point ``current_version`` in place AND write a
        no-change marker commit so the git history records the switch
        without rewriting anything."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")

        def _set_main() -> dict[str, Any]:
            project = self.conn.get_project(project_id)
            assert project is not None
            latest = self.latest_version(project_id)
            seq = latest["id"] + 1 if latest is not None else 1
            repo_dir = Path(project["git_repo_path"])
            versions_dir = repo_dir / "versions"
            marker_name = f"{MAIN_MARKER_PREFIX}{seq}.json"
            marker_dir = versions_dir
            marker_dir.mkdir(parents=True, exist_ok=True)
            marker = marker_dir / marker_name
            # The marker file itself carries no new parameters — it records
            # the pointer move. Written as JSON so the diff is readable.
            install_text_file_atomic(
                marker,
                json.dumps(
                    {
                        "event": "set_as_main",
                        "version": target["id"],
                        "note": f"current version set to '{target['name']}'",
                    },
                    indent=2,
                )
                + "\n",
            )
            try:
                _commit_versions_file(
                    repo_dir,
                    versions_dir,
                    marker_name,
                    f"set as main: {_sanitize_commit_message(target['name'])}",
                )
            except RuntimeError as e:
                marker.unlink(missing_ok=True)
                raise RuntimeError(f"git commit failed: {e}") from e
            # Re-point the project pointer to the chosen version (the
            # marker commit above records the switch in git history). If
            # this fails the marker commit exists in git without a DB row
            # — the divergence is observable via the marker file.
            self.conn.update_project(
                project_id,
                current_version=version_id,
                last_activity=(target["id"], target["name"]),
            )
            row = self.conn.get_project(project_id)
            if row is None:
                raise LookupError(f"project {project_id} not found")
            return self._public_project(row)

        return await self._with_project_lock(project_id, _set_main)

    async def rename_version(
        self, project_id: int, version_id: int, name: str
    ) -> dict[str, Any]:
        """Rename: only the display name changes. params, parent, and the
        git commit are untouched."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")
        clean = name.strip()
        if not clean:
            raise ValueError("name must be non-empty")
        self.conn.raw.execute(
            "UPDATE versions SET name = ?, updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id = ?",
            (clean, version_id),
        )
        self.conn.commit()
        v = self.get_version(project_id, version_id)
        if v is None:
            raise LookupError(f"version {version_id} not found")
        return v

    async def set_pinned(
        self, project_id: int, version_id: int, pinned: bool
    ) -> dict[str, Any]:
        """Pin/unpin. Unpinning never deletes the version or its commit."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")
        self.conn.raw.execute(
            "UPDATE versions SET pinned = ? WHERE id = ?",
            (1 if pinned else 0, version_id),
        )
        self.conn.commit()
        v = self.get_version(project_id, version_id)
        if v is None:
            raise LookupError(f"version {version_id} not found")
        return v

    async def set_archived(
        self, project_id: int, version_id: int, archived: bool
    ) -> dict[str, Any]:
        """Archive/unarchive. Archived variants are hidden from the default
        gallery listing but still restorable."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")
        self.conn.raw.execute(
            "UPDATE versions SET archived = ? WHERE id = ?",
            (1 if archived else 0, version_id),
        )
        self.conn.commit()
        v = self.get_version(project_id, version_id)
        if v is None:
            raise LookupError(f"version {version_id} not found")
        return v

    async def branch_from(
        self,
        project_id: int,
        version_id: int,
        *,
        git_path_factory: Callable[[str], str] = db_mod._default_git_path,
    ) -> dict[str, Any]:
        """Branch-from: create a NEW PROJECT (own git repo) whose first
        version is seeded from the source version's full snapshot.

        The new repo's history contains NO source-project commit (forks are
        variant cards, not a git graph) — the seed is a fresh
        ``versions/{id}/params.json`` commit. Provenance lives in the
        version row's ``forked_from`` column and the commit message;
        the repo carries no separate fork marker file.
        """
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")

        async def _branch() -> dict[str, Any]:
            src_project = self.conn.get_project(project_id)
            assert src_project is not None
            new_repo = git_path_factory(f"fork-{target['id']}")
            new_project_id = self.conn.create_project(
                name=f"{src_project['name']} (variant)",
                tags=src_project["tags"],
                notes="",
                git_repo_path=new_repo,
            )
            try:
                init_git_repo(Path(new_repo))
                seed = await self._run_create(
                    new_project_id,
                    dict(target["params"]),
                    name=target["name"],
                    message=f"forked from project {project_id} version {version_id}",
                    forked_from=(project_id, version_id),
                )
                self.conn.update_project(new_project_id, current_version=seed["id"])
            except (LookupError, ValueError, VersionConflictError, RuntimeError, OSError):
                # Roll back: remove the new project's repo + row so a
                # failed branch never leaves an orphan variant card.
                shutil.rmtree(Path(new_repo), ignore_errors=True)
                try:
                    self.conn.delete_project(new_project_id)
                except sqlite3.Error:
                    # The repo is already removed; if the DB row deletion
                    # also fails the orphan row is still observable in logs.
                    logger.warning(
                        "branch-from rollback: failed to delete project row %s (repo already removed)",
                        new_project_id,
                    )
                raise
            row = self.conn.get_project(new_project_id)
            if row is None:
                raise LookupError(f"project {new_project_id} not found")
            return {
                "project": self._public_project(row),
                "version": seed,
            }

        return await self._with_project_lock(project_id, _branch)

    # -- internals ----------------------------------------------------------

    def _insert_version(
        self,
        project_id: int,
        params: dict[str, ParamValue],
        *,
        name: str,
        created_by_message: str,
        parent: int | None,
        restored_from: int | None,
        forked_from: tuple[int, int] | None,
        thumbnail: str | None,
    ) -> int:
        fork = None
        if forked_from is not None:
            fork = [forked_from[0], forked_from[1]]
        cur = self.conn.raw.execute(
            "INSERT INTO versions"
            " (project_id, params, name, created_by_message, parent,"
            "  restored_from, forked_from, thumbnail)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                json.dumps(params, sort_keys=True),
                name,
                created_by_message,
                parent,
                restored_from,
                json.dumps(fork) if fork else None,
                thumbnail,
            ),
        )
        self.conn.commit()
        return int(cur.lastrowid or 0)

    @staticmethod
    def _row_to_version(row) -> dict[str, Any]:
        out = dict(row)
        out["params"] = json.loads(out.get("params") or "{}")
        fork = out.get("forked_from")
        out["forked_from"] = tuple(json.loads(fork)) if fork else None
        out["pinned"] = bool(out.get("pinned"))
        out["archived"] = bool(out.get("archived"))
        return out

    @staticmethod
    def _public_project(row: dict[str, Any] | None) -> dict[str, Any] | None:
        """A project row with the raw git-repo path removed (git
        invisibility — the on-disk path names a git repo and is never
        exposed in an API response)."""
        if row is None:
            return None
        out = dict(row)
        out.pop("git_repo_path", None)
        return out

    @staticmethod
    def _version_public(version: dict[str, Any]) -> dict[str, Any]:
        """Public shape for a version (no git internals, no DB-only fields)."""
        return {
            "id": version["id"],
            "name": version["name"],
            "params": version["params"],
            "created_by_message": version["created_by_message"],
            "parent": version["parent"],
            "restored_from": version["restored_from"],
            "forked_from": version["forked_from"],
            "pinned": version["pinned"],
            "archived": version["archived"],
            "thumbnail": version["thumbnail"],
            "created_at": version["created_at"],
        }


class VersionConflictError(Exception):
    """A version mutation lost its race (stale target / no-op restore).

    The HTTP layer maps this to 409 — the chain stays linear and the
    loser's request is rejected rather than forking the history."""


def _ensure_column(
    conn: db_mod.Connection,
    table: str,
    column: str,
    column_sql: str,
    *,
    backfill_value: str | None = None,
) -> None:
    """Idempotently add ``column`` to an existing table (ALTER TABLE ADD
    COLUMN has no IF NOT EXISTS; check pragma first).

    SQLite's ``ALTER TABLE ADD COLUMN`` rejects a function-call (non-
    constant) default — e.g. ``DEFAULT (json(...))`` is legal in the
    CREATE TABLE DDL but not here — and a NOT NULL column must arrive WITH
    a default. So when ``backfill_value`` is given (a JSON literal), the
    column is added as plain ``NULL``-able TEXT and the pre-existing rows
    are updated in the same step; the DDL in ``d33d.db`` (fresh DBs) keeps
    the function-call default. ``column_sql`` is the ALTER's declaration
    (e.g. ``INTEGER`` or ``TEXT``)."""
    info = conn.raw.execute(f"PRAGMA table_info({table})").fetchall()
    names = {r[1] for r in info}
    if column not in names:
        conn.raw.execute(f"ALTER TABLE {table} ADD COLUMN {column} {column_sql}")
        if backfill_value is not None:
            conn.raw.execute(
                f"UPDATE {table} SET {column} = ? WHERE {column} IS NULL",
                (backfill_value,),
            )
        conn.commit()


def migrate(conn: db_mod.Connection) -> None:
    """Apply the issue #8 schema additions idempotently.

    Called from ``d33d.app``'s lifespan so every app (and every test
    harness) that opens the DB gets the versions table + the new project
    columns without a migration tool. Fresh DBs already have the project
    columns from the ``projects`` DDL in ``d33d.db`` — the ALTERs here
    exist for databases created before this ticket landed.
    """
    conn.raw.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
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
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    conn.raw.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_project_id ON versions(project_id, id)"
    )
    conn.commit()
    # New columns on projects (existing DBs predate this ticket).
    _ensure_column(conn, "projects", "current_version", "INTEGER")
    # ``last_activity``: the function-call default (json(...)) is legal in
    # the CREATE TABLE DDL in d33d.db (fresh DBs) but NOT in an ALTER TABLE
    # ADD COLUMN (pre-existing DBs — SQLite rejects non-constant defaults
    # there), so the column is added without a default and the pre-existing
    # rows are backfilled with the same null JSON literal.
    _ensure_column(
        conn,
        "projects",
        "last_activity",
        "TEXT",
        backfill_value='{"ts": null, "version_id": null, "name": null}',
    )


__all__ = [
    "MAIN_MARKER_PREFIX",
    "NAME_MAX_LEN",
    "ParamValue",
    "VersionConflictError",
    "VersionService",
    "derive_auto_name",
    "diff_params",
    "init_git_repo",
    "install_text_file_atomic",
    "migrate",
    "validate_params",
]
