"""Design-loop FINALIZE contract tests (issue #8).

A version is created exactly when the design loop passes validation (the
FINALIZE result) — never on clarify/propose/patch/critique events. Covers:

- a ``pass`` result versions the best candidate's named parameters (the
  full snapshot) and advances ``current_version``;
- a non-pass result (``exhausted``) does NOT create a version (422 — no
  spurious version from a failed loop);
- the design loop is injected via ``app.state.run_design_loop`` (the
  DI seam — no live LLM/Docker needed);
- the design source is versioned: the upload persists to the git repo and
  the current source round-trips.
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


def _repo_path_for(app, project_id: int):
    """The on-disk repo path (server-internal — the API masks it)."""
    from pathlib import Path

    for p in app.state.conn.list_projects():
        if p["id"] == project_id:
            return Path(p["git_repo_path"])
    raise AssertionError(f"project {project_id} not found")


class _StubBest:
    """Duck-type of the design loop's best candidate (carries the named
    parameters that the loop's named-param gate verified)."""

    def __init__(self, params: dict) -> None:
        self.params = params


class _StubResult:
    def __init__(self, status: str, params: dict) -> None:
        self.status = status
        self.best = _StubBest(params)


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
    r = await client.post("/api/projects", json={"name": "finalize test"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


# ---------------------------------------------------------------------------
# (1) A pass result versions the best candidate's params
# ---------------------------------------------------------------------------


def test_finalize_pass_creates_version_with_named_params(app_with_versions):
    """The FINALIZE boundary: the loop passes → a version is created whose
    params are the best candidate's named parameters (the full snapshot),
    and the project's current_version advances to it."""

    async def _call(client):
        pid = await _create_project(client)
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "pass", {"W": 20, "H": 25, "D": 30, "slot": 5}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the bracket", "message": "make it 20mm wide"},
        )
        row = (await client.get(f"/api/projects/{pid}")).json()
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return r, row, timeline

    r, row, timeline = _run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    version = r.json()
    # The version carries the loop's named parameters (full snapshot).
    assert version["params"] == {"W": 20, "H": 25, "D": 30, "slot": 5}
    assert version["name"] == "the bracket"
    # current_version advanced to the new version.
    assert row["current_version"] == version["id"]
    # It's in the timeline (the accepted change is versioned).
    assert len(timeline) == 1
    assert timeline[0]["id"] == version["id"]


# ---------------------------------------------------------------------------
# (2) A non-pass result does NOT create a version
# ---------------------------------------------------------------------------


def test_finalize_exhausted_does_not_create_version(app_with_versions):
    """A loop that exhausts its 3 iterations without passing validation
    must NOT create a spurious version (422, no new version)."""

    async def _call(client):
        pid = await _create_project(client)
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "exhausted", {"W": 20}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize", json={"params": {"W": 20}}
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        row = (await client.get(f"/api/projects/{pid}")).json()
        return r, timeline, row

    r, timeline, row = _run_async(app_with_versions, _call)
    assert r.status_code == 422, r.text
    # No spurious version.
    assert timeline == []
    assert row["current_version"] is None


# ---------------------------------------------------------------------------
# (3) The design loop is injected (DI seam)
# ---------------------------------------------------------------------------


def test_finalize_without_injected_loop_is_503(app_with_versions):
    """The route requires the injected design loop — without it, 503 (not
    a fabricated version, not a silent no-op)."""

    async def _call(client):
        pid = await _create_project(client)
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = _run_async(app_with_versions, _call)
    assert r.status_code == 503


def test_finalize_async_loop_result_is_awaited(app_with_versions):
    """The loop may be async (the real run_design_loop is); the route must
    await it — a sync-only path would return the coroutine object and blow
    up on ``.status``."""

    async def _call(client):
        pid = await _create_project(client)

        async def _loop():
            return _StubResult("pass", {"W": 10})

        app_with_versions.state.run_design_loop = _loop
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        return r

    r = _run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert r.json()["params"] == {"W": 10}


# ---------------------------------------------------------------------------
# (4) Design source is versioned (the versioned .scad text)
# ---------------------------------------------------------------------------


def test_design_source_round_trips_and_is_committed(app_with_versions):
    """Upload the design source (the current OpenSCAD), then read it back —
    it's persisted to the git repo (committed, versioned content)."""

    async def _call(client):
        pid = await _create_project(client)
        scad = "W = 20; H = 25; D = 30;\ncube([W, H, D]);\n"
        r = await client.post(
            f"/api/projects/{pid}/design-source", json={"source": scad}
        )
        assert r.status_code == 200, r.text
        back = (await client.get(f"/api/projects/{pid}/design-source")).json()
        repo = _repo_path_for(app_with_versions, pid)
        return back, repo, scad

    back, repo, scad = _run_async(app_with_versions, _call)
    assert back["source"] == scad

    # The source is in the git repo (committed).
    import subprocess

    cmd = ["git", "-C", str(repo), "show", "HEAD:design.scad"]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == scad


def test_design_source_get_before_upload_is_null(app_with_versions):
    """No design yet → ``{"source": null}`` (no error)."""

    async def _call(client):
        pid = await _create_project(client)
        r = await client.get(f"/api/projects/{pid}/design-source")
        return r

    r = _run_async(app_with_versions, _call)
    assert r.status_code == 200
    assert r.json() == {"source": None}


def test_design_source_rejects_non_json(app_with_versions):
    async def _call(client):
        pid = await _create_project(client)
        return await client.post(
            f"/api/projects/{pid}/design-source",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    r = _run_async(app_with_versions, _call)
    assert r.status_code == 400
