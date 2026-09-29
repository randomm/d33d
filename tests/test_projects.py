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
import base64
import shutil as _shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.projects import (
    ALLOWED_CONTENT_TYPES,
    MAX_UPLOAD_BYTES,
    UNDECODABLE_PHOTO_DETAIL,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _valid_png_1x1() -> bytes:
    """A genuinely decodable 1x1 RGBA PNG (issue #299: the upload decode
    gate requires a real image — the old hand-crafted magic-byte literals
    were not decodable)."""
    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGPgSjnBAAAC6Q"
        "E3I8kx3AAAAABJRU5ErkJggg=="
    )


def _valid_jpeg_1x1() -> bytes:
    """A genuinely decodable 1x1 JPEG (issue #299: the upload decode
    gate requires a real image — the old magic-byte literal was not
    decodable)."""
    return base64.b64decode(
        "/9j/4AAQSkZJRgABAQAAAQABAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0aHBwg"
        "JC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/2wBDAQkJCQwLDBgNDRgyIRwhMjIyMjIyMjIyMjIy"
        "MjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjIyMjL/wAARCAABAAEDASIAAhEBAxEB/8QA"
        "HwAAAQUBAQEBAQEAAAAAAAAAAAECAwQFBgcICQoL/8QAtRAAAgEDAwIEAwUFBAQAAAF9AQIDAAQRBRIh"
        "MUEGE1FhByJxFDKBkaEII0KxwRVS0fAkM2JyggkKFhcYGRolJicoKSo0NTY3ODk6Q0RFRkdISUpTVFVW"
        "V1hZWmNkZWZnaGlqc3R1dnd4eXqDhIWGh4iJipKTlJWWl5iZmqKjpKWmp6ipqrKztLW2t7i5usLDxMXG"
        "x8jJytLT1NXW19jZ2uHi4+Tl5ufo6erx8vP09fb3+Pn6/8QAHwEAAwEBAQEBAQEBAQAAAAAAAAECAwQF"
        "BgcICQoL/8QAtREAAgECBAQDBAcFBAQAAQJ3AAECAxEEBSExBhJBUQdhcRMiMoEIFEKRobHBCSMzUvAV"
        "YnLRChYkNOEl8RcYGRomJygpKjU2Nzg5OkNERUZHSElKU1RVVldYWVpjZGVmZ2hpanN0dXZ3eHl6goOE"
        "hYaHiImKkpOUlZaXmJmaoqOkpaanqKmqsrO0tba3uLm6wsPExcbHyMnK0tPU1dbX2Nna4uPk5ebn6Onq"
        "8vP09fb3+Pn6/9oADAMBAAIRAxEAPwDgqKKK8M/VD//Z"
    )


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan.

    Re-enters the lifespan for a FRESH connection on every call (the
    versioning tests' ``run_async`` does the same — the lifespan closes
    the app's connection on teardown, and the next run reconnects)."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


def _svc(app: Any):
    """The app's version service on a LIVE connection (reconnect after a
    previous ``_run_async`` teardown closed the handle — the versioning
    tests' ``_reopen_conn`` pattern)."""
    svc = app.state.versions
    try:
        svc.conn.raw.execute("SELECT 1")
        return svc
    except Exception:  # noqa: BLE001 - any closed-handle state means "reconnect"
        import d33d.db as db_mod
        from d33d import versions as versions_mod

        fresh = db_mod.connect(app.state.db_path)
        versions_mod.migrate(fresh)
        app.state.conn = fresh
        fresh_svc = versions_mod.VersionService(fresh)
        app.state.versions = fresh_svc
        return fresh_svc


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


def test_get_project_storage_field_present(app_with_projects):
    """Issue #295: the project GET response ALWAYS carries a ``storage``
    field ``{"repo_present": bool, "photo_present": true|false|null}``.
    ``photo_present`` is JSON ``null`` for a photo-LESS project (never
    omitted, never ``false``), ``true`` when the stored file is on disk,
    and ``false`` when the path is set but the file is gone. The field is
    always present, never omitted (the wire contract pins this against
    the field-omission bug). Git invisibility still holds: the raw repo
    path is never in the response."""

    async def _call(client):
        # Case 1: fresh project — repo present, photo never uploaded
        # (photo_present must be null, NOT false).
        create_r = await client.post("/api/projects", json={"name": "S1"})
        pid1 = create_r.json()["id"]
        body1 = (await client.get(f"/api/projects/{pid1}")) .json()
        # Case 2: project with a photo — upload, then delete out-of-band
        # (photo_present must be false, NOT null).
        create_r2 = await client.post("/api/projects", json={"name": "S2"})
        pid2 = create_r2.json()["id"]
        png_bytes = _valid_png_1x1()
        files = {"file": ("p.png", png_bytes, "image/png")}
        await client.post(f"/api/projects/{pid2}/photos", files=files)
        row2 = app_with_projects.state.conn.get_project(pid2)
        assert row2 is not None
        photo_path = Path(row2["source_photo_path"])
        body2_present = (await client.get(f"/api/projects/{pid2}")) .json()
        photo_path.unlink()  # delete out-of-band (state 3: lost)
        body2_lost = (await client.get(f"/api/projects/{pid2}")) .json()
        # Case 3: project whose repo is gone (repo_present false).
        create_r3 = await client.post("/api/projects", json={"name": "S3"})
        pid3 = create_r3.json()["id"]
        _shutil.rmtree(_repo_for(app_with_projects, pid3))
        body3 = (await client.get(f"/api/projects/{pid3}")) .json()
        return body1, body2_present, body2_lost, body3

    body1, body2_present, body2_lost, body3 = _run_async(
        app_with_projects, _call
    )
    # The field is ALWAYS present (never omitted) — JSON null for the
    # photo-less project (the wire contract: {"repo_present": true,
    # "photo_present": null} vs {"repo_present": true, "photo_present": false}).
    for body in (body1, body2_present, body2_lost, body3):
        assert "storage" in body, "the storage field must always be present"
        assert set(body["storage"]) == {"repo_present", "photo_present"}
        assert "git_repo_path" not in body, "git invisibility"
    assert body1["storage"] == {"repo_present": True, "photo_present": None}
    assert body2_present["storage"] == {"repo_present": True, "photo_present": True}
    assert body2_lost["storage"] == {"repo_present": True, "photo_present": False}
    assert body3["storage"] == {"repo_present": False, "photo_present": None}


def test_list_projects_storage_signal_is_memoized_not_per_row_decode(
    app_with_projects, monkeypatch
):
    """Issue #299: the project list / GET endpoint's storage signal uses
    the CHEAP structural check (header open + verify, no full ``load()``),
    and the memo means the check runs at most ONCE per distinct
    (path, mtime, size) — not once per row per request. A regression that
    full-decoded per row would (a) call the full-decode helper more than
    once for the same file and (b) never hit the memo."""
    import d33d.design_loop_events as dle

    calls = {"n": 0}
    original = dle._photo_structurally_valid

    def _counting(path: str) -> bool:
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(dle, "_photo_structurally_valid", _counting)

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Memo"})
        pid = create_r.json()["id"]
        files = {"file": ("p.png", _valid_png_1x1(), "image/png")}
        await client.post(f"/api/projects/{pid}/photos", files=files)
        # Two requests, two rows — the memo means the structural check
        # for this file runs at most once total.
        await client.get("/api/projects")
        await client.get(f"/api/projects/{pid}")
        return calls["n"], len(dle._photo_signal_cache)

    n, memo_len = _run_async(app_with_projects, _call)
    # The memo means one structural check for the file across BOTH
    # requests (a per-row regression would show 2).
    assert n == 1, f"structural check ran {n}x for one file"
    # And the memo is populated (the (path, mtime, size) dict the helper
    # reads).
    assert memo_len >= 1


def test_storage_signal_memo_invalidates_on_in_place_overwrite(
    app_with_projects, monkeypatch
):
    """Issue #299, attack #3: the storage-signal memo is keyed on
    ``(path, mtime, size)`` — a file overwritten IN PLACE (same path) must
    recompute on the next read: the memo cannot observe the write itself,
    so the key change (``size`` and/or ``mtime``) is the invalidation this
    test pins. After the overwrite the helper must return the NEW file's
    verdict — never the stale cached ``True``."""
    import d33d.design_loop_events as dle

    # The memo is path-keyed — any tmp dir works; the conn's data_dir
    # only exists after the lifespan, so use the app's db-path parent
    # directly.
    data_dir = Path(app_with_projects.state.db_path).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    good = data_dir / "memo-overwrite.png"
    good.write_bytes(_valid_png_1x1())
    # Prime the memo with the GOOD file's verdict (the primed key: the
    # good file's path/mtime/size).
    assert dle.photo_storage_signal(str(good)) is True

    # Overwrite in place with the ticket's undecodable repro bytes — a
    # SIZE change (8 != the PNG's size) flips the memo key, so the stale
    # entry can never match the new key even if the mtime did not tick.
    good.write_bytes(b"fake-png")

    calls = {"n": 0}
    original = dle._photo_structurally_valid

    def _counting(path: str) -> bool:
        calls["n"] += 1
        return original(path)

    monkeypatch.setattr(dle, "_photo_structurally_valid", _counting)
    assert dle.photo_storage_signal(str(good)) is False
    assert calls["n"] == 1, "in-place overwrite was served from the stale memo"


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
    png_bytes = _valid_png_1x1()

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
        png_bytes = _valid_png_1x1()
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


def test_upload_photo_commit_serialized_by_shared_write_lock(app_with_projects, monkeypatch: pytest.MonkeyPatch):
    """(HIGH 2 regression) The photo upload's git commit must run under the
    shared version-write lock (``VersionService._with_project_lock``) so it
    serializes with the design-source PUT / version-create git writes on the
    same repo — concurrent unguarded committers collide on
    ``.git/index.lock`` (500).

    The test wraps the service's ``commit_all`` seam with a spy that spawns
    a waiter task on the SAME per-project lock while the commit is in
    flight; the waiter must time out (proof the lock is held across the
    commit call, i.e. the route is not bypassing the lock)."""
    import d33d.projects as projects_mod

    png_bytes = _valid_png_1x1()
    real_commit_all = projects_mod.commit_all

    calls: list[int] = []
    probe_results: list[str] = []

    def _spy_commit(repo_dir, message: str) -> None:
        # Get the service at call time (lifespan has run by now).
        svc = app_with_projects.state.versions
        if svc is None:
            real_commit_all(repo_dir, message)
            return
        # The route must hold BOTH the write lock and the project lock while
        # this commit runs. Probe directly: while the route holds the lock,
        # ``locked()`` is True. We are on the same task/loop as the route
        # (the sync commit runs inside the async request handler), so this
        # is a synchronous, deterministic check — no waiter races.
        pid_row = svc.conn.raw.execute(
            "SELECT id FROM projects ORDER BY id DESC LIMIT 1"
        ).fetchone()
        pid = int(pid_row[0]) if pid_row else -1
        proj_lock = svc._lock_for(pid)
        write_lock = svc._write_lock
        calls.append(pid)
        probe_results.append("write_locked" if write_lock.locked() else "write_free")
        probe_results.append("proj_locked" if proj_lock.locked() else "proj_free")
        real_commit_all(repo_dir, message)

    # The photo route calls commit_all defined in d33d.projects itself, so
    # monkeypatch the module attribute (the route's local reference to the
    # function is resolved at call time via the module's global scope).
    monkeypatch.setattr(projects_mod, "commit_all", _spy_commit)

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Lock Test"})
        pid = create_r.json()["id"]
        files = {"file": ("lock.png", png_bytes, "image/png")}
        r = await client.post(f"/api/projects/{pid}/photos", files=files)
        repo_path = _repo_for(app_with_projects, pid)
        return r, pid, repo_path

    r, pid, repo_path = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    # The upload's commit ran through the spy for the right project.
    assert calls and calls[-1] == pid
    # The commit itself happened (real commit_all was called last).
    log = _git(repo_path, "log", "--oneline").stdout
    assert "photo:" in log
    # Both locks must have been held while the commit ran.
    assert "write_locked" in probe_results, f"write lock not held: {probe_results}"
    assert "proj_locked" in probe_results, f"project lock not held: {probe_results}"


def test_upload_photo_accepts_jpeg(app_with_projects):
    """image/jpeg is in the allowed set → accepted (issue #299: a real,
    decodable JPEG — magic bytes alone would now 422)."""
    jpeg_bytes = _valid_jpeg_1x1()

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
    png_bytes = _valid_png_1x1()
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


# ---------------------------------------------------------------------------
# Issue #299 — the upload decode gate: an undecodable file (e.g. an 8-byte
# "fake-png" text blob with an image/png content type) is rejected with 422
# BEFORE any file write / git commit / DB update, and the stored extension
# follows the DETECTED format (Pillow's ``img.format``), not the declared
# content type. Check order is fixed: content type (400) → size (413) →
# decode gate (422) → write/commit/DB.
# ---------------------------------------------------------------------------


def test_upload_photo_rejects_undecodable_file_with_422(app_with_projects):
    """A non-decodable file (8-byte "fake-png" with image/png content type)
    → 422 with the copy.ts message verbatim, and NOTHING is written: no
    file in the repo's photos/ dir, no git commit, and
    ``source_photo_path`` stays unchanged (NULL for a fresh project).
    """

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Gate Test"})
        pid = create_r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        files = {"file": ("fake.png", b"fake-png", "image/png")}
        r = await client.post(f"/api/projects/{pid}/photos", files=files)
        get_r = await client.get(f"/api/projects/{pid}")
        photos_dir = repo_path / "photos"
        remaining = list(photos_dir.iterdir()) if photos_dir.exists() else []
        # A fresh repo has no commits; "no commit" is proven by the
        # photos/ dir being empty (the commit would have staged it).
        return r, get_r.json(), remaining

    r, body, remaining = _run_async(app_with_projects, _call)
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    # The 422 body is {"detail": "<copy text>"} — the SPA renders it verbatim.
    assert r.json()["detail"] == UNDECODABLE_PHOTO_DETAIL
    # Nothing was written to the repo's photos/ dir (and hence no commit).
    assert remaining == [], f"a file was written on a 422: {remaining}"
    # source_photo_path is unchanged (NULL for a fresh project).
    assert body["source_photo_path"] is None


def test_upload_photo_422_fires_after_size_check(app_with_projects):
    """>20 MB undecodable garbage → 413, NOT 422 (the size check fires
    first — the decode gate never sees the bytes). The existing
    ``test_upload_photo_rejects_oversized_file`` pins this; this test
    re-pins it against the decode-gate regression (a 422-before-413
    ordering would break the 20 MB contract)."""
    oversized = b"\x89PNG" + b"\x00" * (MAX_UPLOAD_BYTES + 100)

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Size First"})
        pid = create_r.json()["id"]
        files = {"file": ("big.png", oversized, "image/png")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 413, f"expected 413, got {r.status_code}: {r.text}"
    assert "exceeds" in r.json()["detail"]


def test_upload_photo_extension_follows_detected_format(app_with_projects):
    """Issue #299: the stored suffix follows the DETECTED format
    (Pillow's ``img.format``), never the declared content type.
    - A real PNG declared ``image/jpeg`` → stored as ``.png``.
    - A real JPEG declared ``image/png`` → stored as ``.jpg``.
    """

    async def _call(client):
        # Case 1: PNG bytes declared image/jpeg → .png on disk.
        c1 = await client.post("/api/projects", json={"name": "Mime1"})
        pid1 = c1.json()["id"]
        r1 = await client.post(
            f"/api/projects/{pid1}/photos",
            files={"file": ("x.jpg", _valid_png_1x1(), "image/jpeg")},
        )
        path1 = r1.json().get("source_photo_path")
        # Case 2: JPEG bytes declared image/png → .jpg on disk.
        c2 = await client.post("/api/projects", json={"name": "Mime2"})
        pid2 = c2.json()["id"]
        r2 = await client.post(
            f"/api/projects/{pid2}/photos",
            files={"file": ("x.png", _valid_jpeg_1x1(), "image/png")},
        )
        path2 = r2.json().get("source_photo_path")
        return r1, r2, path1, path2

    r1, r2, path1, path2 = _run_async(app_with_projects, _call)
    assert r1.status_code == 201, r1.text
    assert r2.status_code == 201, r2.text
    assert path1 is not None and path1.endswith(".png"), f"PNG bytes should be .png: {path1}"
    assert path2 is not None and path2.endswith(".jpg"), f"JPEG bytes should be .jpg: {path2}"


def test_upload_photo_422_on_truncated_png(app_with_projects):
    """A valid PNG header with a truncated body (magic bytes + IHDR only,
    no IDAT/IEND) is NOT decodable → 422 (the old hand-crafted test
    fixtures relied on this being accepted; issue #299 pins it as a
    rejection — the upload gate requires a fully decodable image).
    """
    truncated = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Trunc"})
        pid = create_r.json()["id"]
        files = {"file": ("trunc.png", truncated, "image/png")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == UNDECODABLE_PHOTO_DETAIL


def test_upload_photo_rejects_decompression_bomb(app_with_projects):
    """A PNG header claiming 10000×1 is rejected at header-parse time
    (before a full decode) → 422 (issue #299: ``MAX_PHOTO_SIDE_PX`` = 8192,
    ``Image.MAX_IMAGE_PIXELS`` is set — a tiny file with a giant pixel
    declaration must never allocate gigabytes)."""
    # A minimal valid PNG with IHDR declaring 10000×1 (45 bytes total —
    # signature + IHDR + IEND, no IDAT data). Pillow's Image.open reads
    # only the header and reports size (10000, 1); the validator rejects
    # it at the dimension check, never calling verify()/load().
    bomb = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAJxAAAAABCAIAAAD3vCNcAAAAAElFTkSuQmCC"
    )

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Bomb"})
        pid = create_r.json()["id"]
        files = {"file": ("bomb.png", bomb, "image/png")}
        return await client.post(f"/api/projects/{pid}/photos", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422, f"expected 422, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == UNDECODABLE_PHOTO_DETAIL


# ---------------------------------------------------------------------------
# Issue #300 — the offer-acceptance pre-route reads the same single-source
# block the Brief shows (``state_block_for_version`` — rule (a) axis
# promotion, rule (b) confirmed, the measurement comparison): a "yes" is
# accepted only when the offered param row is EXACTLY ``assumed`` there.
# A param the Brief renders stated / measured / disagrees is never
# confirmed — the offer lapses and normal routing applies.
# ---------------------------------------------------------------------------

_TRAY_PARAMS = {
    "tray_width": 60.0,
    "tray_depth": 45.0,
    "tray_height": 20.0,
    "wall_thickness": 3.0,
}

_TRAY_META = {
    "tray_width": {"label": "Tray width", "unit": "mm", "axis": "W"},
    "tray_depth": {"label": "Tray depth", "unit": "mm", "axis": "D"},
    "tray_height": {"label": "Tray height", "unit": "mm", "axis": "H"},
    "wall_thickness": {"label": "Wall thickness", "unit": "mm"},
}


def _drive_event_source(app, client, pid):
    """Drive a registered event source to its terminal frame (the same
    pattern the versioning tests' ``_drive_chat`` uses)."""

    async def _drive():
        source = app.state.event_sources.get(pid)
        frames = []
        assert source is not None, "event source not registered before 202"
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    return _drive()


def test_offer_acceptance_rejects_stated_axis_param_via_single_source_block(
    app_with_projects,
):
    """Issue #300 (acceptance path): a pending offer for ``tray_depth`` on
    a version whose ``stated_dims`` = {W: 60, D: 45, H: 20} — the Brief
    (``state_block_for_version``) renders ``tray_depth`` ``stated`` via
    rule (a) — must NOT be confirmed on a clean "yes": the pre-route
    reads the same single-source block the Brief shows, the row is not
    ``assumed``, so the offer lapses and the message routes normally
    (no design run — the flag is released, no version, no confirmation
    recorded).

    The pre-fix code read ``state_block_from_params`` here (stated-
    blind — the row read ``assumed``) and recorded the confirmation;
    this test pins the single-source read."""
    app_with_projects.state.answer_question = None

    async def _call(client):
        svc = app_with_projects.state.versions
        r = await client.post("/api/projects", json={"name": "tray"})
        pid = r.json()["id"]
        v = await svc.create_version(
            pid,
            dict(_TRAY_PARAMS),
            param_meta=dict(_TRAY_META),
            stated_dims={"W": 60.0, "D": 45.0, "H": 20.0},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "tray_depth"})
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "yes"}
        )
        return r2.status_code, pid

    status, _pid = _run_async(app_with_projects, _call)
    assert status == 202, status  # routed normally (not an acceptance)
    svc = _svc(app_with_projects)
    # The offer was NOT consumed: it is still pending.
    offer = svc.get_pending_offer(_pid)
    assert offer is not None and offer["param"] == "tray_depth"
    # And no confirmation was recorded for the stated param.
    latest = svc.latest_version(_pid)
    assert latest["confirmed_params"] in (None, {})


def test_offer_acceptance_accepts_still_assumed_param_via_same_block(
    app_with_projects,
):
    """Issue #300 (acceptance path, the control case): the SAME tray
    version with a pending offer for ``wall_thickness`` — a param with
    no axis, no stated evidence — renders ``assumed`` in the same
    single-source block the Brief shows, so a clean "yes" IS accepted:
    the value is recorded as user-confirmed, the pending offer is
    cleared, and the acknowledgement frame rides the chat stream.

    The pre-fix code accepted this too (via ``state_block_from_params``);
    the test pins that switching to the single-source block did not
    over-narrow the acceptance (a genuinely assumed param is still
    confirmable)."""
    app_with_projects.state.answer_question = None

    async def _call(client):
        svc = app_with_projects.state.versions
        r = await client.post("/api/projects", json={"name": "tray"})
        pid = r.json()["id"]
        v = await svc.create_version(
            pid,
            dict(_TRAY_PARAMS),
            param_meta=dict(_TRAY_META),
            stated_dims={"W": 60.0, "D": 45.0, "H": 20.0},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "yes"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, _pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    svc = _svc(app_with_projects)
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer"
    assert done[0]["message"] == "Got it — Wall thickness stays 3.0\u202fmm.", done
    assert done[0].get("confirm_ack_label") == "Wall thickness"
    assert done[0].get("confirm_ack_value") == "3.0\u202fmm"
    # The confirmation was recorded; the offer was consumed.
    latest = svc.latest_version(_pid)
    assert latest["confirmed_params"] == {"wall_thickness": 3.0}
    assert svc.get_pending_offer(_pid) is None
