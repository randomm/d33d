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
import json
import shutil as _shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d import fill_recut
from d33d.app import create_app
from d33d.photo_upload import (
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


def test_get_project_undecodable_photo_no_warning(app_with_projects, caplog):
    """Issue #356, operator decision #3: the GET / list storage signal
    does NOT warn. The storage endpoint is POLLED (every SPA render of
    the project list or header calls it), so a WARNING there would spam
    the live log — the operator-visible signal for a lost/undecodable
    photo lives on the CHAT and FINALIZE read sites instead. A project
    whose stored photo is present but undecodable must still report
    ``photo_present: false`` (the cheap structural check, unchanged
    behaviour), and a GET must capture ZERO log records from the d33d
    loggers for the undecodable photo."""
    import logging as _logging

    import d33d.design_loop_events as dle

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "U1"})
        pid = r.json()["id"]
        files = {"file": ("p.png", _valid_png_1x1(), "image/png")}
        await client.post(f"/api/projects/{pid}/photos", files=files)
        row = app_with_projects.state.conn.get_project(pid)
        assert row is not None
        photo_path = Path(row["source_photo_path"])
        # Corrupt the stored photo in place (the ticket's repro bytes —
        # the file's on-disk target of source_photo_path).
        photo_path.write_bytes(b"fake-png")
        # Flush any memo entry for the GOOD bytes (keyed on path/mtime/size
        # — the overwrite changes the key, but flush to be safe).
        dle._photo_signal_cache.clear()
        # Mark the start of the storage read — the lifespan's startup
        # records (repo scan, model pre-flight) precede this point and are
        # not the GET path's doing.
        caplog.clear()
        body = (await client.get(f"/api/projects/{pid}")).json()
        return body["storage"]

    caplog.set_level(_logging.INFO)
    storage = _run_async(app_with_projects, _call)
    # Unchanged behaviour: the undecodable file reads as NOT present.
    assert storage == {"repo_present": True, "photo_present": False}, storage
    # No record from any d33d logger for the undecodable photo — the
    # GET/storage path is silent by design (the operator's signal lives on
    # the chat and finalize read sites, which are not polled).
    d33d_records = [
        r for r in caplog.get_records("call") if r.name.startswith("d33d.")
    ]
    assert d33d_records == [], (
        f"the GET/storage path must not log for an undecodable photo, got: "
        f"{[r.getMessage() for r in d33d_records]}"
    )


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
    import d33d.photo_upload as photo_upload_mod

    png_bytes = _valid_png_1x1()
    real_commit_all = photo_upload_mod.commit_all

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

    # The photo route calls ``d33d.photo_upload.commit_all`` — imported at
    # module level from ``d33d.project_git`` — so patch the attribute on
    # ``d33d.photo_upload`` (the module global the route reads); patching
    # ``d33d.project_git.commit_all`` would miss the binding.
    monkeypatch.setattr(photo_upload_mod, "commit_all", _spy_commit)

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


# ---------------------------------------------------------------------------
# Issue #332 (sub-issue 3) — the fill-and-recut boundary pre-route + the
# unsettled-part guard (chat seam).
#
# The fill-and-recut pre-route fires when ALL of issue #332's operator
# decision (a)–(e) hold: the project has an assumed/settled part (a), the
# message asks to RESIZE or MOVE an existing feature (b), the noun is in
# the closed feature-noun set (c), the noun is NOT a token of the design's
# own current params/labels (d), and the message has no add/create verb
# (e). "yes"/"Yes, do that" on the pending offer runs the design loop with
# the explicit fill-and-recut instruction; "no"/"Leave it" clears it.
# The unsettled-part guard replies with the settle-first copy and never
# runs the design loop.
# ---------------------------------------------------------------------------

_SETTLED_PART_PARAMS = {
    "part_width": 60.0,
    "part_depth": 45.0,
    "part_height": 20.0,
}


def _set_part_columns(
    app: Any,
    pid: int,
    *,
    unit_status: str,
    scale: float = 1.0,
    hole_count: int | None = None,
):
    """Set the project's part columns (a part the user brought, units
    ``unit_status``) directly on the DB — the same UPDATE the #325
    settle path runs, minus the settle call itself.

    ``hole_count`` (issue #351): when given, also writes the ``part_report``
    JSON with ``{"hole_count": hole_count}`` — the stored import-time hole
    fact the fill-and-recut gate reads (the #351 new tests set it
    explicitly; default ``None`` leaves the report NULL, i.e. the
    legacy-row unknown case, which keeps the offer)."""
    conn = app.state.conn
    report = json.dumps({"hole_count": hole_count}) if hole_count is not None else None
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status=?, part_scale=?, part_report=? "
        "WHERE id=?",
        (unit_status, scale, report, pid),
    )
    conn.commit()


def _release_inflight(app: Any, pid: int):
    """Release the per-project in-flight design-loop flag (the test's
    ``async for`` drain of the event source does not run the SSE
    endpoint's ``finally`` — the flag is the route's state, not the
    source's, so consecutive chat turns on the same project 409 on a
    leaked flag; the SSE endpoint's ``finally`` is the production
    release point, and the test releases it directly between turns).
    """
    inflight = getattr(app.state, "design_loop_inflight", None)
    if inflight is not None:
        inflight.discard(pid)


def _insert_version_direct(app: Any, pid: int, params: dict, param_meta: dict | None = None):
    """Insert a version row directly (avoids the git repo commit — the
    pre-route tests only need the params/labels for the discriminator)."""
    import json

    conn = app.state.conn
    meta_json = json.dumps(param_meta) if param_meta else None
    conn.raw.execute(
        "INSERT INTO versions (project_id, name, params, param_meta) "
        "VALUES (?, ?, ?, ?)",
        (pid, "v1", json.dumps(params), meta_json),
    )
    conn.commit()


def test_fill_recut_trigger_no_hole_report_no_offer(app_with_projects):
    """Issue #351 — the stored import-time hole fact gates the offer:
    a settled part whose ``part_report`` carries an explicit
    ``hole_count: 0`` (a plain box, no hole) + "make the hole 10 mm"
    → NO fill-recut offer is stored, the done frame is the honest
    no-feature reply (NOT the boundary sentence), NO ``fill_recut_offer``
    flag on the done frame, and NO design loop (``run_loop`` is False —
    the loop is not wired on the test app, so a fresh_offer would leave
    the offer stored, which the assertion below forbids). The holey
    twin of the same request (``hole_count: 1``) still gets the offer,
    and a legacy NULL report keeps the offer (unknown → today's
    behaviour, operator decision 2)."""

    async def _call(client):
        # No-hole part (hole_count explicitly 0 — the plain-box case).
        r = await client.post("/api/projects", json={"name": "Plain Box"})
        pid = r.json()["id"]
        _set_part_columns(
            app_with_projects, pid, unit_status="settled", hole_count=0
        )
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 10 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        # Holey part (hole_count 1) with the SAME request — the offer
        # fires exactly as today (the gate passes).
        r3 = await client.post("/api/projects", json={"name": "Holey Part"})
        holey_pid = r3.json()["id"]
        _set_part_columns(
            app_with_projects, holey_pid, unit_status="settled", hole_count=1
        )
        r4 = await client.post(
            f"/api/projects/{holey_pid}/chat",
            json={"message": "make the hole 10 mm"},
        )
        holey_frames = await _drive_event_source(app_with_projects, client, holey_pid)
        return (
            r2.status_code,
            pid,
            frames,
            r4.status_code,
            holey_pid,
            holey_frames,
        )

    status, pid, frames, holey_status, holey_pid, holey_frames = _run_async(
        app_with_projects, _call
    )
    assert status == 202, status
    assert holey_status == 202, holey_status
    svc = _svc(app_with_projects)
    # The no-hole part: the honest reply, NO offer stored, NO flag.
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "I don't see a hole on the part you brought" in msg, msg
    assert "That hole came with your file" not in msg, msg
    assert not done[0].get("fill_recut_offer"), done
    assert svc.get_pending_offer(pid) is None
    # The holey twin: the boundary offer exactly as today — stored,
    # flagged, boundary sentence with Ø10 mm.
    holey_done = [d for e, d in holey_frames if e == "done"]
    assert holey_done, f"no done frame: {holey_frames}"
    assert "That hole came with your file" in holey_done[0]["message"], holey_done
    assert "Ø10 mm" in holey_done[0]["message"], holey_done
    assert holey_done[0].get("fill_recut_offer") is True, holey_done
    offer = svc.get_pending_offer(holey_pid)
    assert offer is not None
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert offer["size"] == 10.0, offer


def test_fill_recut_trigger_no_hole_reply_carries_triggering_noun_chat(
    app_with_projects,
):
    """Issue #351 fix — a BORE trigger on a ``hole_count: 0`` part gets
    the honest no-hole reply with the TRIGGERING noun ("I don't see a
    bore…"), not the hardcoded "hole" (the reply formats from the
    trigger's own noun — chat route)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Bore Box"})
        pid = r.json()["id"]
        _set_part_columns(
            app_with_projects, pid, unit_status="settled", hole_count=0
        )
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the bore 10 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    svc = _svc(app_with_projects)
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    msg = done[0]["message"]
    assert "I don't see a bore on the part you brought" in msg, msg
    assert "I don't see a hole" not in msg, msg
    assert "That bore came with your file" not in msg, msg
    assert not done[0].get("fill_recut_offer"), done
    assert svc.get_pending_offer(pid) is None


def test_fill_recut_trigger_no_hole_reply_carries_triggering_noun_region(
    app_with_projects,
):
    """Issue #351 fix — a BORE trigger on a ``hole_count: 0`` part gets
    the honest no-hole reply with the TRIGGERING noun ("I don't see a
    bore…") on the REGION-EDIT route too (the reply formats from the
    trigger's own noun on both routes — parity with the chat route)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Bore Box Region"})
        pid = r.json()["id"]
        _set_part_columns(
            app_with_projects, pid, unit_status="settled", hole_count=0
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_parity_region_edit_body(
                "make the bore 10 mm",
                face_normal=(0.0, 1.0, 0.0),
            ),
        )
        frames = await _parity_run_loop(app_with_projects, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    svc = _svc(app_with_projects)
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    msg = done[0]["message"]
    assert "I don't see a bore on the part you brought" in msg, msg
    assert "I don't see a hole" not in msg, msg
    assert not done[0].get("fill_recut_offer"), done
    assert svc.get_pending_offer(pid) is None


def test_fill_recut_offer_fires_for_annulus_import(app_with_projects):
    """Issue #351 — route-level: a project whose part_report carries the
    ``hole_count`` from a real watertight annulus import (a real drilled
    through-bore: 0 boundary loops but genus 1 → ``hole_count == 1`` —
    exactly the case the old gaps-only count read as 0 and refused) +
    "make the hole 10 mm" → the offer FIRES (pending offer stored,
    boundary sentence with Ø10 mm, ``fill_recut_offer: True`` on the done
    frame). The count is set via the helper from the annulus's computed
    import value — the fill-recut gate reads the stored fact, never
    re-parsing the mesh."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Annulus"})
        pid = r.json()["id"]
        # The annulus import's stored hole fact (watertight, genus 1):
        # 0 open gaps + 1 closed through-hole → 1.
        _set_part_columns(
            app_with_projects, pid, unit_status="settled", hole_count=1
        )
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 10 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "That hole came with your file" in msg, msg
    assert "Ø10 mm" in msg, msg
    assert done[0].get("fill_recut_offer") is True, done
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert offer["size"] == 10.0, offer


def test_fill_recut_trigger_hole_with_dimension(app_with_projects):
    """(a)–(e) all hold: 'make the big hole 38 mm' on a project with a
    settled part and no version of its own → the boundary sentence
    (the spec's hole template, Ø38 mm) as a kind:'answer' done frame, a
    fill-recut offer recorded server-side, NO design run."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Imported Part"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the big hole 38 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "That hole came with your file" in msg, msg
    assert "Ø38 mm" in msg, msg
    assert "fill it, then cut a Ø38 mm one on the same axis" in msg, msg
    # The done frame carries the offer flag (the SPA renders the
    # [Yes, do that] / [Leave it] buttons from it — fresh offer only).
    assert done[0].get("fill_recut_offer") is True, done
    # The offer was recorded server-side with the kind discriminator.
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert offer["size"] == 38.0, offer


def test_fill_recut_decline_done_frame_has_no_offer_flag(app_with_projects):
    """PR #339 fix round (real bug, chat route): a CLEAN DECLINE of the
    pending fill-recut offer ("Leave it" → FILL_RECUT_DECLINE_REPLY) must NOT
    re-emit the ``fill_recut_offer`` flag on the done frame — the SPA
    renders the [Yes, do that] / [Leave it] buttons from that field, so
    a re-emitted flag re-offers an offer that was just declined. (The
    same bug was already fixed on the region-edit seam in b0edf88; the
    chat route's ``post_chat`` registered ``fill_recut_offer=True`` for
    EVERY handled ``fill_recut_turn`` result, including the decline.)
    The companion fresh-trigger test pins the True case."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Decline No Flag"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "Leave it"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # The quiet decline acknowledgement is the reply…
    assert "Understood" in done[0].get("message", ""), done
    # …and the offer flag is ABSENT (the buttons must not re-render for
    # an offer that no longer exists).
    assert not done[0].get("fill_recut_offer"), done
    # The offer was cleared.
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None


def test_fill_recut_trigger_own_param_noun_not_triggered(app_with_projects):
    """Operator decision (d): the design's OWN SCAD already owns a slot
    (a param named slot_width, label 'Slot width' — the token 'slot' is in
    the design's own feature names), so 'make the slot 5 mm' is a resize
    of the user's own feature — NOT the imported mesh — and routes
    normally (no offer, no boundary reply; the design loop would run).
    The loop is stubbed out (no run_design_loop wiring on the test app),
    so the loop call must NOT be attempted — the pre-route must NOT fire.
    We assert the offer is NOT recorded and the reply is NOT the boundary
    sentence (the frame is the loop's, or the loop setup error — either
    way, not the fill-recut offer)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Own Slot"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        _insert_version_direct(
            app_with_projects,
            pid,
            {"slot_width": 6.0, "slot_length": 20.0},
            {"slot_width": {"label": "Slot width"}, "slot_length": {"label": "Slot length"}},
        )
        # The loop is not wired (test app has no run_design_loop) — the
        # pre-route must NOT fire (the noun 'slot' is in the design's own
        # tokens). Whatever frame comes back, it must NOT be the
        # fill-recut boundary sentence, and no fill-recut offer may be
        # recorded.
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the slot 5 mm"}
        )
        frames = []
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # No fill-recut offer was recorded.
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is None, f"unexpected offer: {offer}"
    # The reply must NOT be the boundary sentence (the design loop's
    # error frame or a no-op — the pre-route did NOT fire).
    done = [d for e, d in frames if e == "done"]
    for d in done:
        assert "came with your file" not in d.get("message", ""), d


def test_fill_recut_yes_runs_loop_with_instruction(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
):
    """'yes' on the pending fill-recut offer: the offer is cleared and
    the design loop runs with the explicit fill-and-recut instruction
    appended to the request text (the loop's own import-aware prompt
    already teaches the fill-then-cut move — the instruction makes the
    accepted turn's intent explicit). The loop is stubbed via a spy on
    ``run_design_loop_with_events`` (the test app has no production loop)."""
    import d33d.chat_loop as chat_loop_mod

    _loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        _loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Yes Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # First turn: trigger the offer.
        await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
        )
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for _ in source:
                pass
        _release_inflight(app_with_projects, pid)
        svc = app_with_projects.state.versions
        offer = svc.get_pending_offer(pid)
        assert offer is not None and offer["kind"] == "fill_recut", offer
        # Second turn: accept.
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "yes"}
        )
        frames = []
        source2 = app_with_projects.state.event_sources.get(pid)
        if source2 is not None:
            async for event, data in source2:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was called (the fake loop's frames are on the event source)
    # — the request text carries the fill-and-recut instruction.
    assert _loop_calls, f"the fake loop was not called; frames: {frames}"
    captured = _loop_calls[0]
    rt = captured["request_text"]
    assert "Fill-and-recut:" in rt, rt
    assert "hole" in rt, rt
    assert "38" in rt, rt
    # The offer was cleared (consumed).
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None


def test_fill_recut_yes_setup_failure_keeps_offer(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
):
    """Issue #332 fix round: on the fill-recut "yes" path the pending offer
    must NOT be lost if the design-loop setup raises. The offer is cleared
    only once the event source is registered (``chat_loop.run_design_loop``
    clears it there); when the setup fails, ``post_chat`` restores it so
    the user's acceptance survives the error. The request errors (500),
    the in-flight flag is released, and the fill_recut offer is still
    present."""
    import d33d.chat_loop as chat_loop_mod

    def _boom_loop(*args, **kwargs):
        raise RuntimeError("forced loop setup failure (test)")

    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _boom_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Lost Yes"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # First turn: trigger the offer.
        await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
        )
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for _ in source:
                pass
        _release_inflight(app_with_projects, pid)
        svc = app_with_projects.state.versions
        offer = svc.get_pending_offer(pid)
        assert offer is not None and offer["kind"] == "fill_recut", offer
        # Second turn: accept — the loop setup is forced to raise.
        # The ASGI transport surfaces the server exception as a 500 (FastAPI
        # catches it); if the transport re-raises instead, the test still
        # passes (the offer-restore logic ran before the raise either way).
        try:
            r2 = await client.post(
                f"/api/projects/{pid}/chat", json={"message": "yes"}
            )
            status = r2.status_code
        except RuntimeError:
            status = 500  # the RuntimeError propagated through the transport
        inflight = app_with_projects.state.design_loop_inflight
        return status, pid, inflight

    status, pid, inflight = _run_async(app_with_projects, _call)
    # The request errored (the setup exception propagated — no 202).
    assert status != 202, status
    # The in-flight flag was released (the project is not stuck — the next
    # attempt can start clean).
    assert pid not in inflight, "inflight flag leaked after setup failure"
    # The pending fill_recut offer survived the setup failure — the
    # acceptance was restored, not lost.
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None, "the accepted fill_recut offer was lost"
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert offer["size"] == 38.0, offer


def test_fill_recut_no_clears_offer(app_with_projects):
    """'no' on the pending fill-recut offer: the offer is cleared, a
    quiet acknowledgement frame is emitted, and NO design run happens."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # First turn: trigger the offer.
        await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
        )
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for _ in source:
                pass
        _release_inflight(app_with_projects, pid)
        # Second turn: decline.
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "no"}
        )
        frames = []
        source2 = app_with_projects.state.event_sources.get(pid)
        if source2 is not None:
            async for event, data in source2:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert "Understood" in done[0].get("message", ""), done
    # The offer was cleared.
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None


def test_fill_recut_turn_failure_releases_inflight_flag(app_with_projects, monkeypatch):
    """Fix-round MEDIUM: the fill-recut pre-route runs AFTER the in-flight
    claim but BEFORE an event source is registered. When
    ``fill_recut.fill_recut_turn`` raises, the flag must be released (the
    stream's ``finally`` never runs) — a leaked flag would 409 every
    follow-up chat for the project. The test forces the pre-route to
    raise (monkeypatch), asserts the flag is released, and asserts a
    follow-up chat is NOT 409-blocked."""
    import d33d.fill_recut as _fr

    def _boom_turn(*args, **kwargs):
        raise RuntimeError("forced fill-recut pre-route failure (test)")

    monkeypatch.setattr(_fr, "fill_recut_turn", _boom_turn)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Pre-route Boom"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # First turn: the fill-recut pre-route is forced to raise.
        # The ASGI transport re-raises server exceptions to the client
        # (no 500 wrapping), so the RuntimeError propagates to here.
        try:
            await client.post(
                f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
            )
            status = 202  # unreachable if the pre-route raised
        except RuntimeError:
            status = 500
        # The flag must NOT be held after the pre-route failure.
        inflight = app_with_projects.state.design_loop_inflight
        held = pid in inflight
        # The follow-up chat must NOT 409 — the flag must be released.
        # The monkeypatch is still in place, so the follow-up will also
        # raise (the pre-route re-fires) — but NOT with a 409.
        try:
            r3 = await client.post(
                f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
            )
            followup = r3.status_code
        except RuntimeError:
            followup = 500  # the forced-raise, NOT a 409
        return status, pid, held, followup

    status, _pid, held, followup = _run_async(app_with_projects, _call)
    # The first request errored (the pre-route exception propagated).
    assert status != 202, status
    # The in-flight flag was NOT held after the pre-route failure.
    assert not held, "inflight flag leaked after fill-recut pre-route failure"
    # The follow-up chat was NOT 409-blocked (the flag was released).
    assert followup != 409, f"follow-up chat was 409-blocked: {followup}"


def test_stored_axis_outside_unit_tolerance_dropped(app_with_projects):
    """Fix-round MEDIUM: a stored fill-recut offer whose axis is outside
    the unit-length range (1±NORMAL_LENGTH_TOLERANCE) reads back WITHOUT
    an axis. A 1e6-unit axis is clearly not a face normal — the wire
    validator and the reader must agree on rejecting it."""
    import json as _json

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Huge Axis"})
        pid = r.json()["id"]
        conn = app_with_projects.state.conn
        conn.raw.execute(
            "UPDATE projects SET pending_offer = ? WHERE id = ?",
            (
                _json.dumps(
                    {
                        "kind": "fill_recut",
                        "noun": "hole",
                        "size": 38.0,
                        "axis": [1e6, 0.0, 0.0],
                    }
                ),
                pid,
            ),
        )
        conn.commit()
        svc = _svc(app_with_projects)
        offer = svc.get_pending_offer(pid)
        return pid, offer

    _pid, offer = _run_async(app_with_projects, _call)
    assert offer is not None, "the offer must still be returned"
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert "axis" not in offer, f"axis outside unit tolerance leaked: {offer}"


def test_fill_recut_move_triggers_move_template(app_with_projects):
    """'move the hole left' triggers the move template (the point-at-the-
    spot copy), not the resize template."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Move Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "move the hole left"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    msg = done[0]["message"]
    assert "I can't move it directly" in msg, msg
    assert "Point at the spot" in msg, msg
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None and offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer


def test_fill_recut_add_verb_not_triggered(app_with_projects):
    """Operator decision (e): 'add a 38 mm hole' has an add verb — it's an
    ADD (allowed by rule 4), not a resize — so the pre-route does NOT
    fire (no offer, no boundary reply)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Add Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "add a 38 mm hole"}
        )
        frames = []
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # No fill-recut offer was recorded.
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None, "add verb must not record an offer"
    # No boundary reply (the loop would run — the test app has no loop,
    # so the frame is a loop error; either way, not the boundary copy).
    for e, d in frames:
        if e == "done":
            assert "came with your file" not in d.get("message", ""), d


def test_fill_recut_taller_is_add_not_boundary(app_with_projects):
    """Operator decision: 'make it 10 mm taller' has no closed-set noun —
    it's an ADD (the user is adding geometry onto the part), never a
    boundary case. The pre-route does NOT fire."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Taller Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make it 10 mm taller"}
        )
        frames = []
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, _ = _run_async(app_with_projects, _call)
    assert status == 202, status
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None, "taller is an add, not a boundary"


def test_fill_recut_no_part_not_triggered(app_with_projects):
    """Operator decision (a): a project with NO part — even when the
    message matches 'make the big hole 38 mm' — does NOT trigger the
    pre-route (the offer requires BOTH the phrasing AND a settled/assumed
    part)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Part"})
        pid = r.json()["id"]
        # No part columns set — a no-part project.
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the big hole 38 mm"}
        )
        frames = []
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2.status_code, pid, frames

    status, pid, _ = _run_async(app_with_projects, _call)
    assert status == 202, status
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None, "no-part project must not record an offer"


def test_unsettled_part_no_design_run(app_with_projects):
    """Unsettled part (part_unit_status not in {assumed, settled}): the
    reply is the deterministic 'settle the units first' message and NO
    design loop runs (no version, no render, inflight flag released)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Unsettled"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="unsettled")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert "Settle the units first" in done[0].get("message", ""), done
    # No version was created.
    svc = _svc(app_with_projects)
    assert svc.latest_version(pid) is None


def test_unsettled_part_assumed_status_skips_guard(app_with_projects):
    """Assumed part (unit_status='assumed') skips the unsettled guard —
    the pre-route continues to the fill-recut trigger (a settled/assumed
    part is a valid part for the boundary conversation)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Assumed"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="assumed")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 38 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # The fill-recut boundary reply (NOT the unsettled guard's reply).
    msg = done[0].get("message", "")
    assert "came with your file" in msg, msg
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None and offer["kind"] == "fill_recut", offer


# ---------------------------------------------------------------------------
# Issue #332 fix round: the decimal-token bug, the dropped-distance bug,
# and the copy.ts two-way parity pins.
# ---------------------------------------------------------------------------


def test_fill_recut_trigger_decimal_size_not_truncated() -> None:
    """A full decimal token ("38.5") is captured whole — the user's own
    number is never truncated by trailing punctuation (the pre-fix regex
    stopped the size capture at the first non-digit, rendering Ø38 for
    "make the hole 38.5 mm wide, please")."""
    t = fill_recut.fill_recut_trigger("make the hole 38.5 mm wide, please")
    assert t is not None
    assert t["noun"] == "hole"
    assert t["size"] == 38.5, t
    assert t["move"] is False


def test_fill_recut_trigger_integer_size_rendered_zero() -> None:
    """An integer number ("38") is captured and rendered as 38 (Ø38), the
    same value the pre-fix code produced for the integer case (no
    behaviour change for integers)."""
    t = fill_recut.fill_recut_trigger("make the big hole 38 mm")
    assert t is not None
    assert t["noun"] == "hole"
    assert t["size"] == 38.0, t
    # The rendered sentence (the deck's :g spelling — 38.0 renders as 38).
    s = fill_recut.boundary_sentence(t["noun"], t["size"])
    assert "Ø38 mm" in s, s
    assert "Ø38.0" not in s, s


def test_fill_recut_trigger_partial_number_not_parsed() -> None:
    """A trailing-partial number ("38." with no decimal digits) is NOT
    parsed as a size — no partial number is ever rendered (the pre-fix
    regex captured "38" from "38." and rendered Ø38 for a number the user
    did not state)."""
    t = fill_recut.fill_recut_trigger("make the hole 38.")
    assert t is None, t


def test_fill_recut_trigger_over_long_instruction_returns_none() -> None:
    """PR #339 fix round (item 4): an instruction over
    ``TRIGGER_MAX_INSTRUCTION_CHARS`` (500) returns ``None`` BEFORE any
    regex runs — a 10 KB unpunctuated blob containing the trigger
    phrasing bails out in O(1) instead of spinning the unbounded
    ``[^.!?]`` spans."""
    import time

    long_msg = "a" * (10 * 1024 - 17) + " make the hole 38 mm"
    assert len(long_msg) > 10 * 1024
    t0 = time.monotonic()
    t = fill_recut.fill_recut_trigger(long_msg)
    elapsed = time.monotonic() - t0
    assert t is None, t
    assert elapsed < 1.0, f"trigger took {elapsed:.3f}s on a 10 KB input"
    # The bound itself: at the limit the phrasing still triggers, one
    # char over it does not (the bound is a hard cap, not a suggestion).
    at_bound = "a" * (500 - len(" make the hole 38 mm")) + " make the hole 38 mm"
    assert len(at_bound) <= 500
    assert fill_recut.fill_recut_trigger(at_bound) is not None
    over_bound = at_bound + "a"
    assert len(over_bound) > 500
    assert fill_recut.fill_recut_trigger(over_bound) is None


def test_fill_recut_move_with_distance_keeps_number() -> None:
    """A move carrying a distance + direction keeps the user's number and
    direction (the pre-fix move template dropped the distance —
    "move the hole 10 mm left" got the point-at-the-spot copy and the 10
    was silently lost)."""
    t = fill_recut.fill_recut_trigger("move the hole 10 mm left")
    assert t is not None
    assert t["move"] is True
    assert t["move_distance"] == 10.0, t
    assert t["direction"] == "left", t
    s = fill_recut.boundary_sentence(
        t["noun"],
        t["size"],
        move=t["move"],
        move_distance_mm=t["move_distance"],
        move_direction=t["direction"],
    )
    assert "10 mm left of where it is now" in s, s
    assert "Point at the spot" not in s, s


def test_fill_recut_move_without_distance_uses_point_at_spot() -> None:
    """A move with NO distance keeps the point-at-the-spot copy (the
    pre-fix behaviour for the distanceless move — unchanged)."""
    t = fill_recut.fill_recut_trigger("move the hole left")
    assert t is not None
    assert t["move"] is True
    assert t["move_distance"] is None, t
    assert t["direction"] is None, t
    s = fill_recut.boundary_sentence(t["noun"], t["size"], move=t["move"])
    assert "Point at the spot" in s, s
    assert "mm" not in s, s


def test_fill_recut_offer_dict_carries_only_noun_and_size() -> None:
    """The server-side pending-offer dict stays the pre-fix shape
    (kind/noun/size — the #250 reader's contract); the distance +
    direction are reply-side only and are NOT persisted on the offer
    (the offer is the fill-and-recut instruction's input — the distance
    is the user's move phrasing, not a fill-and-recut parameter)."""
    t = fill_recut.fill_recut_trigger("move the hole 10 mm left")
    assert t is not None
    offer = {"kind": "fill_recut", "noun": t["noun"], "size": t["size"]}
    assert offer == {"kind": "fill_recut", "noun": "hole", "size": None}
    # The offer's instruction (an accepted move offer runs the loop):
    instr = fill_recut.fill_and_recut_instruction(offer)
    assert "hole" in instr
    assert "10" not in instr  # a move offer has no size to state


def test_fill_and_recut_instruction_zero_size_no_clause() -> None:
    """Issue #332 fix round: a zero size renders NO size clause (the guard
    is ``isinstance(size, (int, float)) and size > 0``, never truthiness —
    ``size: 0`` must not render " at 0 mm"). A positive size still renders
    the clause (unchanged)."""
    offer_zero = {"kind": "fill_recut", "noun": "hole", "size": 0}
    instr = fill_recut.fill_and_recut_instruction(offer_zero)
    assert " at " not in instr, instr
    # A positive size still renders the clause (unchanged).
    offer_ok = {"kind": "fill_recut", "noun": "hole", "size": 38.0}
    instr_ok = fill_recut.fill_and_recut_instruction(offer_ok)
    assert " at 38 mm" in instr_ok, instr_ok


def test_fill_and_recut_instruction_invalid_axis_no_clause() -> None:
    """Issue #338 final fix round: the instruction builder validates the
    axis with the SAME ``valid_axis`` the reader uses, so a non-finite
    (``nan``) or non-unit (``1e6``) axis — a malformed offer dict read
    straight from storage — yields NO axis clause rather than leaking a
    bad vector into the instruction. A valid unit axis keeps the clause."""
    import math

    nan_offer = {"kind": "fill_recut", "noun": "hole", "size": 38.0, "axis": [math.nan, 0.0, 0.0]}
    nan_instr = fill_recut.fill_and_recut_instruction(nan_offer)
    assert "axis " not in nan_instr, nan_instr

    nonunit_offer = {"kind": "fill_recut", "noun": "hole", "size": 38.0, "axis": [1e6, 0.0, 0.0]}
    nonunit_instr = fill_recut.fill_and_recut_instruction(nonunit_offer)
    assert "axis " not in nonunit_instr, nonunit_instr

    unit_offer = {"kind": "fill_recut", "noun": "hole", "size": 38.0, "axis": [0.0, 0.0, 1.0]}
    unit_instr = fill_recut.fill_and_recut_instruction(unit_offer)
    assert " (axis 0, 0, 1)" in unit_instr, unit_instr


def test_register_event_source_warns_on_overwrite(caplog) -> None:
    """Issue #338 final fix round: ``register_event_source`` pops any
    previously-registered source and LOGS A WARNING when one existed (a
    stale, still-live source about to be dropped); a first registration
    is silent. The single release point is still the SSE endpoint's
    ``finally`` — this only surfaces the overlap."""
    import types

    from d33d.chat_frames import register_event_source

    state = types.SimpleNamespace(event_sources={})
    app = types.SimpleNamespace(state=state)
    caplog.clear()

    # First registration: no previous source, silent.
    register_event_source(app, 1, "source-a")
    assert list(caplog.messages) == [], caplog.messages

    # Second registration: a previous source exists → warning.
    register_event_source(app, 1, "source-b")
    assert any(
        "dropping" in m or "previous event" in m for m in caplog.messages
    ), caplog.messages
    # The new source is the one now registered.
    assert state.event_sources[1] == "source-b"

    # A different project id (no prior source) stays silent.
    caplog.clear()
    register_event_source(app, 2, "source-c")
    assert list(caplog.messages) == [], caplog.messages


# ---------------------------------------------------------------------------
# copy.ts two-way parity pins (parse copy.ts, as the existing part-upload
# detail pins do — the overstated tripwire claim in the old
# UNSETTLED_PART_REPLY comment is corrected by these pins existing).
# ---------------------------------------------------------------------------


def _copy_ts_text() -> str:
    return (
        Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")


def test_unsettled_part_reply_equals_copy_ts() -> None:
    """``UNSETTLED_PART_REPLY`` equals ``copy.ts``'s
    ``partUnitsUnsettled`` exactly (parse copy.ts, as the existing
    part-upload detail pins do)."""
    import re

    m = re.search(r'partUnitsUnsettled =\s*"([^"]+)"', _copy_ts_text())
    assert m is not None, "copy.ts must define partUnitsUnsettled"
    assert fill_recut.UNSETTLED_PART_REPLY == m.group(1)


def test_unsettled_part_reply_in_fill_recut_all() -> None:
    """Item 4 (MEDIUM): ``UNSETTLED_PART_REPLY`` is in ``fill_recut.__all__``
    (``d33d.projects`` references it by name — a missing ``__all__`` entry
    would break ``from d33d.fill_recut import *`` consumers)."""
    assert "UNSETTLED_PART_REPLY" in fill_recut.__all__, (
        "UNSETTLED_PART_REPLY missing from fill_recut.__all__"
    )
    assert hasattr(fill_recut, "UNSETTLED_PART_REPLY")


def test_fill_recut_decline_reply_equals_copy_ts() -> None:
    """``FILL_RECUT_DECLINE_REPLY`` equals ``copy.ts``'s ``fillRecut.declined``
    exactly."""
    import re

    m = re.search(r'declined:\s*"([^"]+)"', _copy_ts_text())
    assert m is not None, "copy.ts must define fillRecut.declined"
    assert fill_recut.FILL_RECUT_DECLINE_REPLY == m.group(1)


def test_no_hole_reply_template_equals_copy_ts() -> None:
    """Issue #351 — ``part_holes.no_hole_reply`` renders ``copy.ts``'s
    ``fillRecut.noHole`` template with the noun substituted (the same
    two-way pin as the decline reply — a drift in either copy breaks the
    SPA/backend agreement). Both sides are rendered with a SENTINEL noun
    so a hardcoded noun in either side is caught, and again with a
    second noun so the slot, not the sentence, is what carries the
    difference."""
    import re

    from d33d.part_holes import no_hole_reply

    m = re.search(r'noHole:.*?`([^`]*)`', _copy_ts_text(), re.DOTALL)
    assert m is not None, "copy.ts must define fillRecut.noHole"
    ts_template = m.group(1)
    assert "${noun}" in ts_template, (
        f"copy.ts noHole must carry the ${{noun}} slot: {ts_template!r}"
    )
    for noun in ("__SENTINEL__", "bore"):
        ts_rendered = ts_template.replace("${noun}", noun)
        assert no_hole_reply(noun) == ts_rendered, (
            f"backend (noun={noun!r}): {no_hole_reply(noun)!r}\n"
            f"copy.ts (noun={noun!r}): {ts_rendered!r}"
        )


def test_no_hole_reply_uses_triggering_noun() -> None:
    """Issue #351 — the no-hole reply carries the TRIGGERING noun, not a
    hardcoded 'hole' (a bore/counterbore trigger answers 'I don't see a
    bore…', not 'I don't see a hole…')."""
    from d33d.part_holes import no_hole_reply

    assert "a bore" in no_hole_reply("bore")
    assert "a counterbore" in no_hole_reply("counterbore")
    assert "a hole" in no_hole_reply("hole")
    assert "bore" not in no_hole_reply("hole")


def test_part_has_hole_evidence_reads_stored_fact_only() -> None:
    """Issue #351 — the evidence reader is a pure reader of the STORED
    ``part_report.hole_count`` fact (no mesh work, no LLM, no
    re-parse): ``hole_count: 1`` → True (the offer is allowed);
    ``hole_count: 0`` → False (the honest no-hole reply); ``None``
    (no report / legacy row), missing key, and corrupt values (bool,
    string, negative, float) → None (unknown — keep the offer). A
    non-dict ``part`` (no part at all) is None."""
    from d33d.part_holes import part_has_hole_evidence

    assert part_has_hole_evidence({"report": {"hole_count": 1}}) is True
    assert part_has_hole_evidence({"report": {"hole_count": 2}}) is True
    assert part_has_hole_evidence({"report": {"hole_count": 0}}) is False
    # Unknown → None (the offer keeps firing, today's behaviour):
    assert part_has_hole_evidence(None) is None
    assert part_has_hole_evidence({"report": None}) is None
    assert part_has_hole_evidence({"report": {}}) is None
    assert part_has_hole_evidence({"report": {"watertight": True}}) is None
    # Corrupt / unexpected values degrade to unknown, never False:
    assert part_has_hole_evidence({"report": {"hole_count": True}}) is None
    assert part_has_hole_evidence({"report": {"hole_count": "1"}}) is None
    assert part_has_hole_evidence({"report": {"hole_count": 1.0}}) is None
    assert part_has_hole_evidence({"report": {"hole_count": -1}}) is None


def test_part_public_corrupt_report_degrades_to_no_report():
    """Issue #351 — a CORRUPT ``part_report`` blob (unparseable JSON)
    must degrade to ``report: None`` (UNKNOWN), never a raised 500:
    the hole-family gate reads the stored fact, and an unreadable blob
    is no evidence either way — the offer keeps firing, as today.
    ``part_public`` is the single reader of the column on the chat
    route (``post_chat`` line 461), so degrading the reader degrades
    every route identically. (The reader-side degradation lives in
    ``d33d.part_http._loads_or_none`` — issue #351; a corrupt report
    is no evidence either way and must never 500.)"""
    import d33d.part_http as part_http_mod

    row = {
        "id": 1,
        "part_filename": "part.stl",
        "part_format": "stl",
        "part_unit": "mm",
        "part_unit_status": "settled",
        "part_scale": 1.0,
        "part_report": '{"hole_count": ',
        "part_options": None,
    }
    part = part_http_mod.part_public(row)
    assert part is not None
    assert part["report"] is None, part
    from d33d.part_holes import part_has_hole_evidence

    assert part_has_hole_evidence(part) is None


def test_fill_recut_trigger_corrupt_report_degrades_to_offer(app_with_projects):
    """Issue #351 — a CORRUPT ``part_report`` blob (unparseable JSON) on
    a LIVE chat turn degrades the stored fact to UNKNOWN (``part_public``
    reduces it to ``report: None``), so the hole-family offer fires
    exactly as today — never the honest no-hole reply, never a 500.
    The trigger never re-parses the mesh: the stored fact is the sole
    input, and an unreadable blob is no evidence either way."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Corrupt Report"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # Overwrite with a corrupt JSON blob (the degradation case) — on
        # the LIVE connection (the lifespan's, not the closed one).
        conn = app_with_projects.state.conn
        conn.raw.execute(
            "UPDATE projects SET part_report=? WHERE id=?",
            ('{"hole_count": ', pid),
        )
        conn.commit()
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make the hole 10 mm"}
        )
        frames = await _drive_event_source(app_with_projects, client, pid)
        return r2.status_code, pid, frames

    status, pid, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # Unknown fact (report degraded to None) → today's behaviour: the
    # boundary sentence, the flag, and the stored offer — NOT the
    # honest no-hole reply.
    assert "That hole came with your file" in done[0]["message"], done
    assert "I don't see a hole on the part you brought" not in done[0]["message"], done
    assert done[0].get("fill_recut_offer") is True, done
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None and offer["kind"] == "fill_recut", offer


def test_fill_recut_templates_match_copy_ts_fill_recut() -> None:
    """Every fill-recut boundary template, rendered with fixed sample
    values, equals ``copy.ts``'s ``fillRecut`` deck function with the
    same values (parse copy.ts + the backend's own render — the two-way
    agreement the old comment claimed the design-contract tripwire
    pinned; the pin lives here, the #250/#260 way)."""
    import re

    copy_ts = _copy_ts_text()

    # The fillRecut deck block (scope the regex to it so `move` does not
    # match an unrelated `moved.` elsewhere in the file).
    fr_start = copy_ts.index("export const fillRecut = {")
    fr_end = copy_ts.index("} as const;", fr_start)
    seg = copy_ts[fr_start:fr_end]

    def _deck_fn(name: str) -> str:
        # Extract the template literal body of `fillRecut.<name>:` — the
        # backtick-quoted template after the arrow.
        m = re.search(name + r":[^(]*\([^)]*\)[^(]*=>\s*`([^`]*)`", seg)
        assert m is not None, f"copy.ts must define fillRecut.{name}"
        return m.group(1)

    def _render(ts: str, **kwargs) -> str:
        for k, v in kwargs.items():
            ts = ts.replace("${" + k + "}", v)
        return ts

    # The hole/bore diameter resize (dim 38).
    hole = _render(_deck_fn("holeDiameter"), noun="hole", dim="38")
    assert hole == fill_recut.boundary_sentence("hole", 38.0)

    # The other-noun resize (dim 5).
    slot = _render(_deck_fn("nounDimension"), noun="slot", dim="5")
    assert slot == fill_recut.boundary_sentence("slot", 5.0)

    # The point-at-the-spot move (no distance).
    move = _render(_deck_fn("move"), noun="hole")
    assert move == fill_recut.boundary_sentence("hole", None, move=True)

    # The move-with-distance (the new template — the dropped-distance bug
    # fix, pinned two-way).
    move_d = _render(
        _deck_fn("moveWithDistance"), noun="hole", distance="10", direction="left"
    )
    assert move_d == fill_recut.boundary_sentence(
        "hole", None, move=True, move_distance_mm=10.0, move_direction="left"
    )

    # The no-dimension ask.
    ask = _render(_deck_fn("noDimension"), noun="hole")
    assert ask == fill_recut.boundary_sentence("hole", None)


def test_design_contract_pins_fill_recut_deck_key() -> None:
    """The design-contract tripwire's key-list assertion includes the
    fill-recut deck surface (the copy-deck key-list pins ``fillRecut``
    and ``partUnitsUnsettled`` — a rename of the deck key trips the
    tripwire, the #325 way)."""
    src = (
        Path(__file__).parent.parent
        / "web" / "src" / "__tests__" / "design-contract.test.ts"
    ).read_text("utf-8")
    assert "fillRecut" in src
    assert "partUnitsUnsettled" in src


def _copy_ts_no_normal_value() -> str:
    """Extract the copy.ts ``fillRecut.noNormal`` string literal (the
    #338 parity pin reads it from the source — the web strings live in
    TypeScript, the Python test reads the literal; the literal is
    double-quoted, so a raw single-quoted substring read between the
    quotes is exact — the line is a plain string, no escapes)."""
    import re
    from pathlib import Path

    copy_src = (
        Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")
    m = re.search(r'noNormal:\s*\n\s*"([^"]*)"', copy_src)
    assert m is not None, "copy.ts fillRecut.noNormal literal not found"
    return m.group(1)


def test_design_contract_pins_fill_recut_no_normal_reply():
    """The backend's FILL_RECUT_NO_NORMAL_REPLY is BYTE-IDENTICAL to the
    copy.ts noNormal sentence (issue #338, decision 6 parity pin —
    full-string equality: a drift in EITHER copy silently breaks the
    SPA/backend copy agreement, so the two-way pin is exact)."""
    from d33d.fill_recut_region import FILL_RECUT_NO_NORMAL_REPLY

    no_normal = _copy_ts_no_normal_value()
    assert no_normal == FILL_RECUT_NO_NORMAL_REPLY, (
        f"copy.ts noNormal:\n  {no_normal!r}\n"
        f"FILL_RECUT_NO_NORMAL_REPLY:\n  {FILL_RECUT_NO_NORMAL_REPLY!r}"
    )


# ---------------------------------------------------------------------------
# Issue #338 — chat vs region-edit pre-route PARITY:
# projects.post_chat and app.create_region_edit must handle the four
# fill-recut outcomes identically (fresh offer / clean decline /
# acceptance / setup failure on acceptance) — one test per outcome, both
# routes driven in it, reusing this file's fixtures (app_with_projects,
# _set_part_columns, _run_async, _svc) and the region-edit body shape from
# tests/test_app.py (minimal valid 1x1 PNG + required wire fields).
# ---------------------------------------------------------------------------

#: A minimal valid 1x1 PNG, base64 (the same test PNG
#: ``tests/test_app.py``'s ``_region_edit_body`` uses).
_PARITY_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _parity_region_edit_body(instruction: str, face_normal: tuple[float, float, float] | None = None) -> dict[str, Any]:
    """A minimal valid region-edit wire body (the same shape
    ``tests/test_app.py``'s ``_region_edit_body`` posts — the parity test
    sends the face normal so the region route stores an axis, matching the
    chat route's offer shape for the fresh-trigger case)."""
    body: dict[str, Any] = {
        "module_ids": [],
        "view_id": "front",
        "marked_png_base64": _PARITY_TINY_PNG_BASE64,
        "point": {"x": 300.0, "y": 200.0},
        "instruction": instruction,
    }
    if face_normal is not None:
        body["face_normal"] = list(face_normal)
    return body


def _parity_run_loop(app, pid: int) -> list[tuple[str, Any]]:
    """Drive a registered event source to its terminal frame, returning
    the collected frames (a small local copy of this file's
    ``_drive_event_source`` pattern — the parity tests below compare the
    two routes' frames directly). Drives up to 2 frames (the ``progress``
    + ``done`` pair) so a source that emits both is fully consumed."""

    async def _run() -> list[tuple[str, Any]]:
        source = app.state.event_sources.get(pid)
        frames: list[tuple[str, Any]] = []
        if source is None:
            return frames
        for _ in range(2):  # progress + done
            try:
                event, data = await source.__anext__()
            except StopAsyncIteration:
                break
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    return _run()


def _parity_offer(app, pid: int) -> dict[str, Any] | None:
    """Read the project's pending offer, tolerating a closed DB handle
    (reconnect via :func:`_svc`'s pattern) — the parity tests read the
    offer after ``_run_async`` teardown closed the app's connection."""
    svc = _svc(app)
    return svc.get_pending_offer(pid)


def test_fill_recut_parity_no_feature_reply_no_loop(app_with_projects, monkeypatch):
    """Issue #351 — route parity for the honest no-feature reply: a hole
    request on a part with ``hole_count: 0`` gets the SAME honest reply
    on BOTH routes (``post_chat`` and the region-edit route), NO offer
    stored on either, NO ``fill_recut_offer`` flag, and NO loop call on
    either (the same four-outcome parity contract the other #338
    parity tests pin, now covering the no-feature outcome)."""
    import d33d.chat_loop as chat_loop_mod
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Parity No Hole Chat"})
        chat_pid = r.json()["id"]
        _set_part_columns(
            app_with_projects, chat_pid, unit_status="settled", hole_count=0
        )
        await client.post(
            f"/api/projects/{chat_pid}/chat",
            json={"message": "make the hole 10 mm"},
        )
        chat_frames = await _parity_run_loop(app_with_projects, chat_pid)
        _release_inflight(app_with_projects, chat_pid)
        r3 = await client.post("/api/projects", json={"name": "Parity No Hole Region"})
        region_pid = r3.json()["id"]
        _set_part_columns(
            app_with_projects, region_pid, unit_status="settled", hole_count=0
        )
        r5 = await client.post(
            f"/api/projects/{region_pid}/region-edits",
            json=_parity_region_edit_body(
                "make the hole 10 mm",
                face_normal=(0.0, 1.0, 0.0),
            ),
        )
        region_frames = await _parity_run_loop(app_with_projects, region_pid)
        return chat_pid, region_pid, chat_frames, region_frames, r5.status_code

    chat_pid, region_pid, chat_frames, region_frames, region_status = _run_async(
        app_with_projects, _call
    )
    assert region_status == 202, region_status
    # Neither route ran the loop.
    assert not calls, f"the design loop was called: {calls}"
    # Neither route stored an offer.
    assert _parity_offer(app_with_projects, chat_pid) is None
    assert _parity_offer(app_with_projects, region_pid) is None
    # The SAME honest no-feature reply on both routes, and NO offer flag
    # (the SPA must not render [Yes, do that] / [Leave it] for an offer
    # that was never stored).
    chat_done = [d for e, d in chat_frames if e == "done"]
    region_done = [d for e, d in region_frames if e == "done"]
    assert chat_done and region_done
    assert chat_done[0].get("kind") == "answer"
    assert region_done[0].get("kind") == "answer"
    assert chat_done[0]["message"] == region_done[0]["message"]
    assert "I don't see a hole on the part you brought" in chat_done[0]["message"]
    assert "That hole came with your file" not in chat_done[0]["message"]
    assert not chat_done[0].get("fill_recut_offer"), chat_done
    assert not region_done[0].get("fill_recut_offer"), region_done


def test_fill_recut_parity_fresh_offer_no_loop(app_with_projects, monkeypatch):
    """A fresh trigger stores the SAME offer on BOTH routes and runs NO
    loop: ``post_chat`` and the region-edit route agree on the stored
    offer, the boundary-sentence answer frame, and zero loop calls."""
    import d33d.chat_loop as chat_loop_mod
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        conn = app_with_projects.state.conn

        def _read_pending(pid: int) -> dict[str, Any] | None:
            """Read the project's raw pending-offer JSON (pre-normalization
            — the region route stores an extra ``axis`` the #250 reader
            does not surface, so the raw row is the parity comparison
            point)."""
            row = conn.raw.execute(
                "SELECT pending_offer FROM projects WHERE id = ?", (pid,)
            ).fetchone()
            raw = row["pending_offer"]
            if not raw:
                return None
            import json as _json_mod

            return _json_mod.loads(raw)

        # Chat route.
        r2 = await client.post("/api/projects", json={"name": "Parity Chat"})
        chat_pid = r2.json()["id"]
        _set_part_columns(app_with_projects, chat_pid, unit_status="settled")
        await client.post(
            f"/api/projects/{chat_pid}/chat",
            json={"message": "make the big hole 38 mm"},
        )
        chat_frames = await _parity_run_loop(app_with_projects, chat_pid)
        _release_inflight(app_with_projects, chat_pid)
        # Region-edit route (the SAME instruction).
        r3 = await client.post("/api/projects", json={"name": "Parity Region"})
        region_pid = r3.json()["id"]
        _set_part_columns(app_with_projects, region_pid, unit_status="settled")
        r5 = await client.post(
            f"/api/projects/{region_pid}/region-edits",
            json=_parity_region_edit_body(
                "make the big hole 38 mm",
                face_normal=(0.0, 1.0, 0.0),
            ),
        )
        region_frames = await _parity_run_loop(app_with_projects, region_pid)
        chat_offer_raw = _read_pending(chat_pid)
        region_offer_raw = _read_pending(region_pid)
        return chat_pid, region_pid, chat_frames, region_frames, r5.status_code, chat_offer_raw, region_offer_raw

    _chat_pid, _region_pid, chat_frames, region_frames, region_status, chat_offer, region_offer = _run_async(
        app_with_projects, _call
    )
    assert region_status == 202, region_status
    # The loop ran NEITHER route.
    assert not calls, f"the design loop was called: {calls}"
    # The SAME offer was stored server-side on both routes (compared on
    # the raw stored row — the region route's extra ``axis`` is offer
    # state the #250 reader does not surface, but both stores carry the
    # same discriminator / noun / size, and the region route adds the
    # axis the pick's normal carried).
    assert chat_offer is not None and region_offer is not None
    for key in ("kind", "noun", "size"):
        assert chat_offer[key] == region_offer[key] == {"kind": "fill_recut", "noun": "hole", "size": 38.0}[key], (chat_offer, region_offer)
    # The SAME boundary-sentence answer frame on both routes.
    chat_done = [d for e, d in chat_frames if e == "done"]
    region_done = [d for e, d in region_frames if e == "done"]
    assert chat_done and region_done
    assert chat_done[0].get("kind") == "answer"
    assert region_done[0].get("kind") == "answer"
    assert chat_done[0]["message"] == region_done[0]["message"]
    assert "Ø38 mm" in chat_done[0]["message"]


def test_fill_recut_parity_clean_decline_clears_offer(app_with_projects, monkeypatch):
    """A clean decline on a LIVE offer clears the offer on BOTH routes,
    replies with the SAME quiet acknowledgement, and runs NO loop on
    either."""
    import d33d.chat_loop as chat_loop_mod
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        # Chat route: seed the LIVE offer, decline.
        r = await client.post("/api/projects", json={"name": "Parity No Chat"})
        chat_pid = r.json()["id"]
        _set_part_columns(app_with_projects, chat_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            chat_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        await client.post(
            f"/api/projects/{chat_pid}/chat", json={"message": "no"}
        )
        chat_frames = await _parity_run_loop(app_with_projects, chat_pid)
        # Region-edit route: seed the SAME LIVE offer, decline.
        r2 = await client.post("/api/projects", json={"name": "Parity No Region"})
        region_pid = r2.json()["id"]
        _set_part_columns(app_with_projects, region_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            region_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        await client.post(
            f"/api/projects/{region_pid}/region-edits",
            json=_parity_region_edit_body("no"),
        )
        region_frames = await _parity_run_loop(app_with_projects, region_pid)
        return chat_pid, region_pid, chat_frames, region_frames

    chat_pid, region_pid, chat_frames, region_frames = _run_async(
        app_with_projects, _call
    )
    # Neither route ran the loop.
    assert not calls, f"the design loop was called: {calls}"
    # Both offers were cleared.
    assert _parity_offer(app_with_projects, chat_pid) is None
    assert _parity_offer(app_with_projects, region_pid) is None
    # The SAME quiet acknowledgement on both routes.
    chat_done = [d for e, d in chat_frames if e == "done"]
    region_done = [d for e, d in region_frames if e == "done"]
    assert chat_done and region_done
    assert chat_done[0].get("kind") == "answer"
    assert region_done[0].get("kind") == "answer"
    assert chat_done[0]["message"] == region_done[0]["message"]
    assert "Understood" in chat_done[0]["message"]


def test_fill_recut_parity_acceptance_runs_loop(app_with_projects, monkeypatch):
    """An acceptance on a LIVE offer runs the loop on BOTH routes with
    the SAME fill-and-recut instruction in the request text, and clears
    the offer on both."""
    import d33d.chat_loop as chat_loop_mod
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        # Chat route: seed the LIVE offer, accept.
        r = await client.post("/api/projects", json={"name": "Parity Yes Chat"})
        chat_pid = r.json()["id"]
        _set_part_columns(app_with_projects, chat_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            chat_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        await client.post(
            f"/api/projects/{chat_pid}/chat", json={"message": "yes"}
        )
        chat_frames = await _parity_run_loop(app_with_projects, chat_pid)
        # Region-edit route: seed the SAME LIVE offer, accept.
        r2 = await client.post("/api/projects", json={"name": "Parity Yes Region"})
        region_pid = r2.json()["id"]
        _set_part_columns(app_with_projects, region_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            region_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        r4 = await client.post(
            f"/api/projects/{region_pid}/region-edits",
            json=_parity_region_edit_body("yes"),
        )
        status4 = r4.status_code
        region_frames = await _parity_run_loop(app_with_projects, region_pid)
        return chat_pid, region_pid, chat_frames, region_frames, status4

    chat_pid, region_pid, _chat_frames, _region_frames, status4 = _run_async(
        app_with_projects, _call
    )
    assert status4 == 202, status4
    # The loop ran EXACTLY twice (one per route — both routes' acceptance
    # calls go through ``d33d.design_loop_events.run_design_loop_with_events``,
    # the spy's target). The fill-and-recut instruction is in both
    # request texts (the SAME instruction — the offer's noun/size are
    # server-side state, never re-parsed).
    assert len(calls) == 2, f"expected 2 loop calls, got {len(calls)}"
    for c in calls:
        assert "Fill-and-recut:" in c["request_text"], c["request_text"]
        assert "hole" in c["request_text"]
        assert "38" in c["request_text"]
    # Both offers were cleared (consumed).
    assert _parity_offer(app_with_projects, chat_pid) is None
    assert _parity_offer(app_with_projects, region_pid) is None


def test_fill_recut_parity_setup_failure_restores_offer(app_with_projects, monkeypatch):
    """A setup failure on acceptance restores the offer on BOTH routes
    (the #332 "lost yes" contract), releases the in-flight flag, and
    surfaces the error — the two routes never diverge on the offer's
    survival."""
    import d33d.chat_loop as chat_loop_mod
    import d33d.design_loop_events as dle_mod

    def _boom_loop(*args, **kwargs):
        raise RuntimeError("forced loop setup failure (test)")

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _boom_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _boom_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        results: dict[str, Any] = {}
        # Chat route: seed the LIVE offer, accept — setup must fail.
        r = await client.post("/api/projects", json={"name": "Parity Boom Chat"})
        chat_pid = r.json()["id"]
        _set_part_columns(app_with_projects, chat_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            chat_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        try:
            r2 = await client.post(
                f"/api/projects/{chat_pid}/chat", json={"message": "yes"}
            )
            results["chat_status"] = r2.status_code
        except RuntimeError:
            results["chat_status"] = 500  # the ASGI transport re-raised
        # Region-edit route: seed the SAME LIVE offer, accept — same failure.
        r3 = await client.post("/api/projects", json={"name": "Parity Boom Region"})
        region_pid = r3.json()["id"]
        _set_part_columns(app_with_projects, region_pid, unit_status="settled")
        app_with_projects.state.versions.set_pending_offer(
            region_pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        try:
            r4 = await client.post(
                f"/api/projects/{region_pid}/region-edits",
                json=_parity_region_edit_body("yes"),
            )
            results["region_status"] = r4.status_code
        except RuntimeError:
            results["region_status"] = 500
        results["chat_pid"] = chat_pid
        results["region_pid"] = region_pid
        return results

    results = _run_async(app_with_projects, _call)
    # Both requests errored (the setup exception propagated — no 202).
    assert results["chat_status"] != 202, results
    assert results["region_status"] != 202, results
    # Neither project is 409-blocked — the in-flight flag was released.
    inflight = app_with_projects.state.design_loop_inflight
    assert results["chat_pid"] not in inflight
    assert results["region_pid"] not in inflight
    # The SAME accepted offer was RESTORED on both routes (the acceptance
    # survives the failed setup — never lost). Compare the reader's
    # normalized fields (the restore carries the full accepted-offer dict;
    # ``get_pending_offer`` surfaces ``kind``/``noun``/``size``).
    chat_offer = _parity_offer(app_with_projects, results["chat_pid"])
    region_offer = _parity_offer(app_with_projects, results["region_pid"])
    assert chat_offer is not None, "the accepted fill_recut offer was lost (chat)"
    assert region_offer is not None, "the accepted fill_recut offer was lost (region)"
    assert chat_offer == region_offer == {
        "kind": "fill_recut",
        "noun": "hole",
        "size": 38.0,
    }
