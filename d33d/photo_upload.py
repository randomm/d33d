"""Reference-photo upload machinery (issue #299's decode gate lives here).

The ``POST /api/projects/{id}/photos`` route body, the upload bounds, and
the 422 ``detail`` string (a verbatim copy of ``web/src/copy.ts``'s
``photoUpload.undecodable`` — the SPA renders the 422 body's ``detail``
verbatim, so the two copies must never drift apart; the parity test in
``tests/test_projects.py`` pins the two-way agreement). The check order is
fixed: content type (400) → size (413) → decode gate (422) → write /
commit / DB — a 422 writes nothing, commits nothing, updates nothing.

``d33d.projects`` keeps only the ``@router`` wiring (the thin
``photo_upload_route(router)`` call). The module re-exports the two
design-state notice strings (``PHOTO_MISSING_NOTICE`` /
``SAVED_DESIGN_MISSING_REPLY``, from ``d33d.design_frames``) under their
historical names — the photo-upload route reads ``commit_all`` from
``d33d.project_git`` via its own module-level import.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from d33d import db as db_mod
from d33d.design_loop_events import validate_photo_bytes
from d33d.project_git import commit_all, sanitize_commit_message

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB — bounded read chunk size
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}

# Issue #299 — the 422 detail for an undecodable upload (a verbatim copy
# of web/src/copy.ts `photoUpload.undecodable`).
UNDECODABLE_PHOTO_DETAIL = (
    "That file isn't a readable PNG or JPEG image. Try exporting it again."
)

# Commit-message text is sanitized with ``d33d.project_git``'s strict
# safe-character filter (the one definition the photo-upload and
# version-write paths share — user-supplied filenames can never inject
# newlines or shell metacharacters into the repo's commit history).


async def _upload_photo(request: Request, project_id: int) -> dict[str, Any]:
    """Multipart photo upload (png/jpeg, ≤ 20 MB).

    Accepts a single file field (``file``) in the multipart body.
    The content type is validated against the allowed set, the file is
    written to the per-project git repo's ``photos/`` directory, and the
    ``source_photo_path`` DB column is updated.

    The commit runs through ``d33d.project_git.commit_all`` — imported at
    module level (the git primitives live in their own module, so the
    photo-upload route has no import cycle with ``d33d.projects`` at all).
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
            detail="expected multipart form-data body",
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

    # Issue #299 — the decode gate (AFTER the 20 MB size check, BEFORE
    # any file write / git commit / DB update): the bytes must decode
    # as a real PNG or JPEG image with sane dimensions. A failure
    # writes nothing — no file, no commit, no DB update (same contract
    # as the 413 path above). The stored extension follows the
    # DETECTED format (``img.format``), never the declared content type
    # (a PNG declared ``image/jpeg`` must land on disk as ``.png`` —
    # otherwise the data-URI MIME would mislabel the bytes).
    try:
        suffix = validate_photo_bytes(content)
    except ValueError:
        raise HTTPException(status_code=422, detail=UNDECODABLE_PHOTO_DETAIL)

    # Determine a safe filename
    original_name = getattr(file, "filename", None) or "photo"
    safe_name = Path(original_name).name  # strip path components
    safe_name = (
        "".join(c for c in safe_name if c.isalnum() or c in "._-") or "photo"
    )
    # Commit-message text is sanitized separately (stricter, capped) so
    # the repo's commit history can never carry newlines or shell
    # metacharacters derived from the user-supplied filename.
    commit_subject = sanitize_commit_message(original_name) or "photo"
    # Ensure the extension matches the DETECTED format (issue #299 —
    # the decode gate above already guarantees a PNG or JPEG)
    if "." in safe_name:
        safe_name = safe_name.rsplit(".", 1)[0] + suffix
    else:
        safe_name = safe_name + suffix

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


def photo_upload_route(router: APIRouter) -> None:
    """Attach the ``POST {prefix}/{project_id}/photos`` route to
    ``router`` (the thin wiring call ``d33d.projects`` makes — the body
    lives in :func:`_upload_photo` so the router file stays under the
    500-line split threshold)."""

    @router.post("/{project_id}/photos", status_code=201)
    async def upload_photo(request: Request, project_id: int) -> dict[str, Any]:
        return await _upload_photo(request, project_id)


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_UPLOAD_BYTES",
    "UNDECODABLE_PHOTO_DETAIL",
    "photo_upload_route",
]
