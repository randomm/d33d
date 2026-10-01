"""Issue #295 (d5) — restore / branch-from 409 ``source_missing``.

The restore and branch-from seams carry the TARGET version's design
source (``versions/{id}/design.scad``). When that source is expected on
disk but absent — the project's git repo directory is gone, or the
target version's commit recorded a design.scad that has since been lost
out-of-band — the seam 409s with the machine-readable detail code
``"source_missing"`` (body: ``{"detail": {"code": "source_missing",
"message": …}}``) — the SPA maps the code to copy.ts text.

The no-op restore (target IS the current latest) keeps its LEGACY string
detail — ``{"detail": "restore target is already the latest version
(no-op)"}`` — and ``test_restore_to_latest_is_noop_409`` in
``tests/versioning/test_restore.py`` stays green unchanged.

A pre-#105 target (a version whose commit never recorded a
design.scad) is NOT the missing state: the restore/branch proceeds as
today (params-only — the honest case for a design with no recorded
geometry).

The "was it ever written" marker is the version's own commit file list
(``source_expected_for_version`` — the same persistent marker the
missing-source chat pre-route and the project GET's ``storage`` field
use), so the 409 cannot fire on a version that never had a source.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from d33d.design_loop import IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.versioning.helpers import create_project, create_version, run_async


def _stub_pass_result(params: dict, scad: str) -> Any:
    """A passing loop stub whose ``best`` is a REAL ``IterationRecord``
    carrying a design source (the FINALIZE route persists it as
    ``versions/{id}/design.scad`` in the version's own commit — the
    post-#105 "was a source recorded" marker)."""
    render = RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v",) * 6,
    )

    class _Result:
        def __init__(self) -> None:
            self.status = "pass"
            self.failure_reason = None
            self.best = IterationRecord(
                iteration=0,
                scad_source=scad,
                render=render,
                score=Score(bits=(False,) * 5, rank=0, tiebreak=(False,) * 5),
                params=dict(params),
            )

    return _Result()


def _repo_for(app, pid: int) -> Path:
    """The project's on-disk repo path (server-internal — the API masks
    it)."""
    for row in app.state.conn.list_projects():
        if row["id"] == pid:
            return Path(row["git_repo_path"])
    raise AssertionError(f"project {pid} not found")


def _design_scad_for(app, pid: int, version_id: int) -> Path:
    return _repo_for(app, pid) / "versions" / str(version_id) / "design.scad"


async def _make_source_version(client, app, pid: int, params: dict, name: str) -> dict:
    """Create a version whose git commit records a design.scad via the
    FINALIZE route (the loop's best candidate's source is persisted as
    ``versions/{id}/design.scad`` in the version's own commit)."""
    app.state.run_design_loop = lambda: _stub_pass_result(params, "cube([20, 25, 30]);\n")
    r = await client.post(
        f"/api/projects/{pid}/finalize",
        json={"params": dict(params), "name": name, "message": name},
    )
    assert r.status_code == 201, r.text
    return r.json()


def _assert_source_missing_409(r) -> None:
    """The 409 carries the object detail ``{"code": "source_missing",
    "message": …}`` — never the legacy string shape."""
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert isinstance(detail, dict), f"expected object detail, got {detail!r}"
    assert detail["code"] == "source_missing", detail
    assert isinstance(detail.get("message"), str) and detail["message"]


# ---------------------------------------------------------------------------
# (a) repo absent → 409 source_missing (restore + branch)
# ---------------------------------------------------------------------------


def test_restore_repo_absent_is_409_source_missing(app_with_versions):
    """The project's git repo directory is deleted out-of-band → restore
    of a version is a 409 with the detail code ``"source_missing"``
    (the geometry cannot be carried without its repo)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await _make_source_version(client, app_with_versions, pid, {"W": 20}, "v1")
        v2 = await create_version(client, pid, {"W": 24}, name="v2")
        # The repo goes away (out-of-band loss).
        shutil.rmtree(_repo_for(app_with_versions, pid))
        r1 = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        r2 = await client.post(f"/api/projects/{pid}/versions/{v2['id']}/branch-from")
        return r1, r2

    r1, r2 = run_async(app_with_versions, _call)
    _assert_source_missing_409(r1)
    _assert_source_missing_409(r2)


def test_branch_repo_absent_is_409_source_missing(app_with_versions):
    """Branch-from under an absent repo is the same 409 — the fork seeds
    the new project from the target version's source, which cannot be
    read without the repo."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await _make_source_version(client, app_with_versions, pid, {"W": 20}, "v1")
        shutil.rmtree(_repo_for(app_with_versions, pid))
        return await client.post(f"/api/projects/{pid}/versions/{v1['id']}/branch-from")

    r = run_async(app_with_versions, _call)
    _assert_source_missing_409(r)


# ---------------------------------------------------------------------------
# (b) target version's recorded design.scad lost → 409 source_missing
# ---------------------------------------------------------------------------


def test_restore_lost_design_scad_is_409_source_missing(app_with_versions):
    """The repo is present but the TARGET version's design.scad is lost
    out-of-band (its commit recorded it — the post-#105 marker) → the
    restore 409s with ``source_missing``."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await _make_source_version(client, app_with_versions, pid, {"W": 20}, "v1")
        v2 = await create_version(client, pid, {"W": 24}, name="v2")
        # The design.scad goes away (out-of-band loss).
        _design_scad_for(app_with_versions, pid, v1["id"]).unlink()
        return await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")

    r = run_async(app_with_versions, _call)
    _assert_source_missing_409(r)


# ---------------------------------------------------------------------------
# (c) pre-#105 target (params-only) → proceeds as today
# ---------------------------------------------------------------------------


def test_restore_pre105_params_only_target_proceeds(app_with_versions):
    """A target whose commit never recorded a design.scad (a plain
    ``POST /versions`` create — the pre-#105 shape) is NOT the missing
    state: the restore proceeds params-only (201), exactly as today."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="v1")
        v2 = await create_version(client, pid, {"W": 24}, name="v2")
        return await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text


def test_branch_pre105_params_only_target_proceeds(app_with_versions):
    """Branch-from of a pre-#105 (params-only) target proceeds (201) —
    the fork seeds params only, as today."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="v1")
        return await client.post(f"/api/projects/{pid}/versions/{v1['id']}/branch-from")

    r = run_async(app_with_versions, _call)
    # The branch-from route returns 200 (the fork's project + seed
    # version), not 201 — the 200 IS the success.
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------------------
# (d) no-op 409 keeps its legacy string detail
# ---------------------------------------------------------------------------


def test_noop_restore_409_detail_is_string(app_with_versions):
    """Restoring the CURRENT latest is a no-op 409 — and its detail is
    the legacy STRING (``{"detail": "restore target is already the
    latest version (no-op)"}``), never the object shape the
    ``source_missing`` 409 uses. The no-op 409's detail is unchanged
    from today."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await create_version(client, pid, {"W": 20}, name="v1")
        v2 = await create_version(client, pid, {"W": 24}, name="v2")
        return await client.post(f"/api/projects/{pid}/versions/{v2['id']}/restore")

    r = run_async(app_with_versions, _call)
    assert r.status_code == 409, r.text
    body = r.json()
    assert isinstance(body["detail"], str), (
        "the no-op 409 must keep its legacy string detail"
    )
    assert "no-op" in body["detail"]


# ---------------------------------------------------------------------------
# (e) the check fires before any mutation + no path in the detail
# ---------------------------------------------------------------------------


def test_source_missing_409_fires_before_version_create(app_with_versions):
    """The missing-source 409 fires BEFORE any version row is created
    (the check runs before ``_run_create``) — no spurious forward
    version, no path in the 409 body (the git-invisibility rule)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v1 = await _make_source_version(client, app_with_versions, pid, {"W": 20}, "v1")
        repo = _repo_for(app_with_versions, pid)
        shutil.rmtree(repo)
        before = app_with_versions.state.versions.list_versions(pid)
        r = await client.post(f"/api/projects/{pid}/versions/{v1['id']}/restore")
        after = app_with_versions.state.versions.list_versions(pid)
        return r, before, after, str(repo)

    r, before, after, repo_path = run_async(app_with_versions, _call)
    _assert_source_missing_409(r)
    # No spurious version row (the check fires before the create).
    assert len(after) == len(before) == 1
    # Git invisibility: the on-disk repo path never crosses the boundary.
    assert repo_path not in r.text
    assert "git" not in r.json()["detail"]["message"]


# ---------------------------------------------------------------------------
# (f) issue #316 (task-b): the design-state route's ``history_missing``
#     flag mirrors the repo-present check
# ---------------------------------------------------------------------------


def test_design_state_history_missing_true_when_repo_absent(app_with_versions):
    """After the same setup as ``test_restore_repo_absent_is_409_source_
    missing`` (create project + version, delete the ``git_repo_path``
    directory), the design-state GET returns ``history_missing: true``
    (the repo check fires the same way) and still serves the entries
    from the DB."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "D": 30.0})
        # The repo goes away (out-of-band loss — same pattern as the
        # restore/branch 409 tests above).
        shutil.rmtree(_repo_for(app_with_versions, pid))
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_missing"] is True
    # Entries are still served (they come from the DB, not the repo).
    assert len(body["entries"]) > 0


def test_design_state_history_missing_false_when_repo_present(app_with_versions):
    """With the repo intact (the normal case), the design-state GET
    returns ``history_missing: false`` — the flag is ``false`` when the
    project's repo directory exists on disk."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0})
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["history_missing"] is False
