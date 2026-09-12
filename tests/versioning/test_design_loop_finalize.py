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

import json
import subprocess

import pytest

from tests.versioning.helpers import (
    create_project,
    repo_path_for,
    run_async,
)


class _StubBest:
    """Duck-type of the design loop's best candidate (carries the named
    parameters that the loop's named-param gate verified)."""

    def __init__(self, params: dict) -> None:
        self.params = params


class _StubResult:
    def __init__(self, status: str, params: dict) -> None:
        self.status = status
        self.best = _StubBest(params)


# ---------------------------------------------------------------------------
# (1) A pass result versions the best candidate's params
# ---------------------------------------------------------------------------


def test_finalize_pass_creates_version_with_named_params(app_with_versions):
    """The FINALIZE boundary: the loop passes → a version is created whose
    params are the best candidate's named parameters (the full snapshot),
    and the project's current_version advances to it."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
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

    r, row, timeline = run_async(app_with_versions, _call)
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
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "exhausted", {"W": 20}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize", json={"params": {"W": 20}}
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        row = (await client.get(f"/api/projects/{pid}")).json()
        return r, timeline, row

    r, timeline, row = run_async(app_with_versions, _call)
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
        proj = await create_project(client)
        pid = proj["id"]
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 503


def test_finalize_async_loop_result_is_awaited(app_with_versions):
    """The loop may be async (the real run_design_loop is); the route must
    await it — a sync-only path would return the coroutine object and blow
    up on ``.status``."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        async def _loop():
            return _StubResult("pass", {"W": 10})

        app_with_versions.state.run_design_loop = _loop
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert r.json()["params"] == {"W": 10}


# ---------------------------------------------------------------------------
# (4) Design source is versioned (the versioned .scad text)
# ---------------------------------------------------------------------------


def test_design_source_round_trips_and_is_committed(app_with_versions):
    """Upload the design source (the current OpenSCAD), then read it back —
    it's persisted to the git repo (committed, versioned content)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        scad = "W = 20; H = 25; D = 30;\ncube([W, H, D]);\n"
        r = await client.post(
            f"/api/projects/{pid}/design-source", json={"source": scad}
        )
        assert r.status_code == 200, r.text
        back = (await client.get(f"/api/projects/{pid}/design-source")).json()
        repo = repo_path_for(app_with_versions, pid)
        return back, repo, scad

    back, repo, scad = run_async(app_with_versions, _call)
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
        proj = await create_project(client)
        pid = proj["id"]
        r = await client.get(f"/api/projects/{pid}/design-source")
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200
    assert r.json() == {"source": None}


def test_design_source_rejects_non_json(app_with_versions):
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await client.post(
            f"/api/projects/{pid}/design-source",
            content=b"not json",
            headers={"content-type": "application/json"},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 400


def test_design_source_commit_failure_leaves_no_uncommitted_source(
    app_with_versions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(HIGH 1 regression) If the git commit of the design source fails, the
    written ``design.scad`` must NOT be left on disk uncommitted — a prior
    write-before-commit left a silent split state (``GET /design-source``
    read the new source while git history recorded nothing)."""

    def _fail_commit(repo_dir, message: str) -> None:
        raise RuntimeError("git commit failed (simulated index.lock collision)")

    import d33d.projects as projects_mod

    monkeypatch.setattr(projects_mod, "commit_all", _fail_commit)

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        r = await client.post(
            f"/api/projects/{pid}/design-source",
            json={"source": "W = 20; cube([W, 1, 1]);\n"},
        )
        repo = repo_path_for(app_with_versions, pid)
        get_r = await client.get(f"/api/projects/{pid}/design-source")
        return r, repo, pid, get_r

    r, repo, _pid, get_r = run_async(app_with_versions, _call)
    assert r.status_code == 500, r.text
    # The working tree is clean — design.scad is not left behind uncommitted.
    design_scad = repo / "design.scad"
    assert not design_scad.exists(), "design.scad left on disk uncommitted"
    status = subprocess.run(
        ["git", "-C", str(repo), "status", "--porcelain"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert status.returncode == 0, status.stderr
    assert status.stdout.strip() == "", f"working tree not clean: {status.stdout!r}"
    # GET agrees: no uncommitted source is readable.
    assert get_r.status_code == 200
    assert get_r.json() == {"source": None}


def test_design_source_rejects_oversized_body(app_with_versions):
    """A body over the 1 MB cap is rejected with 413."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        body = json.dumps({"source": "x" * (2 * 1024 * 1024)})
        return await client.post(
            f"/api/projects/{pid}/design-source",
            content=body.encode("utf-8"),
            headers={"content-type": "application/json"},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 413


def test_design_source_drain_times_out_on_stalled_stream(
    app_with_versions, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(drain-timeout regression) An oversized body whose stream never ends
    must not hold the connection (and the version-write lock) indefinitely:
    the bounded drain is wrapped in ``asyncio.wait_for`` and a stalled
    client times out into a 413 within the bound."""
    import asyncio as _asyncio

    from fastapi import HTTPException

    import d33d.versions_routes as routes_mod

    monkeypatch.setattr(routes_mod, "_DRAIN_TIMEOUT_SECONDS", 0.5)

    class _StalledStream:
        """A request stream that yields one chunk, then hangs forever."""

        def __init__(self) -> None:
            self._started = False

        def __aiter__(self) -> _StalledStream:
            return self

        async def __anext__(self) -> bytes:
            if not self._started:
                self._started = True
                return b"x" * 1024
            await _asyncio.Event().wait()  # never fires
            return b""

        def close(self) -> None:
            pass

    class _StubRequest:
        def __init__(self) -> None:
            self.headers = {
                "content-type": "application/json",
                "content-length": str(2 * 1024 * 1024),
            }

        def stream(self) -> _StalledStream:
            return _StalledStream()

    async def _call(client):
        # Oversized declared length → the header-reject path drains the
        # stream (which stalls) under the timeout bound → 413.
        stub = _StubRequest()
        try:
            await routes_mod._read_bounded_source(stub)
        except HTTPException as e:
            return e.status_code, e.detail
        raise AssertionError("expected 413 HTTPException")

    status_code, detail = run_async(app_with_versions, _call)
    assert status_code == 413
    assert "timed out" in detail
