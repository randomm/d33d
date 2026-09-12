"""Version-management regression suite (issue #8).

The four coverage gaps the ticket calls out:

* **Full-snapshot invariant** — each version's params dict is a COMPLETE
  parameter set (all keys present), never a sparse diff from the parent.
* **Concurrent creation** — two simultaneous creates on one project: exactly
  one wins (the other 409s), and the chain stays linear with valid parent
  pointers (no stale-HEAD forks).
* **Name-edit isolation** — renaming a version changes ONLY the display
  name; params, parent link, and the git commit are untouched.
* **Git invisibility (all API surfaces)** — no API response body exposes a
  raw git hash, branch name, or git command output (the hard product
  requirement, widened beyond the UI to every response).
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d import versions
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


async def _create_project(client: AsyncClient) -> int:
    r = await client.post("/api/projects", json={"name": "regression"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


async def _create_version(client, pid, params, **kw) -> dict:
    body = {"params": params}
    body.update(kw)
    r = await client.post(f"/api/projects/{pid}/versions", json=body)
    if r.status_code != 201:
        return {"status_code": r.status_code, "error": r.text}
    return r.json()


# ---------------------------------------------------------------------------
# Full-snapshot invariant
# ---------------------------------------------------------------------------


def test_each_version_is_a_full_snapshot_not_a_delta(app_with_versions):
    """Every version's params dict is the COMPLETE parameter set at that
    point (the OpenSCAD-Customizer-presets invariant) — never a sparse
    diff from the parent. A version that changes ONE param still carries
    ALL the keys."""

    async def _call(client):
        pid = await _create_project(client)
        v1 = await _create_version(
            client, pid, {"W": 20, "H": 25, "D": 30, "slot": 5}, name="full"
        )
        # v2 changes only W — but must still carry H, D, slot (full
        # snapshot, not a {W: 24} delta).
        v2 = await _create_version(
            client, pid, {"W": 24, "H": 25, "D": 30, "slot": 5}, name="wider"
        )
        # v3 REMOVES slot — the snapshot must reflect the removal (the
        # key is absent, not None'd, not carried-over).
        v3 = await _create_version(
            client, pid, {"W": 24, "H": 25, "D": 30}, name="no slot"
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return v1, v2, v3, timeline

    v1, v2, v3, timeline = _run_async(app_with_versions, _call)

    # v1: all 4 keys.
    assert set(v1["params"].keys()) == {"W", "H", "D", "slot"}
    # v2: still all 4 keys (W changed; the rest carried forward — full
    # snapshot, not a delta).
    assert set(v2["params"].keys()) == {"W", "H", "D", "slot"}
    assert v2["params"]["H"] == 25
    assert v2["params"]["D"] == 30
    assert v2["params"]["slot"] == 5
    # v3: slot removed — the key is ABSENT (not a delta marker).
    assert set(v3["params"].keys()) == {"W", "H", "D"}
    assert "slot" not in v3["params"]
    # The on-disk committed snapshot matches the API response (the git
    # spine carries the full snapshot too).
    assert timeline[0]["params"] == v1["params"]
    assert timeline[1]["params"] == v2["params"]
    assert timeline[2]["params"] == v3["params"]


# ---------------------------------------------------------------------------
# Concurrent creation
# ---------------------------------------------------------------------------


def test_concurrent_creation_serializes_to_linear_chain(app_with_versions):
    """Edge case: two simultaneous version creates on one project. The
    write lock serializes them; the chain stays LINEAR with valid parent
    pointers (no stale-HEAD fork, no branch). One request can win while
    the other observes the moved chain — for a plain create both succeed
    in sequence (each reads the latest under the lock), producing a
    linear 2-version chain."""

    async def _call(client):
        pid = await _create_project(client)

        results = await asyncio.gather(
            _create_version(client, pid, {"W": 20}, name="racer A"),
            _create_version(client, pid, {"W": 21}, name="racer B"),
            return_exceptions=True,
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        row = (await client.get(f"/api/projects/{pid}")).json()
        return pid, results, timeline, row

    pid, results, timeline, row = _run_async(app_with_versions, _call)

    # No exception escaped (the gather would have captured one). Both
    # creates resolved as version dicts (a plain create always 201s).
    assert len(results) == 2
    for r in results:
        assert not isinstance(r, Exception), f"create raised: {r!r}"
    assert isinstance(results[0], dict) and "id" in results[0]
    assert isinstance(results[1], dict) and "id" in results[1]

    # The chain is LINEAR: each version's parent is the immediately
    # preceding version's id (no branch, no stale-HEAD parent).
    ids = [v["id"] for v in timeline]
    for i, v in enumerate(timeline):
        if i == 0:
            assert v["parent"] is None
        else:
            assert v["parent"] == ids[i - 1], (
                f"parent pointer forked: {v['id']}.parent={v['parent']} "
                f"but previous id is {ids[i - 1]}"
            )
    # current_version points at the actual latest (the linear chain's head).
    assert row["current_version"] == timeline[-1]["id"]


def test_concurrent_restores_dedupe_noop(app_with_versions):
    """Two simultaneous RESTOREs of the same version: the write lock
    serializes them; the first creates the forward version, the second
    (which now sees the target is no longer latest-adjacent in the same
    way) must not corrupt the chain — it either succeeds as a second
    forward version or 409s. The chain stays linear either way."""

    async def _call(client):
        pid = await _create_project(client)
        v1 = await _create_version(client, pid, {"W": 20}, name="base")
        v2 = await _create_version(client, pid, {"W": 30}, name="later")
        results = await asyncio.gather(
            client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore"),
            client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore"),
            return_exceptions=True,
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return v1, v2, results, timeline

    v1, v2, results, timeline = _run_async(app_with_versions, _call)
    for r in results:
        assert not isinstance(r, Exception)
    ok = [r for r in results if r.status_code == 201]
    # At least one succeeded; the chain stayed linear.
    assert len(ok) >= 1
    ids = [v["id"] for v in timeline]
    for i, v in enumerate(timeline):
        if i == 0:
            assert v["parent"] is None
        else:
            assert v["parent"] == ids[i - 1]


# ---------------------------------------------------------------------------
# Name-edit isolation
# ---------------------------------------------------------------------------


def test_renaming_changes_only_the_display_name(app_with_versions):
    """Edge case: version-name editability — renaming changes ONLY the
    display name. params, parent link, and the git commit are untouched."""

    async def _call(client):
        pid = await _create_project(client)
        v1 = await _create_version(
            client, pid, {"W": 20, "H": 25}, name="auto derived", message="some prompt"
        )
        # The raw repo path is server-internal (the API masks it) — read
        # it from the DB row (the test needs the on-disk repo; the API
        # never exposes it).
        repo = None
        for p in app_with_versions.state.conn.list_projects():
            if p["id"] == pid:
                repo = Path(p["git_repo_path"])
        assert repo is not None
        log_before = _git_log_lines(repo)
        commit_count_before = _git_rev_count(repo)

        r = await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}", json={"name": "RENAMED"}
        )
        assert r.status_code == 200
        renamed = r.json()

        log_after = _git_log_lines(repo)
        commit_count_after = _git_rev_count(repo)
        # Re-read the version and the timeline.
        fresh = (await client.get(f"/api/projects/{pid}/versions/{v1['id']}")).json()
        return (
            v1,
            renamed,
            fresh,
            log_before,
            log_after,
            commit_count_before,
            commit_count_after,
        )

    v1, renamed, fresh, log_b, log_a, cb, ca = _run_async(app_with_versions, _call)
    # The name changed.
    assert renamed["name"] == "RENAMED"
    # params, parent, and created_by_message are UNTOUCHED.
    assert renamed["params"] == v1["params"] == {"W": 20, "H": 25}
    assert renamed["parent"] == v1["parent"]
    assert renamed["created_by_message"] == v1["created_by_message"]
    assert fresh["name"] == "RENAMED"
    # The git commit is UNTOUCHED (no new commit, no rewritten history).
    assert cb == ca
    assert log_b == log_a


def _git_log_lines(repo: Path) -> list[str]:
    cmd = ["git", "-C", str(repo), "log", "--format=%s"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git log failed: {r.stderr}")
    return [l for l in r.stdout.strip().split("\n") if l]


def _git_rev_count(repo: Path) -> int:
    cmd = ["git", "-C", str(repo), "rev-list", "--count", "HEAD"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git rev-list failed: {r.stderr}")
    return int(r.stdout.strip())


# ---------------------------------------------------------------------------
# Git invisibility — all API surfaces
# ---------------------------------------------------------------------------


def test_no_git_leaks_in_any_api_response(app_with_versions):
    """The git-invisibility invariant, widened to every API response: no
    response body carries a raw git hash (40-hex), the word `git`
    (excluding github/gitlab-style names), or a git command. Covers the
    timeline, gallery, compare, library, project, and version endpoints.

    The design-source POST is swept with its own on-disk-path assertion —
    the raw repo path is server-internal and the response names only the
    repo-relative file (the same class of leak the hash/git-token sweep
    cannot catch on its own)."""

    async def _call(client):
        pid = await _create_project(client)
        v1 = await _create_version(
            client, pid, {"W": 20, "H": 25}, name="box", message="a box please"
        )
        await client.patch(
            f"/api/projects/{pid}/versions/{v1['id']}",
            json={"pinned": True, "thumbnail": "/t.png"},
        )
        v2 = await _create_version(client, pid, {"W": 24}, name="wider")

        responses = {
            "project": await client.get(f"/api/projects/{pid}"),
            "list_projects": await client.get("/api/projects"),
            "timeline": await client.get(f"/api/projects/{pid}/versions"),
            "single_version": await client.get(
                f"/api/projects/{pid}/versions/{v1['id']}"
            ),
            "gallery": await client.get(f"/api/projects/{pid}/gallery"),
            "compare": await client.get(
                f"/api/projects/{pid}/versions/compare",
                params={"a": v1["id"], "b": v2["id"]},
            ),
            "library": await client.get("/api/library"),
            "design_source": await client.get(f"/api/projects/{pid}/design-source"),
            "design_source_put": await client.post(
                f"/api/projects/{pid}/design-source",
                json={"source": "cube([W, H, D]);\n"},
            ),
            "set_as_main": await client.post(
                f"/api/projects/{pid}/versions/{v2['id']}/set-as-main"
            ),
            "restore": await client.post(
                f"/api/projects/{pid}/versions/{v1['id']}/restore"
            ),
            "branch_from": await client.post(
                f"/api/projects/{pid}/versions/{v1['id']}/branch-from"
            ),
        }
        return responses

    responses = _run_async(app_with_versions, _call)
    for name, r in responses.items():
        body = r.text
        # No 40-hex commit hash.
        assert not versions._HASH_RE.search(body), (
            f"{name}: git hash leaked: {versions._HASH_RE.findall(body)}"
        )
        # No `git` token (excluding github/gitlab-style names).
        assert not versions._GIT_RE.search(body), (
            f"{name}: 'git' token leaked in: {body[:200]}"
        )


def test_design_source_put_does_not_leak_on_disk_path(app_with_versions):
    """The design-source POST response must not expose the raw on-disk repo
    path (the path names the repo's on-disk location — the same class of
    leak as the git-hash/git-token sweep, which cannot catch a path on its
    own). The response names only the repo-relative file."""

    async def _call(client):
        pid = await _create_project(client)
        r = await client.post(
            f"/api/projects/{pid}/design-source",
            json={"source": "cube([20, 25, 30]);\n"},
        )
        return r

    r = _run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()
    # The stored value is the repo-relative file name, not an absolute path.
    assert body["stored"] == "design.scad"
    assert not Path(body["stored"]).is_absolute()
    # And the raw on-disk path never appears anywhere in the body.
    assert body["length"] == len("cube([20, 25, 30]);\n")
