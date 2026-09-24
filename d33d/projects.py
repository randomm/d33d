"""Project CRUD router + per-project git repo + photo upload (issue #23, workstream task-b).

Endpoints (registered on the FastAPI app from ``d33d.app.create_app``):

- ``POST   /api/projects``               — create a project row + git-init the repo
- ``GET    /api/projects``               — list all projects
- ``GET    /api/projects/{id}``          — get a single project
- ``PATCH  /api/projects/{id}``          — update name / tags / notes
- ``DELETE /api/projects/{id}``          — delete the row AND the on-disk git dir
- ``POST   /api/projects/{id}/photos``   — multipart upload (png/jpeg, ≤ 20 MB)

The git repo is the content spine: it is initialised at ``git_repo_path`` on
project creation and committed to on each photo upload. The DB row only
carries the path.

Git operations are local-only (``git init``, ``git config``, ``git add``,
``git commit``). No remote, no network. The identity is set per-repo
(``user.email`` / ``user.name``) so tests work on machines without a global
git identity.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from d33d import db as db_mod
from d33d.design_loop_events import (
    latest_version_stated_dims,
    photo_data_uri,
    run_design_loop_with_events,
)
from d33d.dimension_protocol import stated_axes_from_message, stated_dims_from_message

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB — bounded read chunk size
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}

_GIT_USER_EMAIL = "d33d@local"
_GIT_USER_NAME = "d33d"

# Commit-message messages are stored in the per-project git repo's history,
# which downstream consumers (git log parsing, shell tooling, template
# interpolation) treat as data. Sanitize the message text with a strict
# safe-character filter so user-supplied filenames can never inject newlines
# or shell metacharacters into the commit history.
_MAX_COMMIT_MESSAGE_LEN = 200


def _sanitize_commit_message(text: str) -> str:
    """Reduce ``text`` to a single line of safe alnum+``._-`` characters.

    Mirrors the filename-sanitization filter (defensively stricter than the
    caller needs): any character outside the safe set — including newlines,
    shell metacharacters, and other punctuation — is dropped, and the result
    is capped at ``_MAX_COMMIT_MESSAGE_LEN`` characters.
    """
    safe = "".join(c for c in text if c.isalnum() or c in "._-")
    return safe[:_MAX_COMMIT_MESSAGE_LEN]


# ---------------------------------------------------------------------------
# Git helpers (local only, no network)
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
    """``git init`` + set local identity. Idempotent (skips if .git exists)."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _git(repo_dir, "init", "-q")
        _git(repo_dir, "config", "user.email", _GIT_USER_EMAIL)
        _git(repo_dir, "config", "user.name", _GIT_USER_NAME)


def commit_all(repo_dir: Path, message: str) -> None:
    """Stage everything and commit. No-op if nothing to commit."""
    _git(repo_dir, "add", "-A")
    # Check if there is anything to commit
    status = _git(repo_dir, "status", "--porcelain")
    if not status.stdout.strip():
        return
    _git(repo_dir, "commit", "-q", "-m", message)


def remove_repo(repo_dir: Path) -> None:
    """Remove the repo directory entirely. No-op if missing."""
    if repo_dir.is_dir():
        shutil.rmtree(repo_dir)


# ---------------------------------------------------------------------------
# Pydantic models for request bodies
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    tags: list[str] | None = None
    notes: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


class ChatRequest(BaseModel):
    """Body of ``POST /api/projects/{id}/chat`` (issue #54).

    ``message`` is the user's chat text (the design-loop request text).
    ``stated_dims`` is an optional (W, D, H) triple in mm; when present it
    is passed to the design loop verbatim. When absent (the SPA never
    sends the field), the server resolves dimensions itself — it does NOT
    depend on the client supplying it (ticket #91):

    1. dimensions stated in the user's own message, via the existing
       ``d33d.dimension_protocol`` extraction (``stated_dims_from_message``);
    2. else the latest version's W/D/H (``latest_version_stated_dims`` —
       the same fallback the finalize seam uses);
    3. else ``None`` — "no dimensions known": the loop's bbox gate
       ABSTAINS (``Score.bbox_abstained``) instead of hard-failing on a
       fabricated ``(0.0, 0.0, 0.0)`` target (the bug this ticket fixes).

    ``chat_history`` is the list of prior user messages (the SPA sends the
    last 10); absent → empty tuple.
    """

    message: str
    stated_dims: list[float] | None = None
    chat_history: list[str] | None = None

    @field_validator("message")
    @classmethod
    def _message_must_be_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must be non-empty and not whitespace-only")
        return v

    @field_validator("stated_dims")
    @classmethod
    def _stated_dims_must_be_3_finite(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return None
        if len(v) != 3:
            raise ValueError("stated_dims must be a 3-element array [W, D, H]")
        import math

        for x in v:
            if not isinstance(x, (int, float)) or not math.isfinite(float(x)):
                raise ValueError("stated_dims elements must be finite numbers")
        return [float(x) for x in v]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _public_project_row(row: dict[str, Any]) -> dict[str, Any]:
    """A project row with the raw git-repo path removed (git invisibility
    — the on-disk path names a git repo and is never exposed in an API
    response)."""
    out = dict(row)
    out.pop("git_repo_path", None)
    return out


def create_projects_router() -> APIRouter:
    """Build and return the projects router.

    The router reads the shared ``db.Connection`` from ``request.app.state.conn``
    at request time (set by the lifespan in ``d33d.app.create_app``).
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.post("", status_code=201)
    async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        # Update the DB row (generates git_repo_path, sets activity)
        project_id = conn.create_project(
            name=body.name,
            tags=body.tags or [],
            notes=body.notes,
        )
        row = conn.get_project(project_id)
        assert row is not None
        repo_path = Path(row["git_repo_path"])
        # Initialise the git repo on disk
        try:
            init_git_repo(repo_path)
        except RuntimeError as e:
            # Rollback: delete the DB row since git init failed
            conn.delete_project(project_id)
            raise HTTPException(status_code=500, detail=f"git init failed: {e}")
        # Git invisibility: the raw on-disk repo path is never exposed.
        return _public_project_row(row)

    @router.get("/{project_id}")
    async def get_project(request: Request, project_id: int) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _public_project_row(row)

    @router.get("")
    async def list_projects(request: Request) -> list[dict[str, Any]]:
        conn: db_mod.Connection = request.app.state.conn
        out = []
        for r in conn.list_projects():
            out.append(_public_project_row(r))
        return out

    @router.patch("/{project_id}")
    async def update_project(
        request: Request, project_id: int, body: ProjectUpdate
    ) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        conn.update_project(
            project_id,
            name=body.name,
            tags=body.tags,
            notes=body.notes,
        )
        updated = conn.get_project(project_id)
        assert updated is not None
        return _public_project_row(updated)

    @router.delete("/{project_id}", status_code=204)
    async def delete_project(request: Request, project_id: int) -> None:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        # Remove the on-disk git repo (the DB layer does NOT do this)
        repo_path = Path(row["git_repo_path"])
        remove_repo(repo_path)
        # Delete the DB row (cascades transcripts via FK)
        conn.delete_project(project_id)

    @router.post("/{project_id}/chat", status_code=202)
    async def post_chat(request: Request, project_id: int, body: ChatRequest) -> dict[str, Any]:
        """Wire a chat message to the design loop (issue #54).

        Validates the project exists (404), checks the per-project in-flight
        flag (409), registers the event source synchronously (so the SSE
        stream does not terminate on "no active stream"), and starts the
        background design-loop task. Returns 202 immediately — the design
        loop runs in a background asyncio task and streams progress/token
        frames via ``GET /api/stream/{project_id}``.

        The in-flight flag (``app.state.design_loop_inflight``) is set
        synchronously before the 202 response and cleared in the
        background task's ``finally`` on ALL exit paths (pass, exhausted,
        exception) — a leaked flag would 409 every subsequent chat for
        that project forever.
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        app = request.app
        inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
        if inflight is None:
            inflight = set()
            app.state.design_loop_inflight = inflight
        if project_id in inflight:
            raise HTTPException(status_code=409, detail="a design loop is already in flight")

        # Resolve the loop's stated dimensions (ticket #91), in strict
        # precedence order — the SPA never sends ``stated_dims`` (it posts
        # only ``message`` + ``chat_history``), so a client-absent value
        # must not silently become an unsatisfiable (0,0,0) gate target:
        #   1. the user's own message, via the existing dimension_protocol
        #      extraction (``stated_dims_from_message`` reuses
        #      ``_extract_stated`` — never a new parser);
        #   2. else the latest version's W/D/H (``latest_version_stated_dims``
        #      — the same fallback the finalize seam uses); a version with
        #      null/zero/partial W/D/H yields None, not a zero triple;
        #   3. else None — the loop's bbox gate ABSTAINS (recorded
        #      distinctly in ``Score.bbox_abstained``); it never receives
        #      (0.0, 0.0, 0.0) from this route.
        chat_history = tuple(body.chat_history or ())

        # The per-axis stated evidence persisted on the version the loop
        # pass creates (issue #246): the protocol's per-axis extraction of
        # the user's own words (``stated_axes_from_message`` reuses the
        # same ``_extract_stated`` pipeline as the full-triple extraction
        # above — partial statements count for the axes they state; the
        # body's explicit stated_dims, when a client sends one, is the
        # protocol's highest-priority source and ranks identically). The
        # design-state block reads this PERSISTED set on the version row
        # (never re-derives from live chat), so the loop adapter hands it
        # to ``create_version`` alongside the measured bbox. A statement
        # that names no axis persists ``{}`` → NULL (abstain, never a
        # fabricated axis row).
        per_axis_stated: dict[str, float] = {}
        if body.stated_dims is not None:
            _w, _d, _h = body.stated_dims
            if _w:
                per_axis_stated["W"] = float(_w)
            if _d:
                per_axis_stated["D"] = float(_d)
            if _h:
                per_axis_stated["H"] = float(_h)
        else:
            per_axis_stated = stated_axes_from_message(body.message, chat_history)

        if body.stated_dims is not None:
            stated: tuple[float, float, float] | None = tuple(
                float(d) for d in body.stated_dims
            )
        else:
            stated = stated_dims_from_message(body.message, chat_history)
            if stated is None:
                stated = latest_version_stated_dims(
                    app.state.versions, project_id
                )

        # Photo: read the project's stored photo NOW (synchronously, before
        # the 202 response) — the background task runs via asyncio and the
        # DB may be closed by the time the loop starts (a deleted project
        # or a closed connection). The photo is captured here as a data URI
        # (MIME from the extension; missing file → the fixed 1x1
        # transparent-PNG constant).
        photo = photo_data_uri(row.get("source_photo_path"))

        # Register the event source synchronously BEFORE the 202 response
        # (else the client stream terminates on "no active stream" — see
        # d33d/streaming.py's contract). The adapter is a plain async
        # generator (not a coroutine): ``event_sources`` maps
        # project_id -> AsyncIterator of (event, data) tuples.
        #
        # The SSE endpoint (GET /api/stream/{project_id}) is the SOLE
        # driver of this generator — a single async generator cannot be
        # driven by two concurrent ``async for`` consumers (CPython raises
        # ``RuntimeError: anext(): asynchronous generator is already
        # running`` on the second consumer's first ``__anext__``). The
        # inflight flag is set here (synchronously, before the 202
        # response) and cleared in the SSE endpoint's ``finally`` when the
        # generator is exhausted (or an SSE client disconnects).
        events = run_design_loop_with_events(
            app,
            project_id,
            user_message=body.message,
            stated_dims=stated,
            stated_axes=per_axis_stated,
            chat_history=chat_history,
            photo=photo,
            request_text=body.message,
        )
        app.state.event_sources[project_id] = events

        # Set the in-flight flag (synchronously, before the 202 response).
        # The SSE endpoint's ``finally`` clears it on ALL exit paths
        # (generator exhausted, client disconnect, exception).
        inflight.add(project_id)

        return {"status": "accepted"}

    @router.post("/{project_id}/photos", status_code=201)
    async def upload_photo(request: Request, project_id: int) -> dict[str, Any]:
        """Multipart photo upload (png/jpeg, ≤ 20 MB).

        Accepts a single file field (``file``) in the multipart body.
        The content type is validated against the allowed set, the file is
        written to the per-project git repo's ``photos/`` directory, and the
        ``source_photo_path`` DB column is updated.
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        # Parse the multipart body to extract the file
        content_type_header = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type_header:
            raise HTTPException(
                status_code=400,
                detail="expected multipart/form-data body",
            )

        try:
            form = await request.form()
        except (LookupError, ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"multipart parse error: {e}")

        file = form.get("file")
        if file is None:
            raise HTTPException(status_code=400, detail="missing 'file' field")

        # Validate content type
        file_content_type = getattr(file, "content_type", None) or ""
        if file_content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"content type {file_content_type!r} not allowed (must be image/png or image/jpeg)",
            )

        # Read the file bytes in bounded chunks; abort with 413 as soon as
        # the running total exceeds the cap (never buffers the whole
        # upload first — an unbounded read would defeat the limit).
        buf = bytearray()
        while True:
            chunk = await file.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_UPLOAD_BYTES:
                buf.clear()
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {MAX_UPLOAD_BYTES} byte limit",
                )
        content = bytes(buf)

        # Determine a safe filename
        original_name = getattr(file, "filename", None) or "photo"
        safe_name = Path(original_name).name  # strip path components
        safe_name = (
            "".join(c for c in safe_name if c.isalnum() or c in "._-") or "photo"
        )
        # Commit-message text is sanitized separately (stricter, capped) so
        # the repo's commit history can never carry newlines or shell
        # metacharacters derived from the user-supplied filename.
        commit_subject = _sanitize_commit_message(original_name) or "photo"
        # Ensure extension matches the declared type
        if file_content_type == "image/png":
            if "." in safe_name:
                safe_name = safe_name.rsplit(".", 1)[0] + ".png"
            else:
                safe_name = safe_name + ".png"
        else:
            if "." in safe_name:
                safe_name = safe_name.rsplit(".", 1)[0] + ".jpg"
            else:
                safe_name = safe_name + ".jpg"

        # Write to the repo's photos/ dir
        repo_path = Path(row["git_repo_path"])
        photos_dir = repo_path / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        dest = photos_dir / safe_name
        dest.write_bytes(content)
        size = len(content)

        # Commit the photo to the git repo, under the shared version-write
        # lock (d33d.versions.VersionService._with_project_lock) so EVERY
        # git write to this repo — design-source PUT, version create,
        # set-as-main, and this photo upload — is serialized; concurrent
        # committers would otherwise collide on ``.git/index.lock``.
        svc = getattr(request.app.state, "versions", None)
        try:
            if svc is not None:
                await svc._with_project_lock(project_id, lambda: commit_all(
                    repo_path, f"photo: {commit_subject}"
                ))
            else:  # pragma: no cover - the app lifespan always wires it
                commit_all(repo_path, f"photo: {commit_subject}")
        except RuntimeError as e:
            # Clean up the file but keep the repo consistent
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"git commit failed: {e}")

        # Persist the path in the DB
        stored_path = str(dest)
        conn.update_project(project_id, source_photo_path=stored_path)

        return {
            "id": project_id,
            "source_photo_path": stored_path,
            "size": size,
        }

    return router


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_UPLOAD_BYTES",
    "create_projects_router",
]
