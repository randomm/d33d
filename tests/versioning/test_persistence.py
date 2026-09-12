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

from httpx import ASGITransport, AsyncClient

from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)
# ---------------------------------------------------------------------------
# (a) reopening resumes at the latest version with the full timeline intact
# ---------------------------------------------------------------------------


def test_reopening_resumes_at_latest_version_full_timeline(app_with_versions):
    """A NEW client session over the same app/DB (the "close the browser,
    come back a day later" simulation) sees the full timeline in order and
    the project row pointing at the latest version."""

    async def _call(client):
        pid = (await create_project(client))["id"]
        v1 = await create_version(client, pid, {"W": 20, "H": 25}, name="first")
        v2 = await create_version(client, pid, {"W": 24}, name="second")
        v3 = await create_version(client, pid, {"W": 24, "H": 30}, name="third")
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

    pid, v1, v2, v3, row, timeline = run_async(app_with_versions, _call)
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
        project = await create_project(client, name="metadata project")
        pid = project["id"]
        # Patch the tags/notes (create_project only sets name).
        await client.patch(
            f"/api/projects/{pid}",
            json={"tags": ["desk", "bracket"], "notes": "a desk bracket for the studio"},
        )
        await create_version(client, pid, {"W": 20}, name="v1")

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

    row, photo = run_async(app_with_versions, _call)
    assert row["tags"] == ["desk", "bracket"]
    assert row["notes"] == "a desk bracket for the studio"
    assert row["source_photo_path"] == photo["source_photo_path"]
    assert row["current_version"] is not None


# ---------------------------------------------------------------------------
# (c) zero-version project opens with an empty timeline and no error
# ---------------------------------------------------------------------------


def test_fresh_project_has_empty_timeline_no_error(app_with_versions):
    async def _call(client):
        pid = (await create_project(client))["id"]
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

    status, timeline, gallery_status, gallery, library = run_async(
        app_with_versions, _call
    )
    assert status == 200
    assert timeline == []
    assert gallery_status == 200
    assert gallery == []
    # The library lists the fresh project with a null current version.
    fresh = [p for p in library if p["current_version"] is None]
    assert len(fresh) >= 1


# ---------------------------------------------------------------------------
# Data-model round-trip: notes, last_activity, archived (the resolved
# open-question fields — the surfaces the spec describes need them)
# ---------------------------------------------------------------------------


def test_notes_last_activity_and_archive_round_trip(app_with_versions):
    """The resolved data model (issue #8 open question): Project.notes,
    Project.last_activity, and Version.archived round-trip through the
    API — the library card's last-activity field and the gallery archive
    action depend on these fields existing and persisting."""

    async def _call(client):
        project = await create_project(client, name="round trip project")
        pid = project["id"]
        await client.patch(
            f"/api/projects/{pid}",
            json={"tags": ["desk"], "notes": "bracket for the studio shelf"},
        )
        v1 = await create_version(client, pid, {"W": 20}, name="v1")
        # Pin + archive v1 (the gallery archive action).
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}",
            json={"pinned": True, "archived": True},
        )
        # A fresh client session reads everything back (the "close the
        # browser, come back" simulation — the DB is shared).
        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            row = (await fresh.get(f"/api/projects/{pid}")).json()
            timeline = (await fresh.get(f"/api/projects/{pid}/versions")).json()
            return row, timeline

    row, timeline = run_async(app_with_versions, _call)
    # Project.notes round-trips (free-text outcome notes).
    assert row["notes"] == "bracket for the studio shelf"
    # Project.last_activity is stamped (the library card's activity field —
    # the ts/version of the latest version).
    assert row["last_activity"] is not None
    assert row["last_activity"]["ts"]
    assert row["last_activity"]["version_id"] == timeline[-1]["id"]
    # Version.archived round-trips (the gallery archive action).
    assert timeline[0]["archived"] is True
