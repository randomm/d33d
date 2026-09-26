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
from d33d.design_loop import BBOX_TOLERANCE_MIN_MM, BBOX_TOLERANCE_REL
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


#: Control characters stripped from model-supplied names (issue #245):
#: a title is untrusted text and must not smuggle a control character
#: into the display name.
_CTRL_RE = re.compile(r"[\x00-\x1f\x7f]")

#: Runs of whitespace collapsed to one space (ends are trimmed by
#: :func:`clean_name`).
_WS_RUN_RE = re.compile(r"\s+")


def clean_name(raw: str, existing_names: set[str] | None = None) -> str:
    """Clean a model-supplied name (a ``// title:`` value, a param-diff
    phrase) to a display name — issue #245.

    Deliberately NOT the message-derived ``derive_auto_name`` sanitizer:
    titles and diff phrases keep case and ordinary punctuation (``_ . → ×
    - ,``). Control characters are stripped, whitespace runs collapsed, the
    ends trimmed, and the result capped at :data:`NAME_MAX_LEN`. A
    whitespace-only input yields ``"version"`` (the same honest fallback
    as ``derive_auto_name``); on collision with ``existing_names`` the
    existing numeric-suffix behaviour applies.
    """
    base = _CTRL_RE.sub("", raw)
    base = _WS_RUN_RE.sub(" ", base)
    base = base[:NAME_MAX_LEN].strip()
    if not base:
        base = "version"
    candidate = base
    n = 2
    while existing_names is not None and candidate in existing_names:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def _param_value_str(value: ParamValue) -> str:
    """Render a param value without a spurious trailing zero (12.0 →
    ``"12"``). Non-numeric values (strings, bools) render verbatim."""
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


#: ``W × D × H`` dimension phrase in a version title — the three number-
#: groups joined by ``x`` (no spaces: the design titles the model emits
#: use the compact ``60x45x20`` form) or ``×``. Optional ``mm`` suffix
#: allowed (``60x45x20mm``). Leading/trailing word boundaries keep the
#: match from latching onto a longer digit run (``160x45`` does not match
#: ``160x45x20``'s ``60x45`` tail).
_DIM_TRIPLE_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)\s*[x×]\s*(\d+(?:\.\d+)?)(?:\s*mm)?\b")

#: ``N mm`` — a number immediately followed by the ``mm`` unit (no spaces
#: between number and unit; a space reads as prose, not a dimension —
#: "a 20 mm tray" states a dimension in prose, not in a name badge).
_DIM_MM_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm\b")


def sanitize_dimension_phrase(
    name: str, measured_bbox: tuple[float, float, float] | None
) -> str:
    """Strip dimension claims from a version title that the measured part
    does not support (issue #276, operator decision) — a pure function.

    A version name must never carry dimensions the part does not measure.
    If ``measured_bbox`` (the whole-mesh extents of the render that became
    the version) is ``None`` — no measurement was obtained — nothing can be
    checked, so the name is returned untouched (an absent measurement
    abstains; never a fabricated extent, the issue #91/#137 precedent).

    Otherwise EVERY dimension-like number in the name — each number of an
    ``N x N (x N)`` triple (``x`` or ``×`` join, optional ``mm`` suffix) and
    every number of an ``N mm`` phrase — must match SOME measured extent
    within the bbox tolerance ``max(BBOX_TOLERANCE_REL × extent,
    BBOX_TOLERANCE_MIN_MM)`` (1% / 0.5 mm, the SAME tolerance as bit 3 —
    deliberately not bit 5's disagrees-major threshold). If ANY such number
    fails to match, the WHOLE matching dimension phrase is stripped (the
    operator's worked example: "Flared lip tray 60x45x20" with bbox
    64×49×102 → "Flared lip tray" — 20 does not match any extent, so the
    entire ``60x45x20`` phrase goes, even though 60 and 45 are within
    tolerance of 64 and 49). Whitespace is re-collapsed after the strip.

    If the result is empty or has FEWER than 2 letters (a title that was
    purely dimensions — "60x45x20" → "" — or a badge that left only a
    fragment), ``""`` is returned: the caller falls back to
    :func:`param_diff_name` (never an empty or meaningless name).
    """
    if measured_bbox is None:
        return name
    x, y, z = measured_bbox
    extents = (x, y, z)

    def _matches_extent(value: float) -> bool:
        return any(
            abs(value - extent)
            <= max(BBOX_TOLERANCE_REL * extent, BBOX_TOLERANCE_MIN_MM)
            for extent in extents
        )

    def _strip_triple(match: re.Match[str]) -> str:
        try:
            values = tuple(float(g) for g in match.groups())
        except ValueError:
            logger.debug(
                "dimension triple %r in name %r did not parse — leaving it",
                match.group(0),
                name,
            )
            return match.group(0)  # unparseable — leave it, never mangle
        if all(_matches_extent(v) for v in values):
            return match.group(0)
        return ""

    def _strip_mm(match: re.Match[str]) -> str:
        try:
            value = float(match.group(1))
        except ValueError:
            return match.group(0)
        if _matches_extent(value):
            return match.group(0)
        return ""

    # A measurement with a non-numeric extent is a malformed bbox, not a
    # measurement the name can be checked against: return the name
    # unchanged (the same honest abstain as ``None`` — never a strip
    # based on a number the measurement did not establish).
    if any(
        not isinstance(e, (int, float)) or isinstance(e, bool)
        for e in (x, y, z)
    ):
        return name

    result = _DIM_TRIPLE_RE.sub(_strip_triple, name)
    result = _DIM_MM_RE.sub(_strip_mm, result)
    # A strip that removed a mid-name phrase leaves a dangling separator
    # ("Tray 60x45x20, rev 2" → "Tray , rev 2"): collapse a separator that
    # is stranded between two spaces before the whitespace re-collapse, so
    # the user sees "Tray rev 2", never "Tray , rev 2" (issue #276
    # adversarial finding 4 — cosmetic, but the name is user-facing).
    result = re.sub(r"\s+([,;:])\s*\s+", " ", result)
    result = _WS_RUN_RE.sub(" ", result).strip()
    if not result or sum(c.isalpha() for c in result) < 2:
        return ""
    return result


def resolve_version_name(
    client_name: str | None,
    scad_source: str | None,
    *,
    measured_bbox: tuple[float, float, float] | None,
    prev_params: dict[str, ParamValue] | None,
    new_params: dict[str, ParamValue],
    existing_names: set[str] | None = None,
) -> str:
    """The version display name for a new version (issue #276) — one
    resolver for BOTH version-creation paths (the chat adapter's
    ``_resolve_version_create`` and the finalize route), so the two paths
    can never diverge in naming precedence.

    Name source, in strict precedence:

    1. the client's ``name`` (the finalize route's body field — a user
       statement wins over the model's title);
    2. the model's ``// title:`` comment in the candidate's own SCAD
       (``d33d.design_loop.scad_title``); the chat path (no client
       ``name``) falls straight to this source;
    3. a deterministic phrase from the param diff vs the previous
       version's params (:func:`param_diff_name`).

    Every non-empty candidate is first run through
    :func:`sanitize_dimension_phrase` against ``measured_bbox`` (a name
    must never carry dimensions the part does not measure — issue #276,
    operator decision; ``None`` abstains — the name is used as-is). A
    falsy result (a title that WAS the dimensions — the strip left
    nothing meaningful) falls back to the param-diff phrase. The chosen
    string is cleaned through :func:`clean_name` with the project's
    existing version names as the collision baseline (the #245 follow-up
    — two versions never share a name).
    """
    from d33d.design_loop import scad_title

    source = client_name
    if source is None or not source:
        title = scad_title(scad_source) if scad_source else None
        source = title if title else ""
    if source:
        sanitized = sanitize_dimension_phrase(source, measured_bbox)
        if sanitized:
            return clean_name(sanitized, existing_names)
    return clean_name(param_diff_name(prev_params, new_params), existing_names)


def param_diff_name(
    prev_params: dict[str, ParamValue] | None,
    new_params: dict[str, ParamValue],
) -> str:
    """A deterministic phrase from the param diff vs the previous version
    (issue #245's fallback name source).

    - no previous version → ``"First design"``
    - exactly one value-changed param (none added / removed) →
      ``"<name> <old> → <new>"`` (e.g. ``"hole_diameter 3.3 → 3.8"``)
    - any other non-empty diff → ``"<n> parameters changed"`` (n =
      added + removed + changed)
    - identical params → ``"Revised geometry"``
    """
    if prev_params is None:
        return "First design"
    added, removed, changed = diff_params(prev_params, new_params)
    if len(changed) == 1 and not added and not removed:
        key = changed[0]
        return f"{key} {_param_value_str(prev_params[key])} → {_param_value_str(new_params[key])}"
    n = len(added) + len(removed) + len(changed)
    if n == 0:
        return "Revised geometry"
    return f"{n} parameters changed"


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
        scad_source: str | None = None,
        bbox: tuple[float, float, float] | None = None,
        render_artifact_dir: str | None = None,
        stated_dims: dict[str, float] | None = None,
        param_meta: dict[str, Any] | None = None,
        confirmed_params: dict[str, Any] | None = None,
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

        ``bbox`` (issue #137): the measured per-axis extents of the render
        that produced this version — threaded by the design-loop adapter
        from the best candidate's render (``BboxInfo`` → the matched
        component's extents for a multi-part model, ``None`` when the
        measurement was not obtainable). It is PERSISTED, never
        re-derived; ``None`` stores a NULL (an absent measurement
        abstains — never a fabricated ``(0, 0, 0)``).

        ``render_artifact_dir`` (issue #163): the on-disk path of the
        per-render directory (``<renders_dir>/<uuid8>``) whose
        ``model.stl`` produced this version — threaded by the
        design-loop adapter from the best candidate's render's
        ``RenderResult.render_artifact_dir`` (issue #72's durable
        per-render directory). It is PERSISTED, never re-derived:
        renders land under a per-render uuid unrelated to version ids, so
        a version-to-render link is not inferable after the fact (no
        mtime inference, ever). ``None`` stores a NULL — a pre-existing
        row (pre-#163) or a version created without a recorded render
        must degrade honestly (the 3MF route 409s), never guess which
        directory might be its own.
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
                scad_source=scad_source,
                bbox=bbox,
                render_artifact_dir=render_artifact_dir,
                stated_dims=stated_dims,
                param_meta=param_meta,
                confirmed_params=confirmed_params,
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
        scad_source: str | None = None,
        bbox: tuple[float, float, float] | None = None,
        render_artifact_dir: str | None = None,
        stated_dims: dict[str, float] | None = None,
        param_meta: dict[str, Any] | None = None,
        confirmed_params: dict[str, Any] | None = None,
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
            bbox=bbox,
            render_artifact_dir=render_artifact_dir,
            stated_dims=stated_dims,
            param_meta=param_meta,
            confirmed_params=confirmed_params,
        )

        # Commit the full snapshot to the project's git repo. The version
        # OWNS its geometry (issue #105): when the caller carried a
        # ``scad_source`` (the design loop's passing best candidate), it
        # is written to ``versions/{id}/design.scad`` and committed in the
        # SAME commit as ``params.json`` — one writer, one lock, one
        # commit, so a version row and its geometry can never diverge.
        repo_dir = Path(project["git_repo_path"])
        versions_dir = repo_dir / "versions"
        vdir = versions_dir / str(version_id)
        snapshot_path = vdir / PARAMS_FILENAME
        try:
            install_text_file_atomic(
                snapshot_path, json.dumps(params, indent=2, sort_keys=True) + "\n"
            )
            if scad_source is not None:
                from d33d.design_source import store_version_source

                store_version_source(repo_dir, version_id, scad_source)
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
            if scad_source is not None:
                from d33d.design_source import source_path_for_version

                source_path_for_version(repo_dir, version_id).unlink(missing_ok=True)
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
        full snapshot AND its per-version design source (issue #105 — a
        restore carries the geometry, not just the parameters); parent =
        current latest. No-op (409-style) when the target IS the current
        latest — dedupe, no spurious entry. The restored source rides on
        the new version's ``versions/{id}/design.scad`` file (written +
        committed inside ``_run_create`` with the params snapshot). A
        pre-#105 version without a source file restores params only (the
        honest case — a forward version of a design with no recorded
        geometry, the prompt renders the clean-slate wording)."""
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")

        # The target version's per-version source (None when the version
        # predates per-version sources or was created without geometry).
        project_row = self.conn.get_project(project_id)
        assert project_row is not None
        from d33d.design_source import source_path_for_version

        source_path = source_path_for_version(
            Path(project_row["git_repo_path"]), version_id
        )
        target_source = (
            source_path.read_text(encoding="utf-8")
            if source_path.is_file()
            else None
        )

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
                scad_source=target_source,
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

    # -- offer state (issue #250) ---------------------------------------------

    def get_pending_offer(self, project_id: int) -> dict[str, Any] | None:
        """The project's outstanding offer (``{"version_id": int,
        "param": str}``), or ``None`` (no pending offer — NULL or
        malformed row degrades to no offer, never a raise)."""
        row = self.conn.raw.execute(
            "SELECT pending_offer FROM projects WHERE id = ?", (project_id,)
        ).fetchone()
        if row is None:
            raise LookupError(f"project {project_id} not found")
        raw = row["pending_offer"]
        if not raw:
            return None
        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(doc, dict):
            return None
        version_id = doc.get("version_id")
        param = doc.get("param")
        if (
            not isinstance(version_id, int)
            or isinstance(version_id, bool)
            or not isinstance(param, str)
            or not param
        ):
            return None
        return {"version_id": version_id, "param": param}

    def set_pending_offer(self, project_id: int, offer: dict[str, Any] | None) -> None:
        """Persist (or clear, with ``None``) the project's pending offer.

        ``offer`` is ``{"version_id": int, "param": str}`` — the caller
        validates the param is a real assumed param of that version
        (``d33d.confirm_offer``); the writer stores the JSON as-is
        (``None`` → NULL, the cleared state).

        Shape guard (input validation at the boundary): a non-``None``
        offer must carry an ``int`` ``version_id`` (``bool`` excluded —
        ``isinstance(True, int)`` is true and a bool is never a version
        id) and a NON-EMPTY ``str`` ``param``; anything else is a
        contract violation and raises ``ValueError`` (never silently
        persisted — a malformed row would degrade to no offer on read
        anyway, but the raise is the early, loud failure)."""
        if offer is not None:
            version_id = offer.get("version_id")
            param = offer.get("param")
            if not isinstance(version_id, int) or isinstance(version_id, bool):
                raise ValueError(
                    f"pending offer's version_id must be an int, got {version_id!r}"
                )
            if not isinstance(param, str) or not param:
                raise ValueError(
                    f"pending offer's param must be a non-empty str, got {param!r}"
                )
        raw = json.dumps(offer) if offer else None
        self.conn.raw.execute(
            "UPDATE projects SET pending_offer = ? WHERE id = ?",
            (raw, project_id),
        )
        self.conn.commit()

    def record_confirmation(
        self, project_id: int, version_id: int, name: str, value: Any
    ) -> None:
        """Record ONE param as explicitly user-confirmed on a version
        (issue #250's accepted-offer write — the ONLY writer of
        ``confirmed_params``).

        ``value`` is the param's CURRENT value in the version's params
        snapshot (the caller verifies it matches the offered value before
        calling). The value is frozen into the set (``{name: value}``,
        merged into any existing set) so a later value change in a NEW
        version never inherits a stale confirmation — each version owns
        its own set, and the design-state promotion re-checks the value
        against the current params anyway (tolerance 1e-6)."""
        version = self.get_version(project_id, version_id)
        if version is None:
            raise LookupError(f"version {version_id} not found")
        raw_confirmed = version["confirmed_params"]
        # Shape guard: a malformed stored row (non-dict — a corrupted
        # JSON load or a hand-edited row) must never 500 the acceptance
        # flow: log a WARNING and start from ``{}`` (the new write then
        # overwrites the row with a well-formed set).
        if raw_confirmed is None:
            confirmed: dict[str, Any] = {}
        elif isinstance(raw_confirmed, dict):
            confirmed = dict(raw_confirmed)
        else:
            logger.warning(
                "confirmed_params on version %s is not a dict (%r) — "
                "starting from an empty set",
                version_id,
                raw_confirmed,
            )
            confirmed = {}
        confirmed[name] = value
        self.conn.raw.execute(
            "UPDATE versions SET confirmed_params = ?,"
            " updated_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE id = ?",
            (json.dumps(confirmed, sort_keys=True), version_id),
        )
        self.conn.commit()

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

    async def record_export(
        self, project_id: int, version_id: int
    ) -> dict[str, Any]:
        """Record that a 3MF of this version was handed to the operator
        (issue #126 — the persistent "exported" mark in the filmstrip).

        The mark belongs to the version actually downloaded, which is not
        necessarily the latest. Re-exporting the SAME version updates the
        mark to the LATEST export time (the mark answers "when did I last
        take this one out", and ``copy.history.exportedAt`` shows that
        time) — the last export wins, there is no counter and no first-
        export timestamp. The event is server-side state: a mark held in
        client state would not survive a reload, and the reload is the
        whole point of the mark.
        """
        target = self.get_version(project_id, version_id)
        if target is None:
            raise LookupError(f"version {version_id} not found")
        self.conn.raw.execute(
            "UPDATE versions SET exported_at = strftime('%Y-%m-%dT%H:%M:%fZ','now')"
            " WHERE project_id = ? AND id = ?",
            (project_id, version_id),
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
        bbox: tuple[float, float, float] | None = None,
        render_artifact_dir: str | None = None,
        stated_dims: dict[str, float] | None = None,
        param_meta: dict[str, Any] | None = None,
        confirmed_params: dict[str, Any] | None = None,
    ) -> int:
        fork = None
        if forked_from is not None:
            fork = [forked_from[0], forked_from[1]]
        # ``bbox``: the measured per-axis extents of the render that
        # produced this version (issue #137), JSON-encoded. ``None``
        # persists a NULL — an absent measurement ABSTAINS (issue #91's
        # precedent), it is never encoded as (0,0,0).
        bbox_json = json.dumps({"x": bbox[0], "y": bbox[1], "z": bbox[2]}) if bbox else None
        # ``stated_dims`` (issue #246): the dimension protocol's per-axis
        # confirmed set for the run that produced this version (e.g.
        # ``{"W": 60.0, "H": 80.0}`` — a partial statement counts for the
        # axes it states). Persisted at creation so the design-state
        # block can render stated axis rows without re-deriving from live
        # chat; ``None`` persists a NULL (an absent statement abstains —
        # never a fabricated axis row), exactly the bbox pattern above.
        stated_dims_json = (
            json.dumps({k: float(v) for k, v in stated_dims.items()})
            if stated_dims
            else None
        )
        # ``param_meta`` (issue #248): the model's per-parameter metadata
        # (``{name: {label?, unit?, axis?, reason?}}``) for the run that
        # produced this version — persisted at creation so the design-
        # state block can join the model's labels/units/axis/reason by
        # name without re-deriving from the loop result. ``None``
        # persists a NULL (a model that emitted no ``parameters`` array,
        # or every legacy row — an honest abstain, never a fabricated
        # label).
        param_meta_json = json.dumps(param_meta, sort_keys=True) if param_meta else None
        # ``confirmed_params`` (issue #250): the param-keyed set of values
        # the user explicitly confirmed on this version (``{name: value}``)
        # — written ONLY by the accepted-offer flow in
        # ``d33d.projects.post_chat`` (a new version's set stays NULL
        # until the user accepts THAT version's offer — confirmations
        # from a previous version are honoured by the offer selection
        # and by rule (b)'s value re-check, never re-persisted onto the
        # new row); ``None`` persists a NULL (no confirmed params — never
        # a fabricated ``{}``).
        confirmed_params_json = (
            json.dumps(confirmed_params, sort_keys=True) if confirmed_params else None
        )
        # ``render_artifact_dir``: the on-disk path of the per-render
        # directory whose model.stl produced this version (issue #163).
        # Stored as a plain path string (no JSON wrapping — it is a single
        # value, not a structure); ``None`` persists a NULL, never an
        # empty string (an absent render degrades to a clear error at the
        # 3MF route, never a guess at a directory).
        cur = self.conn.raw.execute(
            "INSERT INTO versions"
            " (project_id, params, name, created_by_message, parent,"
            "  restored_from, forked_from, thumbnail, bbox,"
            "  render_artifact_dir, stated_dims, param_meta,"
            "  confirmed_params)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                project_id,
                json.dumps(params, sort_keys=True),
                name,
                created_by_message,
                parent,
                restored_from,
                json.dumps(fork) if fork else None,
                thumbnail,
                bbox_json,
                render_artifact_dir,
                stated_dims_json,
                param_meta_json,
                confirmed_params_json,
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
        # ``bbox``: NULL (no measurement — pre-change rows, or a version
        # whose measurement was not obtainable) maps to ``None``, NEVER
        # to a zero triple (issue #91: a fabricated (0,0,0) once made a
        # gate unsatisfiable — an absent measurement abstains).
        raw_bbox = out.get("bbox")
        out["bbox"] = json.loads(raw_bbox) if raw_bbox else None
        # ``stated_dims`` (issue #246): NULL (pre-change rows, or a run
        # where the user stated no axes) maps to ``None`` — never a
        # fabricated ``{}`` that a consumer could misread (an absent
        # statement abstains).
        raw_stated = out.get("stated_dims")
        out["stated_dims"] = json.loads(raw_stated) if raw_stated else None
        # ``param_meta`` (issue #248): NULL (legacy rows, or a model that
        # emitted no parameters array) maps to ``None`` — never a
        # fabricated ``{}`` (an absent metadata set degrades the design
        # state block to identifier labels for every entry, honestly).
        raw_meta = out.get("param_meta")
        out["param_meta"] = json.loads(raw_meta) if raw_meta else None
        # ``confirmed_params`` (issue #250): NULL (no confirmed params —
        # every pre-change row) maps to ``None``, never a fabricated ``{}``
        # (an absent set means "nothing was confirmed" — the design-state
        # block reads that as: no rule (b) evidence).
        raw_confirmed = out.get("confirmed_params")
        out["confirmed_params"] = json.loads(raw_confirmed) if raw_confirmed else None
        # ``render_artifact_dir``: NULL (pre-#163 rows, or a version
        # created without a recorded render) maps to ``None``, NEVER to a
        # sentinel or empty string (issue #163: an absent render record
        # degrades honestly — the 3MF route 409s with a clear cause — it
        # is never a guessed directory, never mtime inference).
        out["render_artifact_dir"] = out.get("render_artifact_dir") or None
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
            # The last export of this version (issue #126) — ``None``
            # when it has never been exported (pre-change rows read back
            # as NULL; never a fabricated timestamp).
            "exported_at": version["exported_at"],
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
            bbox        TEXT,
            exported_at TEXT,
            render_artifact_dir TEXT,
            stated_dims TEXT,
            param_meta TEXT,
            confirmed_params TEXT,
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
    # ``versions.bbox``: the measured per-axis extents (JSON) of the render
    # that produced the version (issue #137). Nullable with no default —
    # SQLite rejects non-constant defaults on ALTER, and a NOT NULL column
    # would force a fabricated backfill. Pre-existing rows read back as
    # ``None`` (an absent measurement abstains; never a zero triple).
    _ensure_column(conn, "versions", "bbox", "TEXT")
    # ``versions.exported_at``: the last 3MF export of the version (issue
    # #126 — the persistent filmstrip mark). Nullable, no backfill: an
    # unexported version is NULL (never a fabricated timestamp).
    _ensure_column(conn, "versions", "exported_at", "TEXT")
    # ``versions.render_artifact_dir``: the on-disk path of the per-render
    # directory whose model.stl produced the version (issue #163). Nullable
    # with no default — SQLite rejects non-constant defaults on ALTER, and
    # a NOT NULL column would force a fabricated backfill. Pre-existing
    # rows read back as ``None`` (an absent render record degrades
    # honestly — never a guessed directory, never mtime inference).
    _ensure_column(conn, "versions", "render_artifact_dir", "TEXT")
    # ``versions.stated_dims``: the dimension protocol's per-axis
    # confirmed set (JSON) for the run that produced the version
    # (issue #246 — the design-state block's axis rows render ``stated``
    # from this persisted evidence, never from live chat). Nullable with
    # no default; pre-existing rows read back as ``None`` (an absent
    # statement abstains — never a fabricated axis row).
    _ensure_column(conn, "versions", "stated_dims", "TEXT")
    # ``versions.param_meta``: the model's per-parameter metadata (JSON)
    # for the run that produced the version (issue #248 — the design-state
    # block joins the model's labels/units/axis/reason by name from this
    # persisted evidence, never from live loop results). Nullable with no
    # default; pre-existing rows read back as ``None`` (an absent metadata
    # set abstains — never a fabricated label).
    _ensure_column(conn, "versions", "param_meta", "TEXT")
    # ``versions.confirmed_params`` (issue #250): the param-keyed set of
    # values the user EXPLICITLY confirmed by accepting the assistant's
    # offer on this version (``{name: value}`` — JSON, e.g. ``{"wall_
    # thickness": 3.0}``). Written ONLY by the accepted-offer flow in
    # ``d33d.projects.post_chat`` (explicit user confirmation of that
    # specific param — never by name inference); ``None`` persists a NULL
    # (no confirmed params, never a fabricated ``{}``). The design-state
    # block promotes a param ``assumed`` → ``stated`` from this set (the
    # rule (b) evidence, issue #250's operator decision).
    _ensure_column(conn, "versions", "confirmed_params", "TEXT")
    # ``projects.pending_offer`` (issue #250): the server-side state of
    # the assistant's OUTSTANDING offer — which assumed parameter was
    # offered for confirmation on which version (JSON ``{"version_id":
    # 7, "param": "wall_thickness"}`` — the offer is only live while that
    # version is the project's latest). Persisted so the acceptance check
    # in ``post_chat`` never relies on the client's chat history; a new
    # offer overwrites, an accepted/lapsed offer clears it (NULL).
    _ensure_column(conn, "projects", "pending_offer", "TEXT")


__all__ = [
    "MAIN_MARKER_PREFIX",
    "NAME_MAX_LEN",
    "ParamValue",
    "VersionConflictError",
    "VersionService",
    "clean_name",
    "derive_auto_name",
    "diff_params",
    "init_git_repo",
    "install_text_file_atomic",
    "migrate",
    "param_diff_name",
    "resolve_version_name",
    "sanitize_dimension_phrase",
    "validate_params",
]
