"""Non-destructive restore tests (issue #8, test-surface: test_restore.py).

Covers the restore contract:
- restoring from version N creates a NEW forward Version (not a git reset);
- the new version's parent is the CURRENT latest (not version N's parent);
- the restored version's params match the target's params exactly (full
  snapshot);
- provenance is the dedicated ``restored_from`` field (never the editable
  name — renaming cannot destroy lineage);
- the original version N is unmodified and still reachable;
- restoring the current latest version is a no-op (409, no spurious entry);
- the git history gains a forward commit and never rewinds.

All non-slow: local git only.
"""

from __future__ import annotations

from tests.versioning.helpers import (
    create_project,
    create_version,
    git_log,
    repo_path_for,
    run_async,
)


async def _three_versions(client, pid):
    v1 = await create_version(client, pid, {"W": 20, "H": 25, "D": 30}, name="v1")
    v2 = await create_version(client, pid, {"W": 24, "H": 25, "D": 30}, name="v2")
    v3 = await create_version(client, pid, {"W": 24, "H": 30, "D": 30}, name="v3")
    return v1, v2, v3


# ---------------------------------------------------------------------------
# (a)+(b)+(c) restore creates a forward version, parent = latest, params equal
# ---------------------------------------------------------------------------


def test_restore_creates_forward_version_with_correct_parent(app_with_versions):
    """Restoring v1 when v3 is latest: new version's parent is v3 (the
    current latest at restore time), NOT v1's parent (None), and its params
    match v1's snapshot exactly."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20, "H": 25, "D": 30}, name="v1")
        v2 = await create_version(client, pid, {"W": 24, "H": 25, "D": 30}, name="v2")
        v3 = await create_version(client, pid, {"W": 24, "H": 30, "D": 30}, name="v3")
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        return pid, v1, v2, v3, r

    _pid, v1, _v2, v3, r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    restored = r.json()

    # Forward: a NEW version id, strictly after v3.
    assert restored["id"] > v3["id"]
    # Parent = the current latest (v3), not v1's parent (None).
    assert restored["parent"] == v3["id"]
    # Full snapshot equality with the target.
    assert restored["params"] == v1["params"] == {"W": 20, "H": 25, "D": 30}


# ---------------------------------------------------------------------------
# (d) provenance marker is a dedicated field, not the editable name
# ---------------------------------------------------------------------------


def test_restore_provenance_is_dedicated_field(app_with_versions):
    """``restored_from`` records the lineage independently of the display
    name (which the user can edit without destroying provenance)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1, _v2, _v3 = await _three_versions(client, pid)
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        restored = r.json()
        # Rename the restored version — provenance must survive.
        r2 = await client.patch(
            f"/api/projects/{pid}/versions/{restored['id']}",
            json={"name": "my favourite box"},
        )
        return v1, restored, r2.json()

    v1, restored, renamed = run_async(app_with_versions, _call)
    assert restored["restored_from"] == v1["id"]
    # The name is the auto-derived one (from the provenance message), not
    # a copy of "v1" — and renaming changes the name only.
    assert renamed["name"] == "my favourite box"
    assert renamed["restored_from"] == v1["id"]
    assert renamed["params"] == v1["params"]


def test_restore_to_latest_is_noop_409(app_with_versions):
    """Edge case: restoring the CURRENT latest version is a no-op — 409, no
    spurious 'restored from vN' entry with an identical snapshot."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        _v1, _v2, v3 = await _three_versions(client, pid)
        before = (await client.get(f"/api/projects/{pid}/versions")).json()
        r = await client.post(f"/api/projects/{pid}/versions/{v3['id']}/restore")
        after = (await client.get(f"/api/projects/{pid}/versions")).json()
        return r, before, after

    r, before, after = run_async(app_with_versions, _call)
    assert r.status_code == 409, r.text
    assert len(after) == len(before) == 3


# ---------------------------------------------------------------------------
# (e) original version N unmodified and still reachable
# ---------------------------------------------------------------------------


def test_original_version_unmodified_after_restore(app_with_versions):
    """The source version survives a restore byte-for-byte: same id, same
    params, still reachable in the timeline, and still restorable a second
    time (a new forward commit each time)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1, _v2, _v3 = await _three_versions(client, pid)
        await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        original = next(v for v in timeline if v["id"] == v1["id"])
        # Still restorable a second time (new forward version each time).
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        return v1, timeline, original, r

    v1, timeline, original, r = run_async(app_with_versions, _call)
    # v1 still in the timeline, unmodified.
    assert [v["id"] for v in timeline].count(v1["id"]) == 1
    assert original["params"] == {"W": 20, "H": 25, "D": 30}
    assert original["parent"] is None
    # The second restore succeeded (a new forward version, not a 409 no-op
    # — the latest has moved since the first restore).
    assert r.status_code == 201, r.text


# ---------------------------------------------------------------------------
# git history: forward commit, never a reset
# ---------------------------------------------------------------------------


def test_restore_is_forward_commit_not_reset(app_with_versions):
    """The git repo GAINS a commit (the forward commit); history never
    rewinds — the v1/v2/v3 commits remain in order, plus the new one."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1, _v2, _v3 = await _three_versions(client, pid)
        repo = repo_path_for(app_with_versions, pid)
        before = git_log(repo)
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        after = git_log(repo)
        return v1, before, after, r.status_code

    _v1, before, after, status = run_async(app_with_versions, _call)
    assert status == 201
    # The new commit is prepended (git log is newest-first); the prior
    # commits all survive in their relative order at the tail.
    assert after[len(after) - len(before):] == before
    assert len(after) == len(before) + 1
    assert "restored from version" in after[0]


def test_restore_of_unknown_version_is_404(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await client.post(f"/api/projects/{pid}/versions/999/restore")

    r = run_async(app_with_versions, _call)
    assert r.status_code == 404
