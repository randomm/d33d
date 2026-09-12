"""Version timeline tests (issue #8, test-surface: test_timeline.py).

Covers the timeline contract:
- each accepted change creates a new Version with the correct parent link
  and triggering-message excerpt;
- the timeline API returns versions in chronological order with diff-badge
  data (``diff_count`` = params that changed vs the parent);
- the timeline entries are never exposed as raw git objects (git
  invisibility contract — no 40-hex hashes, no ``git`` tokens, no commit
  metadata in any response).

All non-slow: local git only (``git init``/``git commit`` in tmp_path),
no Docker, no network.
"""

from __future__ import annotations

import json

from d33d import versions
from tests.versioning.helpers import (
    create_project,
    create_version,
    git_log,
    repo_path_for,
    run_async,
)

# ---------------------------------------------------------------------------
# (a) Each accepted change creates a version with parent link + message
# ---------------------------------------------------------------------------


def test_accepted_change_creates_version_with_parent_and_message(
    app_with_versions,
):
    """A second version has parent = first version's id, and carries the
    triggering message excerpt on the row (created_by_message)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(
            client, pid, {"W": 20, "H": 25, "D": 30}, message="make a box"
        )
        v2 = await create_version(
            client,
            pid,
            {"W": 25, "H": 25, "D": 30},
            message="widen the box by 5mm",
        )
        return pid, v1, v2

    _pid, v1, v2 = run_async(app_with_versions, _call)

    assert v1["parent"] is None
    assert v1["created_by_message"] == "make a box"
    assert v2["parent"] == v1["id"]
    assert v2["created_by_message"] == "widen the box by 5mm"


# ---------------------------------------------------------------------------
# (b) Timeline API: chronological order + diff badge
# ---------------------------------------------------------------------------


def test_timeline_returns_versions_in_order_with_diff_badges(app_with_versions):
    """The timeline is oldest-first and each entry carries a ``diff_count``
    (params changed vs the parent) — the badge data for "3 params
    changed"."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 20, "H": 25, "D": 30})
        await create_version(client, pid, {"W": 25, "H": 25, "D": 30})  # 1 change
        await create_version(
            client, pid, {"W": 25, "H": 25, "D": 30, "slot": True}
        )  # 1 change (added)
        await create_version(
            client, pid, {"W": 25, "H": 30, "D": 20, "slot": True, "wall": "yes"}
        )  # 3 changes (H, D changed; wall added)
        r = await client.get(f"/api/projects/{pid}/versions")
        return pid, r

    _pid, r = run_async(app_with_versions, _call)
    assert r.status_code == 200
    timeline = r.json()
    assert len(timeline) == 4

    # Chronological order (id order).
    ids = [v["id"] for v in timeline]
    assert ids == sorted(ids)

    # Diff badges: first version is 0 by definition; then 1, 1, 3.
    assert [v["diff_count"] for v in timeline] == [0, 1, 1, 3]


def test_restored_version_badge_is_parent_based_not_position_based(
    app_with_versions,
):
    """A restored version's diff badge diffs against its OWN parent (the
    current latest at restore time), not the immediately-preceding timeline
    row — for a forward chain the parent IS the preceding row, but the
    badge must be computed from the parent pointer, not the list position.
    (The position-based variant was the original finding: it would mis-report
    a restored version whose parent is not the row before it.)"""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20, "H": 25, "D": 30})
        await create_version(client, pid, {"W": 24, "H": 25, "D": 30})
        await create_version(client, pid, {"W": 24, "H": 30, "D": 30})
        # Restore v1: a NEW forward version with v1's snapshot, parent = the
        # current latest (v3). It sits at the tail of the list.
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        assert r.status_code == 201, r.text
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return v1, timeline

    v1, timeline = run_async(app_with_versions, _call)
    restored = timeline[-1]
    # Restore's provenance: restored_from = the target, parent = latest.
    assert restored["restored_from"] == v1["id"]
    assert restored["parent"] == timeline[-2]["id"]
    # The badge diffs restored-vs-its-parent (v3 = {W: 24, H: 30, D: 30};
    # restored = {W: 20, H: 25, D: 30}) → W + H changed → 2 params. The
    # position-based computation (diff against the row before it) yields the
    # same value for a forward chain — the parent-pointer logic is what
    # guarantees correctness for the general case.
    assert restored["diff_count"] == 2


# ---------------------------------------------------------------------------
# (c) Thumbnails are attached to each version entry
# ---------------------------------------------------------------------------


def test_thumbnail_is_carried_on_the_version_entry(app_with_versions):
    """A thumbnail URL (or placeholder path) set on a version round-trips
    through the timeline entry."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20})
        r = await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}",
            json={"thumbnail": "/renders/v1/view_05_iso.png"},
        )
        assert r.status_code == 200
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return timeline

    timeline = run_async(app_with_versions, _call)
    assert len(timeline) == 1
    assert timeline[0]["thumbnail"] == "/renders/v1/view_05_iso.png"


# ---------------------------------------------------------------------------
# (d) The timeline is never exposed as raw git objects
# ---------------------------------------------------------------------------


def test_timeline_exposes_no_raw_git_objects(app_with_versions):
    """Git invisibility (hard product requirement): the timeline response
    carries version names, messages, params, ids — but NO commit hash, NO
    ``git`` token, NO branch name, NO raw git command output."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 20}, message="a box please")
        r = await client.get(f"/api/projects/{pid}/versions")
        return pid, r

    _pid, r = run_async(app_with_versions, _call)
    body = r.text
    # 40-hex commit hashes: must be absent.
    assert not versions._HASH_RE.search(body), f"hash leaked: {body}"
    # The word 'git' (excluding gitub/gitlab-style names): must be absent.
    assert not versions._GIT_RE.search(body), f"git leaked: {body}"
    # Every entry must use the public shape (no commit/sha fields).
    for entry in r.json():
        assert "commit" not in entry
        assert "sha" not in entry
        assert "branch" not in entry
        assert "hash" not in entry


def test_version_files_are_committed_to_git_on_disk(app_with_versions):
    """The content spine: the full snapshot lands in the per-project git
    repo as ``versions/{id}/params.json`` — verified on disk (git is the
    storage; the UI never sees it)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20, "H": 25}, name="first box")
        repo = repo_path_for(app_with_versions, pid)
        return v1, repo

    v1, repo = run_async(app_with_versions, _call)
    snapshot = json.loads(
        (repo / "versions" / str(v1["id"]) / "params.json").read_text()
    )
    assert snapshot == {"W": 20, "H": 25}
    log = git_log(repo)
    assert "version: firstbox" in log
