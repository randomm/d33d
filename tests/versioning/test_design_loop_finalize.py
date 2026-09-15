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

from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    create_version,
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
        # ``create_app`` now wires the production hook (issue #9) into
        # ``app.state.run_design_loop``; the 503 path requires the seam
        # to be explicitly unset.
        app_with_versions.state.run_design_loop = None
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 503


def test_finalize_production_seam_supplies_full_kwargs(app_with_versions):
    """(CRITICAL regression) The FINALIZE route, with the PRODUCTION-shape
    seam (``(app, **kwargs)`` — the signature of
    ``d33d.app._build_production_design_loop``), must call the loop with
    the full design-loop kwargs contract — ``photo``, ``stated_dims``,
    ``render_fn``, ``llm_fn``, ``model``, ``prompt_version`` and a
    non-empty ``request`` — not zero kwargs (the old call was a bare
    ``run_loop()`` whose missing mandatory kwargs escaped the route's
    bounded exception set as an unclassified ``TypeError`` and left the
    failures.jsonl hook an empty ``request`` that
    ``FailureEvent(min_length=1)`` would silently drop).

    The loop is mocked to capture the kwargs and return a pass result,
    so the test exercises the route's seam branch without a live LLM,
    Docker render, or catalogue.
    """

    captured: dict = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # The real production closure is async; a stub of the same
        # production shape ((app, **kwargs)) captures the kwargs and
        # returns a pass result — the route must await it and call it
        # with the full design-loop kwargs contract, not zero kwargs.
        async def _loop(app, **kwargs):
            captured.update(kwargs)
            return _StubResult("pass", {"W": 10})

        app_with_versions.state.run_design_loop = _loop
        return await client.post(
            f"/api/projects/{pid}/finalize",
            json={"request": "make a 20mm wide bracket"},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    for key in (
        "photo",
        "stated_dims",
        "render_fn",
        "llm_fn",
        "model",
        "prompt_version",
    ):
        assert key in captured, f"missing design-loop kwarg {key!r}"
    assert captured["stated_dims"] == (0.0, 0.0, 0.0)
    assert callable(captured["render_fn"])
    assert callable(captured["llm_fn"])
    assert isinstance(captured["prompt_version"], str) and captured["prompt_version"]
    # ``request`` is always a non-empty string (empty would make the
    # failures.jsonl hook silently drop the line for an exhausted loop).
    assert isinstance(captured["request"], str) and captured["request"]
    assert captured["request"] == "make a 20mm wide bracket"


def test_production_seam_forwards_bbox_fn_to_real_loop(app_with_versions, monkeypatch):
    """(CRITICAL #54 regression) The REAL production closure
    (``d33d.app._build_production_design_loop`` — the hook-wrapped loop that
    the chat route calls via ``app.state.run_design_loop``) must FORWARD
    the ``bbox_fn`` it receives in ``**kwargs`` through to the real
    ``run_design_loop_async``. Without the forward, the bbox gate can never
    score (``bbox=None`` at ``design_loop.py:489``), no candidate can score
    the bbox bit, and every production loop exhausts on
    ``bbox_out_of_tolerance`` — the exact defect the chat wiring's
    ``bbox_fn`` kwarg exists to fix.

    The catalogue/probe/llm/render deps are stubbed so the closure runs
    without a live LLM, Docker render, or model config; the real
    ``run_design_loop_async`` is monkeypatched to capture the kwargs it is
    actually handed (the ``bbox_fn`` included) and return a pass result.
    """
    import types

    from d33d import design_loop as _design_loop_mod
    from d33d.app import _build_production_design_loop
    from d33d.config import catalogue as _catalogue_mod
    from d33d.config import probes as _probes_mod
    from d33d.config import resolve as _resolve_mod
    from d33d.design_loop_events import bbox_from_render

    captured: dict = {}

    async def _fake_real_run(**kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _fake_probe(base_url, model_id, api_key, request_factory):
        return None

    class _NoopLLM:
        async def __call__(self, *a, **k):
            return ""

    monkeypatch.setattr(
        _catalogue_mod,
        "load_catalogue",
        lambda p: types.SimpleNamespace(
            providers={"p": types.SimpleNamespace(key="stub")}
        ),
    )
    monkeypatch.setattr(
        _resolve_mod,
        "resolve_model",
        lambda cat, role: types.SimpleNamespace(
            entry=types.SimpleNamespace(model="stub"),
            provider=types.SimpleNamespace(base="http://stub"),
        ),
    )
    monkeypatch.setattr(_probes_mod, "probe_capabilities", _fake_probe)
    monkeypatch.setattr(_design_loop_mod, "make_llm_fn", lambda cat, f, c: _NoopLLM())
    monkeypatch.setattr(_design_loop_mod, "run_design_loop_async", _fake_real_run)

    async def _call(client):
        app_with_versions.state.failures_jsonl_path = str(
            app_with_versions.state.catalogue_path.parent / "failures.jsonl"
        )
        closure = _build_production_design_loop()
        return await closure(
            app=app_with_versions,
            photo="data:image/png;base64,x",
            chat_history=("hi",),
            stated_dims=(1.0, 2.0, 3.0),
            render_fn=None,
            llm_fn=None,
            bbox_fn=bbox_from_render,
            request="make a box",
        )

    result = run_async(app_with_versions, _call)
    assert result.status == "pass"
    # The real loop received the chat wiring's bbox_fn — not None.
    assert "bbox_fn" in captured, "bbox_fn not forwarded to run_design_loop_async"
    assert captured["bbox_fn"] is bbox_from_render


def test_finalize_production_seam_no_body_supplies_nonempty_request(app_with_versions):
    """(HIGH 3 regression) A FINALIZE with an empty body must still give
    the hook a non-empty ``request`` — the route falls back to a
    deterministic placeholder so ``FailureEvent.request`` (``min_length=1``)
    validates and the failures.jsonl line is never silently dropped."""

    captured: dict = {}

    async def _loop_awaited(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop_awaited
        return await client.post(f"/api/projects/{pid}/finalize", json={})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert isinstance(captured.get("request"), str) and captured["request"]


def test_finalize_async_loop_result_is_awaited(app_with_versions):
    """The loop may be async (the real run_design_loop is); the route must
    await it — a sync-only path would return the coroutine object and blow
    up on ``.status``.

    Seams the "no nested asyncio.run" contract: inside the FastAPI
    request handler (a coroutine running on the server's event loop)
    the injected loop is AWAITED inside the running loop — the result
    comes back fully resolved. If the route (or the seam) bridged via
    a nested ``asyncio.run``, that call would raise ``RuntimeError(
    'cannot be called from a running event loop')`` inside the
    request — an uncaught 500, never the 201 + resolved params
    asserted here. A sync-only path would instead leave an un-awaited
    coroutine and blow up on ``.status`` (also never 201)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        async def _loop():
            # Runs inside the server's event loop: ``get_running_loop``
            # only succeeds here because the coroutine is driven by the
            # FastAPI request cycle, not a nested ``asyncio.run`` (which
            # would raise RuntimeError the moment it executes).
            import asyncio as _a

            _a.get_running_loop()  # running loop present → awaited
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


# ---------------------------------------------------------------------------
# (6) The region-edit route (issue #68) — POST /api/projects/{id}/region-edits
#
# The route drives the injected design loop with the composed region-edit
# request text (instruction prefixed with the resolved module_ids + view_id),
# the marked PNG as the photo, the latest version's W/D/H as stated_dims,
# and an EMPTY chat_history (a scoped directive, not a chat turn). The 202
# body mirrors /chat ({project_id, status: "accepted"}); the version arrives
# only via the SSE stream's version-created frame.
# ---------------------------------------------------------------------------

#: A minimal valid 1x1 PNG, base64-encoded (same as tests/test_app.py's
#: region-edit fixture — small enough for the 5 MB cap path without a real
#: render artifact).
_REGION_EDIT_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _region_edit_body() -> dict:
    return {
        "module_ids": ["curl_3", "curl_4"],
        "view_id": "front",
        "marked_png_base64": _REGION_EDIT_PNG_BASE64,
        "polygon": [
            {"x": 10.0, "y": 10.0},
            {"x": 50.0, "y": 10.0},
            {"x": 30.0, "y": 40.0},
        ],
        "instruction": "open up this spiral, it's too tight to print",
    }


def test_region_edit_returns_202_accepted_and_records_full_kwargs(
    app_with_versions,
):
    """A valid region edit returns 202 {project_id, status: accepted}
    (mirroring /chat — no deferred field, no module_ids/view_id echo) and
    drives the injected design loop with the FULL kwargs contract:
    the composed request text (instruction prefixed with module_ids +
    view_id, non-empty), the marked PNG as a data URI (NOT the stored
    photo), stated_dims from the latest version's W/D/H ((0,0,0) for a
    fresh project), an EMPTY chat_history (a scoped directive, not a
    chat turn — even when the project has prior transcripts), and a
    callable bbox_fn."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10}, scad="W = 10; cube([W]);")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        source = app_with_versions.state.event_sources.get(pid)
        async for _event, _data in source:
            if _event in ("done", "error"):
                break
        return pid, r, source

    pid, r, source = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"project_id": pid, "status": "accepted"}
    assert source is not None, "event source not registered before 202 response"
    # Full kwargs contract — the same shape the /chat adapter asserts.
    for key in ("photo", "stated_dims", "bbox_fn", "request", "chat_history"):
        assert key in captured, f"missing design-loop kwarg {key!r}"
    # request: non-empty instruction text prefixed with the resolved
    # module_ids + view_id (the failures.jsonl hook's FailureEvent.request).
    assert captured["request"], "request kwarg must be non-empty"
    assert "curl_3" in captured["request"]
    assert "curl_4" in captured["request"]
    assert "front" in captured["request"]
    assert "open up this spiral, it's too tight to print" in captured["request"]
    # photo: the marked PNG from the body as a data URI (the vision model
    # sees the marked-up render, not the stored reference photo).
    assert captured["photo"] == f"data:image/png;base64,{_REGION_EDIT_PNG_BASE64}"
    # stated_dims: fresh project (no versions) → (0, 0, 0) so the
    # dimension gate measures rather than fabricates.
    assert captured["stated_dims"] == (0.0, 0.0, 0.0)
    # chat_history: the EMPTY tuple — a region edit is a scoped directive,
    # not a chat turn (the project's transcript is never auto-included).
    assert captured["chat_history"] == ()
    assert callable(captured["bbox_fn"])


def test_region_edit_stated_dims_from_latest_version(app_with_versions):
    """stated_dims is ALWAYS derived from the latest version's W/D/H
    (no client override for region edits) — a project with an existing
    version passes that version's W/D/H, not (0,0,0)."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("exhausted", {})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 12.0, "D": 8.0, "H": 5.0})
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        source = app_with_versions.state.event_sources.get(pid)
        async for _event, _data in source:
            if _event in ("done", "error"):
                break
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] == (12.0, 8.0, 5.0)


def test_region_edit_pass_creates_version_visible_in_get_versions(
    app_with_versions,
):
    """A passing loop creates a version visible via GET /versions (via
    VersionService.create_version, the sole version-creation path) and
    the SSE stream emits the version-created progress frame with the
    version_id."""

    async def _loop(app, **kwargs):
        return _StubResult("pass", {"W": 11, "H": 22}, scad="W = 11; cube([W]);")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/region-edits", json=_region_edit_body())
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return frames, timeline

    frames, timeline = run_async(app_with_versions, _call)
    # A version was created and is visible in GET /versions.
    assert len(timeline) == 1, "no version created on pass"
    assert timeline[0]["name"] == "design"
    assert timeline[0]["params"] == {"W": 11, "H": 22}
    # The version-created progress frame carries the version id.
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    assert vc[0]["version_id"] == timeline[0]["id"]
    # Terminal frame is a done (not an error).
    assert frames[-1][0] == "done"


def test_region_edit_exhausted_emits_error_frame_and_no_version(app_with_versions):
    """An exhausted loop produces NO version and the stream emits a
    terminal error frame (mirroring the finalize/chat contract — no
    spurious version)."""

    async def _loop(app, **kwargs):
        return _StubResult("exhausted", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/region-edits", json=_region_edit_body())
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return frames, timeline

    frames, timeline = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert event_names[-1] == "error", f"no terminal error frame: {event_names}"
    assert "error" in event_names, "no error frame emitted for exhausted loop"
    assert timeline == [], "exhausted loop must not create a version"


def test_region_edit_unwired_loop_returns_202_and_terminates_with_error(
    app_with_versions,
):
    """The not-wired case mirrors /chat exactly: the route still returns
    202 (no synchronous 503) and the stream emits the adapter's terminal
    error frame ("design loop not wired")."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = None
        r = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        source = app_with_versions.state.event_sources.get(pid)
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, f"expected 202, got {r.status_code}: {r.text}"
    assert r.json()["status"] == "accepted"
    assert frames[-1][0] == "error"
    assert "not wired" in frames[-1][1]["message"]


def test_region_edit_409_while_in_flight(app_with_versions):
    """A second region edit while a design loop is in flight is 409
    (the same per-project guard as /chat — the event source is a single
    async generator, so a second concurrent drive would lose frames)."""
    import asyncio as _a

    release = _a.Event()

    async def _loop(app, **kwargs):
        await release.wait()
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r1 = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        release.set()
        return r1, r2

    r1, r2 = run_async(app_with_versions, _call)
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 409, r2.text


# ---------------------------------------------------------------------------
# (5) The chat route (issue #54) — POST /api/projects/{id}/chat
# ---------------------------------------------------------------------------


class _StubRender:
    """Duck-type of ``RenderResult`` for chat-route tests: ``stl`` + the
    6-element ``views`` tuple (``render_worker.py`` VIEWS contract).
    ``render_artifact_dir`` mirrors the REAL declared ``RenderResult``
    field name (issue #72) — the adapter reads it as a real attribute; a
    stub carrying any other name would exercise a dead read, which is
    exactly the bug issue #89 fixes, so the stub's ctor takes the real
    field name only.
    """

    def __init__(
        self,
        stl: str | None = None,
        views: tuple[str, ...] = (),
        render_artifact_dir: str | None = None,
    ) -> None:
        self.stl = stl
        self.views = views
        # Always defined (None when unset) so the reader's attribute read
        # cannot raise AttributeError on a stub — mirroring the declared
        # ``RenderResult`` field (always present, ``str | None``).
        self.render_artifact_dir = render_artifact_dir


def _write_durable_artifacts(root, *, partial_views: bool = False):
    """Write a live durable artifact directory (the issue #72 seam): a real
    on-disk ``model.stl`` + the 6 fixed VIEWS view PNGs under ``root``
    (the worker's per-render uuid directory), returning the directory path.
    ``partial_views=True`` writes only 5 of the 6 PNGs (the partial-omit
    edge case — the frame must then omit ``views`` entirely).

    A UNIQUE subdirectory per call — each test's fixture is built ONCE
    (module-level fixture collection happens before ``run_async``'s lifespan
    enters ``tmp_path``/isolation), so a fixed ``artifacts/`` name would be
    shared across tests in the same pytest session and clobbered.
    """
    import uuid

    from d33d.render_worker import VIEWS

    d = root / f"artifacts-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    (d / "model.stl").write_bytes(b"\x84\xab\x50\x53fake-stl-bytes")
    for name, _cam in VIEWS:
        if partial_views and name == "view_05_iso.png":
            continue  # 5 of 6 — the partial-omit edge case
        (d / name).write_bytes(b"\x89PNG-fake-view-bytes")
    return str(d)


class _StubBest:
    """Duck-type of the design loop's best candidate (carries the named
    parameters + the generated SCAD source)."""

    def __init__(self, params: dict, scad: str = "", render=None) -> None:
        self.params = params
        self.scad_source = scad
        self.render = render


class _StubResult:
    def __init__(self, status: str, params: dict, scad: str = "", render=None) -> None:
        self.status = status
        self.best = _StubBest(params, scad, render)
        self.failure_reason = None if status == "pass" else "bbox_out_of_tolerance"


def test_chat_empty_message_is_422(app_with_versions):
    """A whitespace-only message is 422 (field_validator)."""

    async def _call(client):
        proj = await create_project(client)
        return await client.post(
            f"/api/projects/{proj['id']}/chat", json={"message": "   "}
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 422


def test_chat_404_for_missing_project(app_with_versions):
    """A chat to a non-existent project is 404."""

    async def _call(client):
        return await client.post("/api/projects/999999/chat", json={"message": "hi"})

    r = run_async(app_with_versions, _call)
    assert r.status_code == 404


def test_chat_returns_202_accepted_and_registers_event_source(app_with_versions):
    """A valid chat message returns 202 {status: accepted} and the event
    source is registered synchronously BEFORE the 202 response (the SSE
    stream must not terminate on 'no active stream')."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        # Simulate a slow loop — the 202 must return before this completes.
        import asyncio as _a

        await _a.sleep(0.05)
        return _StubResult("pass", {"W": 10}, scad="W = 10; cube([W]);")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make a 10mm box"},
        )
        # The event source must be registered synchronously (before the
        # 202 response is returned) — the SSE stream reads it at request
        # time and would otherwise terminate on "no active stream".
        source = app_with_versions.state.event_sources.get(pid)
        # Drive the generator to completion (the SSE endpoint is the sole
        # driver in production; here we do it directly to trigger the
        # design-loop call and capture kwargs).
        async for _event, _data in source:
            if _event in ("done", "error"):
                break
        return r, source

    r, source = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"status": "accepted"}
    assert source is not None, "event source not registered before 202 response"
    # The loop was called with the full kwargs contract (photo, stated_dims,
    # bbox_fn, request — the same shape the finalize seam supplies).
    for key in ("photo", "stated_dims", "bbox_fn", "request"):
        assert key in captured, f"missing design-loop kwarg {key!r}"
    assert captured["stated_dims"] == (0.0, 0.0, 0.0)
    assert callable(captured["bbox_fn"])
    assert captured["request"] == "make a 10mm box"


def test_chat_409_while_in_flight(app_with_versions):
    """A second chat while a design loop is in flight is 409."""
    import asyncio as _a

    release = _a.Event()

    async def _loop(app, **kwargs):
        await release.wait()
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r1 = await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        # The first loop is in flight (awaiting release) — the flag is set.
        inflight = app_with_versions.state.design_loop_inflight
        assert pid in inflight, "in-flight flag not set"
        r2 = await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        release.set()
        return r1, r2

    r1, r2 = run_async(app_with_versions, _call)
    assert r1.status_code == 202, r1.text
    assert r2.status_code == 409, r2.text


def test_chat_flag_released_after_loop_completes(app_with_versions):
    """The in-flight flag is cleared after the loop completes (pass path)."""

    async def _loop(app, **kwargs):
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        # Consume the SSE stream (the SSE endpoint is the sole driver of
        # the generator; the flag is cleared in its finally when the
        # generator is exhausted).
        async with client.stream("GET", f"/api/stream/{pid}") as resp:
            async for _chunk in resp.aiter_text():
                pass
        return pid in app_with_versions.state.design_loop_inflight

    still_inflight = run_async(app_with_versions, _call)
    assert still_inflight is False, "in-flight flag leaked after loop completion"


def test_chat_exhausted_emits_error_frame_and_no_version(app_with_versions):
    """An exhausted loop emits a terminal error frame and does NOT create a
    version."""
    import asyncio as _a

    async def _loop(app, **kwargs):
        return _StubResult("exhausted", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        # Collect the frames from the event source.
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        # Wait for the flag to be released.
        for _ in range(50):
            if pid not in app_with_versions.state.design_loop_inflight:
                break
            await _a.sleep(0.01)
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return frames, timeline

    frames, timeline = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert "error" in event_names, "no error frame emitted for exhausted loop"
    # The terminal frame is an error (not a done).
    assert event_names[-1] == "error"
    # No version was created.
    assert timeline == []


def test_chat_pass_creates_version_and_emits_token_and_done(app_with_versions):
    """A passing loop creates a version, emits a token frame (the SCAD
    source) and a done frame."""
    import asyncio as _a

    async def _loop(app, **kwargs):
        return _StubResult(
            "pass", {"W": 10, "H": 20}, scad="W = 10; H = 20; cube([W, H, 1]);"
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make a box 10 wide and 20 high"},
        )
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        for _ in range(50):
            if pid not in app_with_versions.state.design_loop_inflight:
                break
            await _a.sleep(0.01)
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return frames, timeline

    frames, timeline = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    data_by_event = {}
    for event, data in frames:
        data_by_event.setdefault(event, []).append(data)
    # A version was created.
    assert len(timeline) == 1, "no version created on pass"
    assert timeline[0]["name"] == "design"
    assert timeline[0]["params"] == {"W": 10, "H": 20}
    # The version-created progress frame carries the version id.
    version_created = data_by_event.get("progress", [])
    vc = [d for d in version_created if d.get("step") == "version-created"]
    assert vc, "no version-created progress frame"
    assert vc[0]["version_id"] == timeline[0]["id"]
    # No render on the stub result → the frame carries no viewer fields
    # (stl_data_uri / views omitted entirely, not null, no bogus paths).
    assert "stl_data_uri" not in vc[0], "stl_data_uri must be omitted without a render"
    assert "views" not in vc[0], "views must be omitted without a render"
    # A token frame carries the SCAD source.
    assert "token" in data_by_event, "no token frame"
    assert data_by_event["token"][0]["text"] == "W = 10; H = 20; cube([W, H, 1]);"
    # A done frame is emitted.
    assert "done" in data_by_event, "no done frame"
    # The terminal frame is a done (not an error).
    assert event_names[-1] == "done"


def test_chat_pass_real_render_result_frame_carries_stl_and_views(
    app_with_versions, tmp_path
):
    """(issue #89 decisive test) A REAL frozen ``RenderResult`` — the exact
    production type, constructed with the declared ``render_artifact_dir``
    field pointing at a durable directory holding the real fixture STL plus
    the 6 VIEWS PNGs — fed through the real frame-assembly path yields a
    version-created frame whose ``stl_data_uri`` base64-decodes to EXACTLY
    the on-disk STL bytes and whose ``views`` maps all 6 VIEWS names to
    ``data:image/png;base64,`` URIs with byte equality against the planted
    bytes. No stub, no byte cache, no hand-constructed frames — a hand-
    built reader input is exactly what let the dead-``getattr`` bug survive
    #69 and #72. No SCAD leaks into any progress frame (the token frame
    stays the sole SCAD owner).
    """
    import base64 as _b64

    from d33d.render_worker import VIEWS

    async def _call(client):
        # Production-shaped durable dir: the real box_20mm.stl fixture bytes
        # as model.stl + the 6 VIEWS PNGs (the _write_durable_artifacts
        # layout, with the golden STL fixture in place of the placeholder).
        from pathlib import Path

        artifact_dir = _write_durable_artifacts(tmp_path)
        fixture = Path("tests/fixtures/stl/box_20mm.stl")
        (Path(artifact_dir) / "model.stl").write_bytes(fixture.read_bytes())

        async def _loop(app, **kwargs):
            render = RenderResult(
                ok=True,
                exit_code=0,
                duration_ms=100,
                error_class="ok",
                stderr="",
                stl=str(Path(artifact_dir) / "model.stl"),
                csg=str(Path(artifact_dir) / "model.csg"),
                views=tuple(
                    str(Path(artifact_dir) / name) for name, _cam in VIEWS
                ),
                render_artifact_dir=artifact_dir,
            )
            return _StubResult(
                "pass", {"W": 10, "H": 20}, scad="W = 10; cube([W]);", render=render
            )

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make a box"},
        )
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    data_by_event = {}
    for event, data in frames:
        data_by_event.setdefault(event, []).append(data)
    vc = [
        d
        for d in data_by_event.get("progress", [])
        if d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    # stl_data_uri decodes to EXACTLY the on-disk STL bytes (the fixture).
    from pathlib import Path as _Path

    fixture = _Path("tests/fixtures/stl/box_20mm.stl")
    stl_uri = vc[0].get("stl_data_uri")
    assert stl_uri is not None, "stl_data_uri missing on version-created frame"
    assert stl_uri.startswith("data:") and ";base64," in stl_uri
    payload = _b64.b64decode(stl_uri.split(";base64,", 1)[1])
    assert payload == fixture.read_bytes(), "STL bytes mismatch vs on-disk fixture"
    # views: all 6 VIEWS names, each a PNG data URI with byte equality.
    view_map = vc[0].get("views")
    expected_view_names = {name for name, _cam in VIEWS}
    assert set(view_map.keys()) == expected_view_names, (
        f"views keys {sorted(view_map)} != expected {sorted(expected_view_names)}"
    )
    for name in view_map:
        uri = view_map[name]
        assert uri.startswith("data:image/png;base64,"), f"view URI: {uri[:40]}"
        decoded = _b64.b64decode(uri.split(";base64,", 1)[1])
        assert decoded == b"\x89PNG-fake-view-bytes", f"view bytes mismatch for {name}"
    # No SCAD in any progress frame — the token frame is the sole owner.
    for d in data_by_event.get("progress", []):
        assert "W = 10; cube([W]);" not in str(d), "SCAD leaked into a progress frame"
    # The token frame still carries the SCAD source.
    assert data_by_event["token"][0]["text"] == "W = 10; cube([W]);"


def test_render_result_reader_reads_declared_fields():
    """(issue #89 rename guard) Every attribute the frame-assembly path
    reads off the render object is a DECLARED ``RenderResult`` field —
    asserted via ``dataclasses.fields`` and a source-level AST check of
    the reader, so a future rename to a non-existent name fails LOUDLY
    here instead of silently yielding ``None`` (the exact bug: the reader
    once ``getattr``-ed a non-existent attribute name and every real
    frame shipped without an STL).
    """
    import ast
    import dataclasses
    import inspect

    from d33d.design_loop_events import _artifact_bytes_from_path

    declared = {f.name for f in dataclasses.fields(RenderResult)}
    for name in ("render_artifact_dir", "stl", "views"):
        assert name in declared, f"{name!r} is not a declared RenderResult field"
    # AST check: every ``render.<attr>`` read in the reader must target a
    # declared field (a misspelled getattr cannot be caught by a frozen
    # dataclass read at runtime — it returns None silently, which is how
    # this bug shipped).
    source = inspect.getsource(_artifact_bytes_from_path)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "render"
        ):
            assert node.attr in declared, (
                f"reader references render.{node.attr!r}, which is not a "
                f"declared RenderResult field"
            )


def test_chat_pass_dead_render_paths_omit_fields(app_with_versions, tmp_path):
    """A pass whose best render points at torn-down tempdir paths (the
    production seam — bytes dead by frame time) still passes: the frame
    omits stl_data_uri/views (never bogus paths), and the stream still
    terminates with the terminal done frame.
    """

    async def _call(client):
        # Dead paths: a tempdir the render worker would have torn down.
        import shutil

        dead = tmp_path / "torn-down"
        dead.mkdir()
        stl = dead / "model.stl"
        stl.write_bytes(b"gone")
        views = []
        for i in range(6):
            p = dead / f"view_{i:02d}.png"
            p.write_bytes(b"gone")
            views.append(str(p))
        render = _StubRender(stl=str(stl), views=tuple(views))
        shutil.rmtree(dead)  # simulate the worker's torn-down tempdir

        async def _loop(app, **kwargs):
            return _StubResult(
                "pass", {"W": 10}, scad="W = 10; cube([W]);", render=render
            )

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert event_names[-1] == "done", f"no terminal done frame: {event_names}"
    for _event, data in frames:
        if data.get("step") == "version-created":
            assert "stl_data_uri" not in data, (
                "stl_data_uri must be omitted for dead paths"
            )
            assert "views" not in data, "views must be omitted for dead paths"


def test_chat_pass_live_render_artifact_dir_emits_stl_and_views(
    app_with_versions, tmp_path
):
    """(issue #72 emit path) A pass whose best render carries a DURABLE
    artifact directory — ``render_artifact_dir`` (the per-render directory
    the worker persisted to inside its tempdir with-block — live on disk
    when the frame is built) emits
    ``stl_data_uri`` + all 6 ``views`` on the version-created frame — the
    adapter reads the persisted files, not the dead tempdir paths the
    render's ``stl``/``views`` point at.
    """
    import base64 as _b64

    from d33d.render_worker import VIEWS

    async def _call(client):
        # The durable artifacts survive the worker's tempdir teardown — a
        # real on-disk directory with a non-empty STL + the 6 view PNGs.
        artifact_dir = _write_durable_artifacts(tmp_path)
        # Dead tempdir paths: the worker's with-block has exited, so the
        # render's own stl/views paths are gone (only the durable copy is
        # reachable — the adapter must read the durable source).
        dead = tmp_path / "torn-down"
        dead.mkdir()
        dead_stl = dead / "model.stl"
        dead_stl.write_bytes(b"dead")
        dead_views = []
        for i in range(6):
            p = dead / f"view_{i:02d}.png"
            p.write_bytes(b"dead")
            dead_views.append(str(p))

        async def _loop(app, **kwargs):
            import shutil as _shutil

            render = _StubRender(
                stl=str(dead_stl),
                views=tuple(dead_views),
                render_artifact_dir=artifact_dir,
            )
            _shutil.rmtree(dead)  # simulate the worker's torn-down tempdir
            return _StubResult(
                "pass", {"W": 10, "H": 20}, scad="W = 10; cube([W]);", render=render
            )

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    data_by_event = {}
    for event, data in frames:
        data_by_event.setdefault(event, []).append(data)
    vc = [
        d
        for d in data_by_event.get("progress", [])
        if d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    # The frame's bytes come from the DURABLE artifacts, not the dead
    # tempdir paths (the dead paths would have read b"dead" / b"gone").
    stl_uri = vc[0].get("stl_data_uri")
    assert stl_uri is not None, "stl_data_uri missing on version-created frame"
    assert stl_uri.startswith("data:") and ";base64," in stl_uri
    payload = _b64.b64decode(stl_uri.split(";base64,", 1)[1])
    assert payload == b"\x84\xab\x50\x53fake-stl-bytes", (
        "STL bytes must come from the durable artifact dir, not the dead tempdir"
    )
    view_map = vc[0].get("views")
    expected_view_names = {name for name, _cam in VIEWS}
    assert set(view_map.keys()) == expected_view_names, (
        f"views keys {sorted(view_map)} != expected {sorted(expected_view_names)}"
    )
    for name in view_map:
        uri = view_map[name]
        assert uri.startswith("data:image/png;base64,"), f"view URI: {uri[:40]}"
        decoded = _b64.b64decode(uri.split(";base64,", 1)[1])
        assert decoded == b"\x89PNG-fake-view-bytes", f"view bytes mismatch for {name}"


def test_chat_pass_partial_durable_views_omit_views_field(app_with_versions, tmp_path):
    """(issue #72 partial edge) A durable artifact directory that persisted
    only 5 of the 6 view PNGs (a failed copy — the worker's partial-harvest
    case) emits ``stl_data_uri`` (the STL is fine) but omits ``views``
    ENTIRELY: a consumer cannot distinguish a 5-of-6 map from a complete
    one.
    """
    import base64 as _b64

    async def _call(client):
        artifact_dir = _write_durable_artifacts(tmp_path, partial_views=True)

        async def _loop(app, **kwargs):
            render = _StubRender(render_artifact_dir=artifact_dir)
            return _StubResult(
                "pass", {"W": 10}, scad="W = 10; cube([W]);", render=render
            )

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    # The STL is complete → stl_data_uri is present (the 5-of-6 case only
    # drops the views, never the STL).
    stl_uri = vc[0].get("stl_data_uri")
    assert stl_uri is not None, "stl_data_uri must be present for a complete STL"
    assert (
        _b64.b64decode(stl_uri.split(";base64,", 1)[1])
        == b"\x84\xab\x50\x53fake-stl-bytes"
    )
    # 5 of 6 views → "no views" (the frame omits the field entirely rather
    # than emit a map a consumer cannot distinguish from a complete one).
    assert "views" not in vc[0], "partial (5-of-6) views must omit the field entirely"


def test_chat_pass_dead_render_artifact_dir_omits_fields(
    app_with_versions, tmp_path
):
    """(issue #89) A render whose ``render_artifact_dir`` is set but
    UNREACHABLE (the directory was deleted out-of-band — a project wiped
    mid-flight) degrades exactly as a missing source: the frame OMITS
    ``stl_data_uri`` and ``views`` (never a bogus path, never an
    empty-string data URI), and the stream still terminates with the
    terminal done frame. The setattr byte-cache fallback that this test
    used to exercise (``cache_render_artifact_bytes``) is deleted — it
    raised ``FrozenInstanceError`` on the frozen ``RenderResult`` and never
    worked in production.
    """

    async def _call(client):
        # A durable dir that will be deleted before the frame is built.
        durable = tmp_path / "deleted-durable"
        durable.mkdir()

        async def _loop(app, **kwargs):
            import shutil as _shutil

            render = _StubRender(render_artifact_dir=str(durable))
            _shutil.rmtree(durable)  # durable dir deleted out-of-band
            return _StubResult(
                "pass", {"W": 10}, scad="W = 10; cube([W]);", render=render
            )

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert event_names[-1] == "done", f"no terminal done frame: {event_names}"
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    # Dead durable dir → both fields omitted (never null, never an
    # empty-string data URI), nothing raised.
    assert "stl_data_uri" not in vc[0], "stl_data_uri must be omitted for a dead dir"
    assert "views" not in vc[0], "views must be omitted for a dead dir"


def test_chat_exhausted_emits_no_stl_or_views_fields(app_with_versions):
    """An exhausted run terminates with the terminal error frame and emits
    NO stl_data_uri or views fields — the {progress,token,done,error}
    schema is unchanged (no new event kind)."""

    async def _call(client):
        async def _loop(app, **kwargs):
            return _StubResult("exhausted", {"W": 10})

        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert event_names[-1] == "error", f"no terminal error frame: {event_names}"
    for event, data in frames:
        assert "stl_data_uri" not in data, f"stl_data_uri in {event} frame"
        assert "views" not in data, f"views in {event} frame"
    # Only the existing event kinds — no new event kind.
    assert set(event_names) <= {"progress", "token", "done", "error"}


def test_chat_supplies_real_bbox_fn(app_with_versions):
    """The chat wiring supplies a real bbox_fn (per-axis extents from the
    render's artifacts) — the finalize seam's absence of one is the defect
    this ticket fixes."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        # Drive the generator to trigger the design-loop call.
        source = app_with_versions.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break

    run_async(app_with_versions, _call)
    assert "bbox_fn" in captured, "bbox_fn not supplied by chat wiring"
    assert callable(captured["bbox_fn"]), "bbox_fn is not callable"
    # The bbox_fn is the real implementation (not None, not a stub).
    from d33d.design_loop_events import bbox_from_render

    assert captured["bbox_fn"] is bbox_from_render


def test_chat_stated_dims_from_body(app_with_versions):
    """stated_dims from the request body is passed through to the loop."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "hi", "stated_dims": [20.0, 30.0, 40.0]},
        )
        source = app_with_versions.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break

    run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (20.0, 30.0, 40.0)


def test_chat_chat_history_from_body(app_with_versions):
    """chat_history from the request body is passed through to the loop."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "hi", "chat_history": ["first", "second"]},
        )
        source = app_with_versions.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break

    run_async(app_with_versions, _call)
    assert captured["chat_history"] == ("first", "second")


def test_chat_photo_data_uri_from_project(app_with_versions, tmp_path):
    """The photo is read from the project's source_photo_path and emitted
    as a data URI with MIME from the file extension."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        # Create a PNG file and set it as the project's photo.
        png_path = tmp_path / "photo.png"
        # 1x1 PNG
        png_path.write_bytes(
            bytes.fromhex(
                "89504e470d0a1a0a0000000d49484452000000010000000108060000"
                "001f15c4890000000d49444154789c626001000000050001"
                "0d0a2fbc1e0000000049454e44ae426082"
            )
        )
        app_with_versions.state.conn.update_project(
            pid, source_photo_path=str(png_path)
        )
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break

    run_async(app_with_versions, _call)
    photo = captured.get("photo")
    assert photo is not None, "photo not supplied"
    assert photo.startswith("data:image/png;base64,"), f"wrong MIME: {photo[:50]}"


def test_chat_missing_photo_falls_back_to_empty_constant(app_with_versions):
    """A project with no photo gets the fixed 1x1 transparent-PNG data URI
    constant (never None)."""
    from d33d.design_loop_events import EMPTY_PHOTO_DATA_URI

    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("pass", {"W": 10})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break

    run_async(app_with_versions, _call)
    assert captured.get("photo") == EMPTY_PHOTO_DATA_URI


def test_chat_project_deleted_mid_flight_emits_error(app_with_versions):
    """Project deleted mid-flight → terminal error frame + flag release,
    not a 500."""

    class _DeleteMidLoop:
        """A loop that deletes the project before completing."""

        def __init__(self, app) -> None:
            self._app = app

        def __call__(self, **kwargs):
            self._app.state.conn.delete_project(42)  # will fail (wrong id)
            raise LookupError("project deleted")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # A loop that raises LookupError (simulating project deletion).
        async def _loop(app, **kwargs):
            raise LookupError(f"project {pid} not found")

        app_with_versions.state.run_design_loop = _loop
        r = await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        # Consume the SSE stream (the SSE endpoint is the sole driver;
        # the flag is cleared in its finally when the generator is
        # exhausted or a terminal frame is reached).
        sse_chunks = []
        async with client.stream("GET", f"/api/stream/{pid}") as resp:
            async for _chunk in resp.aiter_text():
                sse_chunks.append(_chunk)
        # Parse SSE frames from the raw text.
        frames = []
        for chunk in sse_chunks:
            for line in chunk.split("\n"):
                if line.startswith("event: "):
                    frames.append((line[len("event: ") :], {}))
        return r, frames, pid in app_with_versions.state.design_loop_inflight

    r, frames, still_inflight = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    event_names = [f[0] for f in frames]
    assert "error" in event_names, "no error frame for deleted project"
    assert event_names[-1] == "error"
    assert still_inflight is False, "flag not released after error"


def test_sse_wide_catch_emits_terminal_error():
    """The SSE stream's broad catch emits a terminal error frame when the
    event source raises an unhandled exception (e.g. KeyError)."""
    import asyncio as _a

    from httpx import ASGITransport, AsyncClient

    from d33d.app import create_app

    async def _call():
        app = create_app(
            ":memory:",
            master_key_path="/tmp/pi-rukas-test-master.key",
            catalogue_path="/tmp/pi-rukas-test-models.yaml",
        )
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                create_r = await client.post(
                    "/api/projects", json={"name": "WideCatch"}
                )
                pid = create_r.json()["id"]

                async def _bad_source():
                    raise KeyError("simulated adapter failure")

                app.state.event_sources[pid] = _bad_source()
                async with client.stream("GET", f"/api/stream/{pid}") as resp:
                    chunks = []
                    async for chunk in resp.aiter_text():
                        chunks.append(chunk)
                    raw = "".join(chunks)
        return raw

    raw = _a.run(_call())
    # The stream must end with a terminal error frame (not hang, not die
    # silently).
    assert "event: error" in raw, f"no terminal error frame: {raw!r}"


def test_photo_data_uri_jpeg(app_with_versions, tmp_path):
    """A JPEG photo gets image/jpeg MIME (from the .jpg extension)."""
    from d33d.design_loop_events import photo_data_uri

    jpg_path = tmp_path / "photo.jpg"
    jpg_path.write_bytes(b"\xff\xd8\xff\xdbfakejpegdata")
    uri = photo_data_uri(str(jpg_path))
    assert uri.startswith("data:image/jpeg;base64,"), f"wrong MIME: {uri[:50]}"


def test_photo_data_uri_missing_file_returns_constant():
    """A missing file (path exists in row but file deleted out-of-band)
    falls back to the fixed 1x1 transparent-PNG constant, never None."""
    from d33d.design_loop_events import EMPTY_PHOTO_DATA_URI, photo_data_uri

    uri = photo_data_uri("/nonexistent/path/photo.png")
    assert uri == EMPTY_PHOTO_DATA_URI


def test_photo_data_uri_none_returns_constant():
    """None (no photo) returns the fixed constant, never None."""
    from d33d.design_loop_events import EMPTY_PHOTO_DATA_URI, photo_data_uri

    uri = photo_data_uri(None)
    assert uri == EMPTY_PHOTO_DATA_URI


def test_chat_render_runs_off_the_event_loop(app_with_versions):
    """The spec's ``asyncio.to_thread`` acceptance criterion, made testable.

    The design loop's SYNC ``render_fn`` (the multi-minute Docker
    ``subprocess.run`` in ``render_for_design_loop``) must run on a worker
    thread, NOT the event loop — otherwise one slow render stalls every
    concurrent SSE stream and API handler on the app. The adapter implements
    this via ``asyncio.to_thread(_run_in_loop, raw)``: the loop coroutine is
    driven by ``asyncio.run`` (a fresh loop) inside a worker thread, so the
    entire design loop — the blocking sync render AND the LLM's awaits —
    runs off the app's event loop.

    This test drives the real adapter (``run_design_loop_with_events``)
    with a loop whose sync render records the thread id it runs on and
    whose async LLM records the same thread id. After the loop completes,
    both the render's and the LLM's thread ids must EQUAL each other (both
    on the worker thread) and DIFFER from the event loop's thread id (the
    render is off the loop). A naive ``await raw`` (the prior implementation)
    would fail: the render's thread id would equal the event-loop thread id.
    """
    import threading

    from d33d.design_loop_events import run_design_loop_with_events

    render_thread_id = {}  # set by the sync render fn
    llm_thread_id = {}  # set by the async llm fn

    class _Result:
        status = "pass"
        best = _StubBest({"W": 10}, scad="W = 10; cube([W]);")
        failure_reason = None

    class _Loop:
        """The injected ``app.state.run_design_loop`` seam: mirrors the real
        loop's sync-or-await dispatch — sync render on the calling thread,
        async LLM awaited on the running loop."""

        def __init__(self, app) -> None:
            self._app = app

        def __call__(self, **kwargs):
            async def _run():
                # Sync render — runs on whatever thread drives the
                # coroutine. Under the to_thread implementation this is a
                # worker thread; under a bare ``await`` it would be the
                # event-loop thread.
                render_thread_id["id"] = threading.get_ident()
                # Async LLM — record the thread that drives the running
                # loop. Under ``asyncio.run`` in the worker thread the
                # fresh loop is installed in that thread, so this thread IS
                # the one the LLM awaits on; under a bare ``await`` it is
                # the event-loop thread.
                llm_thread_id["id"] = threading.get_ident()
                return _Result()

            return _run()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _Loop(app_with_versions)
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="hi",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="hi",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        # The event loop's own thread id (this coroutine runs on it).
        event_loop_thread = threading.get_ident()
        return render_thread_id, llm_thread_id, event_loop_thread, frames

    render_tid, llm_tid, event_loop_tid, frames = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert "done" in event_names, f"no done frame: {frames}"
    # The render ran on a worker thread — NOT the event-loop thread.
    assert render_tid["id"] != event_loop_tid, (
        "sync render ran on the event loop (would stall other streams); "
        f"render thread {render_tid['id']} == event loop thread {event_loop_tid}"
    )
    # The LLM also ran on the worker thread (asyncio.run in the worker
    # thread installs a fresh loop there, so the LLM's ``await`` runs on
    # the same worker thread as the render). Both render and LLM share the
    # worker-thread id, and that id differs from the event-loop thread —
    # proving the ENTIRE design loop (blocking render + LLM) is off the
    # app's event loop.
    assert llm_tid["id"] == render_tid["id"], (
        f"LLM did not run on the same worker thread as the render: "
        f"llm={llm_tid['id']} render={render_tid['id']}"
    )
    assert llm_tid["id"] != event_loop_tid, (
        f"LLM ran on the event-loop thread: llm={llm_tid['id']} "
        f"event loop={event_loop_tid}"
    )
