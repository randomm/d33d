"""Pinned variant gallery tests (issue #8, test-surface: test_gallery.py).

Covers the gallery contract:
- pinning a version makes it appear in the gallery endpoint;
- the gallery card includes thumbnail, name, params summary, and the
  available actions (set-as-main, branch-from, archive);
- renaming a pinned version persists and is reflected in the gallery;
- unpinning removes it from the gallery but does NOT delete the version or
  its git commit;
- archived variants are hidden from the default gallery listing but still
  restorable (and revealable via ``?archived=1``);
- "set as main" re-points ``current_version`` (and writes the marker
  commit); "branch from" creates a new project with an isolated git
  history (the variant-card constraint).
"""

from __future__ import annotations

from tests.versioning.helpers import (
    create_project,
    create_version,
    git_log_lines,
    git_rev_count,
    repo_path_for,
    run_async,
)

# ---------------------------------------------------------------------------
# (a) pinning → appears in gallery
# ---------------------------------------------------------------------------


def test_pinning_makes_version_appear_in_gallery(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="box one")
        v2 = await create_version(client, pid, {"W": 30}, name="box two")

        # Gallery empty initially (nothing pinned).
        empty = (await client.get(f"/api/projects/{pid}/gallery")).json()

        # Pin v1 (and set a thumbnail).
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}",
            json={"pinned": True, "thumbnail": "/thumbs/v1.png"},
        )
        cards = (await client.get(f"/api/projects/{pid}/gallery")).json()
        return v1, v2, empty, cards

    v1, v2, empty, cards = run_async(app_with_versions, _call)
    assert empty == []
    assert len(cards) == 1
    card = cards[0]
    assert card["id"] == v1["id"]
    # (b) card includes thumbnail, name, params summary, and actions.
    assert card["thumbnail"] == "/thumbs/v1.png"
    assert card["name"] == "box one"
    assert card["params"] == {"W": 20}
    assert set(card["actions"]) == {"set-as-main", "branch-from", "archive"}
    # v2 (unpinned) is not in the gallery.
    assert all(c["id"] != v2["id"] for c in cards)


# ---------------------------------------------------------------------------
# (c) renaming a pinned version persists and is reflected
# ---------------------------------------------------------------------------


def test_renaming_pinned_version_reflected_in_gallery(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="old name")
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"pinned": True}
        )
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"name": "the good one"}
        )
        return (await client.get(f"/api/projects/{pid}/gallery")).json()

    cards = run_async(app_with_versions, _call)
    assert len(cards) == 1
    assert cards[0]["name"] == "the good one"
    # Renaming did not touch params (edge case: name-edit isolation).
    assert cards[0]["params"] == {"W": 20}


# ---------------------------------------------------------------------------
# (d) unpinning removes from gallery but NOT the version or its commit
# ---------------------------------------------------------------------------


def test_unpinning_removes_from_gallery_not_the_commit(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="pinned box")
        repo = repo_path_for(app_with_versions, pid)

        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"pinned": True}
        )
        commits_before = git_rev_count(repo)
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"pinned": False}
        )
        cards = (await client.get(f"/api/projects/{pid}/gallery")).json()
        commits_after = git_rev_count(repo)
        # The version still exists (timeline) and the commit is intact.
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return cards, commits_before, commits_after, timeline

    cards, before, after, timeline = run_async(app_with_versions, _call)
    assert cards == []  # removed from gallery
    assert len(timeline) == 1  # version still in the timeline
    assert before == after  # git commit untouched


def test_unpinning_leaves_git_commit_untouched(app_with_versions):
    """Explicit: the gallery is a VIEW — unpin never touches the repo."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="keep me")
        repo = repo_path_for(app_with_versions, pid)
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"pinned": True}
        )
        commits_before = git_rev_count(repo)
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"pinned": False}
        )
        commits_after = git_rev_count(repo)
        return commits_before, commits_after, git_log_lines(repo)

    before, after, log = run_async(app_with_versions, _call)
    assert before == after
    assert "version: keep me" in log


# ---------------------------------------------------------------------------
# (e) archived: hidden from default listing, still restorable
# ---------------------------------------------------------------------------


def test_archived_hidden_from_default_but_restorable(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="archived box")
        await create_version(client, pid, {"W": 25}, name="current box")

        # Pin + archive v1.
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}",
            json={"pinned": True, "archived": True},
        )
        default = (await client.get(f"/api/projects/{pid}/gallery")).json()
        archived_view = (
            await client.get(f"/api/projects/{pid}/gallery", params={"archived": 1})
        ).json()

        # Still restorable: restore v1 → a new forward version.
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        return v1, default, archived_view, r

    v1, default, archived_view, r = run_async(app_with_versions, _call)
    # Hidden from the default listing.
    assert default == []
    # Revealable via ?archived=1.
    assert len(archived_view) == 1 and archived_view[0]["id"] == v1["id"]
    # Still restorable (non-destructive forward version).
    assert r.status_code == 201, r.text
    assert r.json()["restored_from"] == v1["id"]


# ---------------------------------------------------------------------------
# set-as-main: re-points current_version + marker commit
# ---------------------------------------------------------------------------


def test_set_as_main_repoints_current_version(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="first")
        v2 = await create_version(client, pid, {"W": 30}, name="second")
        repo = repo_path_for(app_with_versions, pid)
        commits_before = git_rev_count(repo)

        # v2 is current (latest). Set v1 as main.
        await client.post(f"/api/projects/{pid}/versions/{v1['id']}/set-as-main")
        row_after = (await client.get(f"/api/projects/{pid}")).json()
        commits_after = git_rev_count(repo)
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return v1, v2, row_after, commits_before, commits_after, timeline

    v1, v2, row_after, cb, ca, timeline = run_async(app_with_versions, _call)
    assert row_after["current_version"] == v1["id"]
    # Marker commit written (history grew by 1, no rewrite).
    assert ca == cb + 1
    # The timeline chain is intact (no version added/removed).
    assert [t["id"] for t in timeline] == [v1["id"], v2["id"]]


# ---------------------------------------------------------------------------
# branch-from: variant card with ISOLATED git history
# ---------------------------------------------------------------------------


def test_branch_from_creates_variant_card_with_isolated_history(app_with_versions):
    """Forks are variant cards, not a git graph: the new project's git
    history does NOT include the source project's commits."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 20, "H": 25}, name="base box")
        v2 = await create_version(client, pid, {"W": 24}, name="widened")
        src_repo = repo_path_for(app_with_versions, pid)

        r = await client.post(f"/api/projects/{pid}/versions/{v2['id']}/branch-from")
        result = r.json()
        new_pid = result["project"]["id"]
        new_repo = repo_path_for(app_with_versions, new_pid)

        # The new project's repo is a distinct, fresh git history.
        src_revs = git_rev_count(src_repo)
        new_revs = git_rev_count(new_repo)
        # Source commits must not appear in the new repo's history.
        new_log = git_log_lines(new_repo)
        src_log = git_log_lines(src_repo)
        return (
            result,
            new_pid,
            src_revs,
            new_revs,
            new_log,
            src_log,
        )

    result, new_pid, src_revs, new_revs, new_log, src_log = run_async(
        app_with_versions, _call
    )
    # New project, own repo, own history.
    assert new_pid is not None
    assert new_revs == 1  # just the seed commit
    # Source history (multiple commits) does NOT leak into the new repo.
    assert len(src_log) > 1
    assert new_log != src_log
    # The seed version carries the source's FULL snapshot + fork provenance.
    seed = result["version"]
    fork = seed["forked_from"]
    # forked_from is stored as a (project_id, version_id) pair (tuple/JSON).
    assert fork is not None
    src_project_id, src_version_id = (
        fork
        if isinstance(fork, (list, tuple))
        else (fork["project_id"], fork["version_id"])
    )
    assert isinstance(src_project_id, int)
    assert isinstance(src_version_id, int)
    # The seed's params equal the source version's full snapshot.
    assert seed["params"] == {"W": 24}
    # The source project is untouched (its repo still has >= 2 commits).
    assert src_revs >= 2
