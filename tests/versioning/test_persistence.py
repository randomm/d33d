"""Version persistence tests (issue #8, test-surface: test_persistence.py).

Covers the "resuming a project" contract:
- closing the project and reopening it (simulated by a new session/context —
  a fresh client over the same app/DB) resumes at the latest version with
  the full timeline intact;
- the source photo, tags, notes, and current_version survive the round-trip;
- a project with zero versions (freshly created) opens with an empty
  timeline and no error.

All non-slow: local git + SQLite only.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app


def _run_async(app: Any, coro_factory) -> Any:
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


@pytest.fixture
def app_paths(tmp_path):
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_versions(app_paths, tmp_path):
    import d33d.db as db_mod

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
    db_mod._default_git_path = original_default


async def _create_project(
    client: AsyncClient, name: str = "persist test", **kw
) -> dict:
    body = {"name": name}
    body.update(kw)
    r = await client.post("/api/projects", json=body)
    assert r.status_code == 201, r.text
    return r.json()


async def _create_version(client, pid, params, **kw) -> dict:
    body = {"params": params}
    body.update(kw)
    r = await client.post(f"/api/projects/{pid}/versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# (a) reopening resumes at the latest version with the full timeline intact
# ---------------------------------------------------------------------------


def test_reopening_resumes_at_latest_version_full_timeline(app_with_versions):
    """A NEW client session over the same app/DB (the "close the browser,
    come back a day later" simulation) sees the full timeline in order and
    the project row pointing at the latest version."""

    async def _call(client):
        pid = (await _create_project(client))["id"]
        v1 = await _create_version(client, pid, {"W": 20, "H": 25}, name="first")
        v2 = await _create_version(client, pid, {"W": 24}, name="second")
        v3 = await _create_version(client, pid, {"W": 24, "H": 30}, name="third")
        # "Reopen": a FRESH client over the same app/DB (a new session).
        # The lifespan (and thus the DB connection) stays open — closing
        # the browser never drops the backend state.
        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            row = (await fresh.get(f"/api/projects/{pid}")).json()
            timeline = (await fresh.get(f"/api/projects/{pid}/versions")).json()
        return pid, v1["id"], v2["id"], v3["id"], row, timeline

    pid, v1, v2, v3, row, timeline = _run_async(app_with_versions, _call)
    # The timeline is fully intact, in order.
    assert [v["id"] for v in timeline] == [v1, v2, v3]
    # The project points at the latest version.
    assert row["current_version"] == v3
    # Last activity reflects the latest version.
    assert row["last_activity"]["version_id"] == v3


# ---------------------------------------------------------------------------
# (b) source photo, tags, notes, current_version survive the round-trip
# ---------------------------------------------------------------------------


def test_project_metadata_survives_round_trip(app_with_versions, tmp_path):
    """tags, notes, source_photo (via the upload endpoint), and
    current_version all persist across a session boundary."""

    async def _call(client):
        project = await _create_project(
            client,
            name="metadata project",
            tags=["desk", "bracket"],
            notes="a desk bracket for the studio",
        )
        pid = project["id"]
        await _create_version(client, pid, {"W": 20}, name="v1")

        # Upload a source photo (1x1 PNG, png content-type).
        png_bytes = (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
            b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
            b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x01\x00\x05\x18"
            b"\xd8\x9c\x00\x00\x00\x00IEND\xaeB\x60\x82"
        )
        r = await client.post(
            f"/api/projects/{pid}/photos",
            files={"file": ("ref.png", png_bytes, "image/png")},
        )
        assert r.status_code == 201, r.text
        photo = r.json()
        # "Reopen": fresh client, same app/DB.
        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            row = (await fresh.get(f"/api/projects/{pid}")).json()
        return row, photo

    row, photo = _run_async(app_with_versions, _call)
    assert row["tags"] == ["desk", "bracket"]
    assert row["notes"] == "a desk bracket for the studio"
    assert row["source_photo_path"] == photo["source_photo_path"]
    assert row["current_version"] is not None


# ---------------------------------------------------------------------------
# (c) zero-version project opens with an empty timeline and no error
# ---------------------------------------------------------------------------


def test_fresh_project_has_empty_timeline_no_error(app_with_versions):
    async def _call(client):
        pid = (await _create_project(client))["id"]
        r = await client.get(f"/api/projects/{pid}/versions")
        gallery = await client.get(f"/api/projects/{pid}/gallery")
        library = await client.get("/api/library")
        return (
            r.status_code,
            r.json(),
            gallery.status_code,
            gallery.json(),
            library.json(),
        )

    status, timeline, gallery_status, gallery, library = _run_async(
        app_with_versions, _call
    )
    assert status == 200
    assert timeline == []
    assert gallery_status == 200
    assert gallery == []
    # The library lists the fresh project with a null current version.
    fresh = [p for p in library if p["current_version"] is None]
    assert len(fresh) >= 1
