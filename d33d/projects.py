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
from pydantic import BaseModel

from d33d import db as db_mod

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}

_GIT_USER_EMAIL = "d33d@local"
_GIT_USER_NAME = "d33d"


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


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_projects_router() -> APIRouter:
    """Build and return the projects router.

    The router reads the shared ``db.Connection`` from ``request.app.state.conn``
    at request time (set by the lifespan in ``d33d.app.create_app``).
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])

    @router.post("", status_code=201)
    async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        # Create the DB row (generates git_repo_path)
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
        return {
            "id": project_id,
            "name": row["name"],
            "git_repo_path": row["git_repo_path"],
            "tags": row["tags"],
            "notes": row["notes"],
            "source_photo_path": row["source_photo_path"],
            "created_at": row["created_at"],
        }

    @router.get("")
    async def list_projects(request: Request) -> list[dict[str, Any]]:
        conn: db_mod.Connection = request.app.state.conn
        return conn.list_projects()

    @router.get("/{project_id}")
    async def get_project(request: Request, project_id: int) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return row

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
        return updated

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

        # Read the file bytes with size enforcement
        content = await file.read()
        if len(content) > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"file exceeds {MAX_UPLOAD_BYTES} byte limit",
            )

        # Determine a safe filename
        original_name = getattr(file, "filename", None) or "photo"
        safe_name = Path(original_name).name  # strip path components
        safe_name = (
            "".join(c for c in safe_name if c.isalnum() or c in "._-") or "photo"
        )
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

        # Commit the photo to the git repo
        try:
            commit_all(repo_path, f"photo: {safe_name}")
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
