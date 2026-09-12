"""Project library tests (issue #8, test-surface: test_project_library.py).

Covers the project-library contract:
- the library grid returns name, last-activity timestamp, and thumbnail for
  each project;
- search filters by name, tags, and free-text notes (client-side over the
  full rows the endpoint returns — this test asserts the filterable data is
  present and filterable);
- opening a project returns the conversation context at its latest version
  plus the version timeline (the "resumed chat" surface).
"""

from __future__ import annotations

from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)

# ---------------------------------------------------------------------------
# (a) library grid: name, last-activity, thumbnail
# ---------------------------------------------------------------------------


def test_library_returns_name_last_activity_and_thumbnail(app_with_versions):
    async def _call(client):
        p1 = await create_project(client, name="the desk bracket")
        await client.patch(
            f"/api/projects/{p1['id']}",
            json={"tags": ["desk"], "notes": "for the studio"},
        )
        v1 = await create_version(client, p1["id"], {"W": 20}, name="bracket v1")
        # Set a thumbnail on v1 (it becomes the project's card thumbnail —
        # the latest version's thumbnail).
        await client.patch(
            f"/api/projects/{p1['id']}/versions/{v1['id']}",
            json={"thumbnail": "/thumbs/bracket-iso.png"},
        )
        # A second project with no versions (empty card fields).
        p2 = await create_project(client, name="empty project")

        r = await client.get("/api/library")
        cards = r.json()
        p1_card = next(c for c in cards if c["id"] == p1["id"])
        p2_card = next(c for c in cards if c["id"] == p2["id"])
        return p1_card, p2_card

    p1_card, p2_card = run_async(app_with_versions, _call)

    # (a) name, last-activity timestamp, and thumbnail present.
    assert p1_card["name"] == "the desk bracket"
    assert p1_card["last_activity"] is not None
    assert p1_card["last_activity"]["ts"]  # an ISO timestamp string
    assert p1_card["last_activity"]["version_id"] is not None
    assert p1_card["thumbnail"] == "/thumbs/bracket-iso.png"

    # The zero-version project: null activity payload (no version yet),
    # null thumbnail, no error.
    assert p2_card["name"] == "empty project"
    assert (p2_card["last_activity"] or {}).get("version_id") is None
    assert p2_card["thumbnail"] is None
    assert p2_card["current_version"] is None


# ---------------------------------------------------------------------------
# (b) search over name, tags, and notes
# ---------------------------------------------------------------------------


def test_search_filters_by_name_tags_and_notes(app_with_versions):
    """The library endpoint returns the FULL filterable rows; the client's
    search over names/tags/notes works over them (simulated here — the
    endpoint carries all three fields; no server-side search param)."""

    async def _call(client):
        p1 = await create_project(client, name="the desk bracket")
        await client.patch(f"/api/projects/{p1['id']}", json={"tags": ["desk"], "notes": "studio shelf"})
        p2 = await create_project(client, name="wall hook")
        await client.patch(f"/api/projects/{p2['id']}", json={"tags": ["wall"], "notes": "holds the lamp"})
        p3 = await create_project(client, name="lamp arm")
        await client.patch(f"/api/projects/{p3['id']}", json={"tags": ["desk", "light"], "notes": "articulated arm"})
        cards = (await client.get("/api/library")).json()
        return cards

    cards = run_async(app_with_versions, _call)

    def _matches(cards, needle: str) -> list[dict]:
        n = needle.lower()
        return [
            c
            for c in cards
            if n in c["name"].lower()
            or any(n in t.lower() for t in c["tags"])
            or n in (c["notes"] or "").lower()
        ]

    # Name search.
    assert [c["name"] for c in _matches(cards, "bracket")] == ["the desk bracket"]
    # Tag search.
    assert len(_matches(cards, "desk")) == 2  # the desk bracket + lamp arm
    # Notes search.
    assert [c["name"] for c in _matches(cards, "lamp")] == ["wall hook", "lamp arm"]


# ---------------------------------------------------------------------------
# (c) opening a project resumes at the latest version + the timeline
# ---------------------------------------------------------------------------


def test_opening_project_resumes_at_latest_version_with_timeline(
    app_with_versions,
):
    """GET /api/projects/{id} (open) + GET /api/projects/{id}/versions
    (the side-rail timeline) give the resumed state: the conversation's
    latest version plus the full history."""

    async def _call(client):
        p = await create_project(client, name="resume me")
        pid = p["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="v1")
        v2 = await create_version(client, pid, {"W": 25}, name="v2")
        v3 = await create_version(client, pid, {"W": 30}, name="v3")

        opened = (await client.get(f"/api/projects/{pid}")).json()
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return pid, v1["id"], v2["id"], v3["id"], opened, timeline

    _pid, v1, v2, v3, opened, timeline = run_async(app_with_versions, _call)

    # The opened project points at the LATEST version (resume state).
    assert opened["current_version"] == v3
    # The timeline side rail: the full history, in order.
    assert [t["id"] for t in timeline] == [v1, v2, v3]
    # The latest entry is v3 (the conversation resumes THERE).
    assert timeline[-1]["name"] == "v3"
    # The transcript (conversation context) is intact alongside.
    assert opened["name"] == "resume me"
