"""Project CRUD + git repo + photo upload tests (issue #23, workstream task-b).

Covers:
- Project CRUD through the HTTP layer (create/get/list/update/delete)
  with per-project git repo lifecycle (real ``git init`` on disk).
- Photo upload bounds (20 MB, png/jpeg only), storage in the git repo,
  and DB update of ``source_photo_path``.
- Delete removes the on-disk git directory.

All tests non-slow: no Docker, no network, no port binding. Git is local-only
(``git init`` + ``git commit`` with per-repo identity).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.projects import ALLOWED_CONTENT_TYPES, MAX_UPLOAD_BYTES

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in repo_dir (local only)."""
    cmd = ["git", "-C", str(repo_dir), *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result


def _repo_for(app, project_id: int) -> Path:
    """The on-disk repo path (server-internal — the API masks it)."""
    for p in app.state.conn.list_projects():
        if p["id"] == project_id:
            return Path(p["git_repo_path"])
    raise AssertionError(f"project {project_id} not found")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under tmp_path."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance (the projects router is mounted by the
    factory itself — no manual wiring in tests).

    The ``git_repo_path`` for new projects is overridden to use ``tmp_path``
    so the test repos are cleaned up by pytest's tmp_path fixture.
    """
    import d33d.db as db_mod

    # Monkeypatch the default git path to use tmp_path
    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )

    yield app

    # Restore
    db_mod._default_git_path = original_default


# ---------------------------------------------------------------------------
# Project CRUD
# ---------------------------------------------------------------------------


def test_create_project_returns_201_and_git_repo(app_with_projects):
    """POST /api/projects creates a project row AND a git-initialized repo."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Test Project"})
        pid = r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        return r, repo_path

    r, repo_path = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    body = r.json()
    assert body["name"] == "Test Project"
    assert body["id"] > 0

    # Verify the git repo exists and is initialised (the path is read from the
    # DB within the lifespan — the API masks it in the response).
    assert (repo_path / ".git").exists(), "git repo should be initialised"

    # Verify git identity is set locally
    email = _git(repo_path, "config", "--get", "user.email").stdout.strip()
    name = _git(repo_path, "config", "--get", "user.name").stdout.strip()
    assert email == "d33d@local"
    assert name == "d33d"


def test_create_project_with_tags_and_notes(app_with_projects):
    """POST /api/projects with tags and notes stores them correctly."""

    async def _call(client):
        return await client.post(
            "/api/projects",
            json={"name": "Tagged", "tags": ["foo", "bar"], "notes": "hello"},
        )

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    body = r.json()
    assert body["tags"] == ["foo", "bar"]
    assert body["notes"] == "hello"


def test_list_projects(app_with_projects):
    """GET /api/projects returns all created projects."""

    async def _call(client):
        await client.post("/api/projects", json={"name": "P1"})
        await client.post("/api/projects", json={"name": "P2"})
        return await client.get("/api/projects")

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200
    body = r.json()
    assert len(body) == 2
    names = {p["name"] for p in body}
    assert "P1" in names
    assert "P2" in names


def test_get_project(app_with_projects):
    """GET /api/projects/{id} returns the project by ID."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Fetch Me"})
        pid = create_r.json()["id"]
        return await client.get(f"/api/projects/{pid}")

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200
    assert r.json()["name"] == "Fetch Me"


def test_get_project_not_found(app_with_projects):
    """GET /api/projects/{id} for a non-existent ID → 404."""

    async def _call(client):
        return await client.get("/api/projects/99999")

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


def test_update_project(app_with_projects):
    """PATCH /api/projects/{id} updates name/tags/notes."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Old"})
        pid = create_r.json()["id"]
        return await client.patch(
            f"/api/projects/{pid}",
            json={"name": "New", "tags": ["x"], "notes": "updated"},
        )

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200
    body = r.json()
    assert body["name"] == "New"
    assert body["tags"] == ["x"]
    assert body["notes"] == "updated"


def test_update_project_not_found(app_with_projects):
    """PATCH /api/projects/{id} for a non-existent ID → 404."""

    async def _call(client):
        return await client.patch("/api/projects/99999", json={"name": "X"})

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


def test_delete_project_removes_db_row_and_git_dir(app_with_projects):
    """DELETE /api/projects/{id} removes the DB row AND the on-disk git dir."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Doomed"})
        pid = create_r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        del_r = await client.delete(f"/api/projects/{pid}")
        return del_r, repo_path

    del_r, repo_path = _run_async(app_with_projects, _call)
    assert del_r.status_code == 204
    # The git directory should be gone
    assert not Path(repo_path).exists(), "git repo dir should be removed"


def test_delete_project_not_found(app_with_projects):
    """DELETE /api/projects/{id} for a non-existent ID → 404."""

    async def _call(client):
        return await client.delete("/api/projects/99999")

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


def test_delete_project_cascades_transcripts(app_with_projects, app_paths):
    """After DELETE, transcripts for the project are gone (FK cascade)."""
    from d33d import db as db_mod

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Cascade Test"})
        pid = create_r.json()["id"]
        # Add a transcript directly via the shared connection
        conn: db_mod.Connection = app_with_projects.state.conn
        conn.append_message(project_id=pid, role="user", content="hello")
        del_r = await client.delete(f"/api/projects/{pid}")
        # Verify transcript is gone
        transcript = conn.get_transcript(pid)
        return del_r, transcript

    del_r, transcript = _run_async(app_with_projects, _call)
    assert del_r.status_code == 204
    assert transcript == []


# ---------------------------------------------------------------------------
# Photo upload
# ---------------------------------------------------------------------------


def test_upload_photo_success(app_with_projects, tmp_path):
    """POST /api/projects/{id}/photos with a valid PNG stores the file
    in the git repo and updates source_photo_path."""
    # Create a minimal valid PNG (1x1 pixel)
    png_bytes = (
        b"\x89PNG\r\n\x1a\n"
        b"\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
        b"\x08\x02\x00\x00\x00\x90w\xfe\xed"
        b"\x00\x00\x00\x0cIDATx\x9cc\x00\x01\x00\x00\x05"
        b"\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB\xbfK"
    )

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Photo Project"})
        pid = create_r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)

        files = {"file": ("test.png", png_bytes, "image/png")}
        return await client.post(f"/api/projects/{pid}/photos", files=files), repo_path

    r, repo_path = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    body = r.json()
    assert body["size"] == len(png_bytes)
    # The file should be in the repo's photos/ dir
    photos_dir = repo_path / "photos"
    assert photos_dir.is_dir()
    photo_files = list(photos_dir.iterdir())
    assert len(photo_files) == 1
    assert photo_files[0].suffix == ".png"

    # The file should be committed (git log should show a commit)
    log = _git(repo_path, "log", "--oneline").stdout.strip()
    assert "photo:" in log


def test_upload_photo_updates_source_photo_path(app_with_projects):
    """After a successful upload, source_photo_path is updated in the DB."""

    async def _call(client):
        png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        create_r = await client.post("/api/projects", json={"name": "Path Test"})
        pid = create_r.json()["id"]
        files = {"file": ("img.png", png_bytes, "image/png")}
        await client.post(f"/api/projects/{pid}/photos", files=files)
        get_r = await client.get(f"/api/projects/{pid}")
        return get_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200
    body = r.json()
    assert body["source_photo_path"] is not None
    assert Path(body["source_photo_path"]).suffix == ".png"
    assert "photos" in body["source_photo_path"]


def test_upload_photo_rejects_wrong_content_type(app_with_projects):
    """A non-image content type (e.g. text/plain) → 400."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Type Test"})
        pid = create_r.json()["id"]
        files = {"file": ("evil.txt", b"not an image", "text/plain")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 400
    assert "content type" in r.json()["detail"]


def test_upload_photo_rejects_gif(app_with_projects):
    """image/gif is not in the allowed set → 400."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Gif Test"})
        pid = create_r.json()["id"]
        files = {"file": ("anim.gif", b"GIF89a", "image/gif")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 400


def test_upload_photo_rejects_oversized_file(app_with_projects):
    """A file exceeding 20 MB → 413, and no partial file is left behind."""
    # Create a file that's just over the limit
    oversized = b"\x89PNG" + b"\x00" * (MAX_UPLOAD_BYTES + 100)

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Big Test"})
        pid = create_r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        files = {"file": ("big.png", oversized, "image/png")}
        r = await client.post(f"/api/projects/{pid}/photos", files=files)
        # Check no partial file left
        photos_dir = repo_path / "photos"
        remaining = list(photos_dir.iterdir()) if photos_dir.exists() else []
        return r, remaining

    r, remaining = _run_async(app_with_projects, _call)
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]
    # No partial file should remain
    assert remaining == [], f"partial file left behind: {remaining}"


def test_upload_photo_to_nonexistent_project(app_with_projects):
    """Upload to a non-existent project → 404."""

    async def _call(client):
        files = {"file": ("x.png", b"\x89PNG", "image/png")}
        return await client.post("/api/projects/99999/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


def test_upload_photo_accepts_jpeg(app_with_projects):
    """image/jpeg is in the allowed set → accepted."""
    # Minimal JPEG magic bytes
    jpeg_bytes = b"\xff\xd8\xff\xdb\x00\x43\x08" + b"\x00" * 10

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Jpeg Test"})
        pid = create_r.json()["id"]
        files = {"file": ("photo.jpg", jpeg_bytes, "image/jpeg")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    assert r.json()["size"] == len(jpeg_bytes)


# ---------------------------------------------------------------------------
# Upload bounds constants
# ---------------------------------------------------------------------------


def test_upload_photo_sanitizes_commit_message(app_with_projects):
    """A malicious filename (embedded newlines + shell metacharacters) must
    never reach the git commit message: the message stays a single line of
    safe alnum+``._-`` characters (regression test for the commit-message
    injection sink in the per-project repo's commit history)."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    evil_name = "a\n$(whoami);rm -rf / # " + """" ' `""" + ".png"

    async def _call(client):
        create_r = await client.post(
            "/api/projects", json={"name": "Commit Sanitize Test"}
        )
        pid = create_r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        files = {"file": (evil_name, png_bytes, "image/png")}
        r = await client.post(f"/api/projects/{pid}/photos", files=files)
        # Full commit message of the upload commit, raw body format.
        msg = _git(repo_path, "log", "-1", "--format=%B").stdout
        return r, msg

    r, msg = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    # The message body is exactly one line: the subject ("photo: …") plus
    # git's own trailing newline — no embedded newlines survived.
    body = msg.rstrip("\n")
    assert "\n" not in body, f"commit message contains a newline: {msg!r}"
    assert body.startswith("photo: ")
    subject = body[len("photo: ") :]
    # Only safe alnum + . _ - characters — newlines and shell metachars
    # ( $( ) ; ' " ` # ) were all stripped from the filename.
    assert all(c.isalnum() or c in "._-" for c in subject), (
        f"commit subject contains unsafe characters: {subject!r}"
    )
    assert "$(whoami)" not in msg
    assert "rm -rf" not in msg
    assert len(subject) <= 200


def test_max_upload_bytes_is_20mb():
    """The committed bound is 20 MB."""
    assert MAX_UPLOAD_BYTES == 20 * 1024 * 1024


def test_allowed_content_types():
    """The allowed set is exactly png + jpeg."""
    assert ALLOWED_CONTENT_TYPES == {"image/png", "image/jpeg"}
