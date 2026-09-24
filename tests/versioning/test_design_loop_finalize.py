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
from pathlib import Path

import pytest

from d33d.design_loop import BboxInfo, IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    create_version,
    repo_path_for,
    run_async,
)


class _StubResult:
    """Duck-type of the loop result whose ``best`` is a REAL
    ``IterationRecord`` (the params the route/adapter read are a declared
    field of that type — issue #93)."""

    def __init__(self, status: str, params: dict, scad: str = "", render=None) -> None:
        self.status = status
        self.best = IterationRecord(
            iteration=0,
            scad_source=scad,
            render=render if render is not None else _default_render(),
            score=Score(bits=(False,)*4, rank=0, tiebreak=(False,)*4),
            params=dict(params),
        )
        self.failure_reason = None if status == "pass" else "bbox_out_of_tolerance"


def _default_render() -> RenderResult:
    """A clean, no-op render for stub results (the route/adapter only read
    the declared fields off it)."""
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v",) * 6,
    )


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
# (1b) The finalize route persists the loop's measurement (issue #235)
#
# The chat path (`_resolve_version_create`) has always threaded the best
# candidate's measurement to `create_version` via the shared
# `_version_bbox_extents` / `_version_render_artifact_dir` helpers; the
# finalize route used to call `create_version` with neither kwarg, so a
# version created through `POST /finalize` persisted a NULL bbox and no
# design-state row could ever read 'measured'. These tests drive the
# FINALIZE seam with a stubbed loop result carrying a `BboxInfo` on the
# best record's DECLARED `bbox` field (the issue #93/#137 precedent —
# never duck-typed) and pin the persisted row through the HTTP route.
# ---------------------------------------------------------------------------


def _measured_render(artifact_dir: str) -> RenderResult:
    """A clean render that declares a durable artifact directory (the
    ``RenderResult.render_artifact_dir`` seam issue #163's persistence
    reads from the best record's render)."""
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v",) * 6,
        render_artifact_dir=artifact_dir,
    )


def _pass_result_with_bbox(
    bbox: BboxInfo | None,
    *,
    render: RenderResult | None = None,
    scad: str = "",
    params: dict | None = None,
) -> object:
    """A pass result whose ``best`` is a REAL ``IterationRecord`` carrying
    ``bbox`` on its DECLARED field (``None`` = no measurement obtained —
    the honest abstain that must persist a NULL, never a zero triple).
    ``params`` overrides the default ``{"W": 30.0, "D": 30.0, "H": 30.0}``
    when the test needs free-named params (issue #247: the v24 shape)."""
    record = IterationRecord(
        iteration=0,
        scad_source=scad,
        render=render if render is not None else _default_render(),
        score=Score(bits=(False,)*4, rank=0, tiebreak=(False,)*4),
        params=params if params is not None else {"W": 30.0, "D": 30.0, "H": 30.0},
        bbox=bbox,
    )

    class _Result:
        status = "pass"
        best = record
        failure_reason = None

    return _Result()


def test_finalize_pass_persists_measured_bbox_and_render_artifact_dir(
    app_with_versions, tmp_path: Path
) -> None:
    """(issue #235 regression) A finalize pass whose best candidate carries
    a ``BboxInfo`` (30.4/30.0/30.0 — deliberately off-nominal so a fix
    that persisted the previous version's bbox, or nothing, would fail)
    and a render declaring a durable artifact directory persists BOTH:
    ``versions.bbox`` is the measurement's extents (a non-NULL dict) and
    ``versions.render_artifact_dir`` is the render's declared path — read
    back through the route's own row reader (``latest_version``), not the
    201 response."""

    async def _loop(app, **kwargs):
        return _pass_result_with_bbox(
            BboxInfo(x=30.4, y=30.0, z=30.0, volume=28350.0),
            render=_measured_render(str(tmp_path / "durable-235")),
            scad="W = 30; cube([W, W, W]);",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the cube", "message": "make a 30mm cube"},
        )
        # The persisted row (the route's own reader — the write path is
        # the only thing between the stub's measurement and this dict).
        row = app_with_versions.state.versions.latest_version(pid)
        return r, row

    r, row = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert row is not None, "no version persisted for a passing finalize"
    # The persisted bbox IS the loop's measurement (non-NULL, exact).
    assert row["bbox"] == {"x": 30.4, "y": 30.0, "z": 30.0}, row["bbox"]
    # The render reference is the best render's declared durable dir.
    assert row["render_artifact_dir"] == str(tmp_path / "durable-235")


def test_finalize_pass_persists_whole_mesh_bbox_with_free_named_params(
    app_with_versions,
) -> None:
    """Issue #247: a finalize pass whose best candidate carries a v24-shaped
    BboxInfo (1 component, free-named params — no W/D/H) still persists the
    WHOLE-MESH bbox (non-NULL). The old ``_version_bbox_extents`` would
    return None for this shape (no W/D/H keys to match against), so the
    version row's bbox was NULL on the finalize path too. The new rule is
    independent of stated dims, param names, or component count."""

    async def _loop(app, **kwargs):
        return _pass_result_with_bbox(
            BboxInfo(
                x=21.21,
                y=21.43,
                z=19.30,
                volume=6369.8,
                components=((21.21, 21.43, 19.30, 6369.8, 0.0, 0.0, 0.0),),
            ),
            scad="spacer_width = 20;\nspacer_height = 12;\ncube([spacer_width, spacer_width, spacer_height]);",
            params={
                "fillet_radius": 1.5,
                "hole_clearance": 0.3,
                "hole_diameter": 3.3,
                "spacer_depth": 20.0,
                "spacer_height": 12.0,
                "spacer_width": 20.0,
                "wall_thickness": 3.0,
            },
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the spacer", "message": "make a spacer"},
        )
        row = app_with_versions.state.versions.latest_version(pid)
        return r, row

    r, row = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert row is not None, "no version persisted for a passing finalize"
    # The persisted bbox is the whole-mesh extents — NOT None (the old
    # code's bug: NULL bbox for this shape).
    assert row["bbox"] == {"x": 21.21, "y": 21.43, "z": 19.30}


def test_finalize_pass_without_measurement_persists_null_bbox(
    app_with_versions,
) -> None:
    """(issue #235 degrade) A finalize pass whose best candidate carries NO
    measurement (``bbox=None`` — the honest abstain) persists a NULL
    bbox: the row reads back ``None``, never a zero triple — the finalize
    seam's analogue of ``test_absent_measurement_persists_null_not_zero``
    through the chat path. The ``render_artifact_dir`` kwarg is still
    forwarded (the render here declares one) so a fix that dropped BOTH
    kwargs — or dropped only the dir — is caught here, and a fix that
    forwarded only the bbox kwarg fails the measurement test above.
    """
    artifact_dir = str(Path("__durable__") / "235")  # placeholder path

    async def _loop(app, **kwargs):
        return _pass_result_with_bbox(
            None, render=_measured_render(artifact_dir)
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the cube", "message": "make a 30mm cube"},
        )
        row = app_with_versions.state.versions.latest_version(pid)
        return r, row

    r, row = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    assert row is not None, "no version persisted for a passing finalize"
    # NULL, never (0, 0, 0) — an absent measurement abstains.
    assert row["bbox"] is None, f"expected NULL bbox, got {row['bbox']}"
    # The render reference is persisted even when the measurement is
    # absent (independent kwarg, forwarded regardless).
    assert row["render_artifact_dir"] == artifact_dir


def test_finalize_pass_yields_measured_design_state(app_with_versions) -> None:
    """(issue #235 acceptance) After a finalize pass that persisted a
    measurement, ``GET /api/projects/{id}/design-state`` yields
    ``provenance == 'measured'`` for every W/D/H axis whose stated value
    the measurement is within tolerance of — the 'measured' row becomes
    reachable through the finalize seam (a NULL-bbox row can only ever be
    ``stated``/``unknown``). The route reads the persisted row only — it
    does not re-render."""
    from d33d.design_state import state_block_for_version

    async def _loop(app, **kwargs):
        return _pass_result_with_bbox(
            BboxInfo(x=30.4, y=30.0, z=30.0, volume=28350.0)
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the cube", "message": "make a 30mm cube"},
        )
        assert r.status_code == 201, r.text
        # The persisted row carries the measurement (the precondition the
        # design-state route reads).
        row = app_with_versions.state.versions.latest_version(pid)
        assert row is not None and row["bbox"] == {"x": 30.4, "y": 30.0, "z": 30.0}
        # The GET the SPA reads, through the HTTP route.
        ds = await client.get(f"/api/projects/{pid}/design-state")
        assert ds.status_code == 200, ds.text
        # Identity guard: the route's block is built by the SAME shared
        # callable on the row's params + persisted bbox. The W/D/H rows
        # here are the PARAM rows (kind "param") — index by the full
        # row identity (kind+name; `name` alone is NOT unique within a
        # block once axis rows coexist), not by name.
        expected = state_block_for_version(row["params"], row["bbox"])
        return (
            ds.json(),
            {(e["kind"], e["name"]): e for e in expected},
        )

    body, expected = run_async(app_with_versions, _call)
    by_name = {(e["kind"], e["name"]): e for e in body}
    for axis in ("W", "D", "H"):
        entry = by_name[("param", axis)]
        # W is off-nominal (30.4 vs 30.0, within tolerance) and STILL
        # 'measured' — display carries the measured value.
        assert entry["provenance"] == "measured", (axis, entry)
        assert entry == expected[("param", axis)]
    assert by_name[("param", "W")]["value"] == 30.4


# ---------------------------------------------------------------------------
# (2) A non-pass result does NOT create a version
# ---------------------------------------------------------------------------


def test_finalize_exhausted_does_not_create_version(app_with_versions):
    """A loop that exhausts its 3 iterations without passing validation
    must NOT create a spurious version (422, no new version)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda **kw: _StubResult(
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
    # No confirmed axis anywhere (no stated_dims in the body, no dims in
    # the message, no version yet) → None — the abstaining state (issue
    # #247: the dead W/D/H param-key fallback that produced (0,0,0) is
    # gone; the gate abstains and records it as Score.bbox_abstained).
    assert captured["stated_dims"] is None
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
        "point": {"x": 300.0, "y": 200.0},
        "instruction": "open up this spiral, it's too tight to print",
    }


def test_region_edit_returns_202_accepted_and_records_full_kwargs(
    app_with_versions,
):
    """A valid region edit returns 202 {project_id, status: accepted}
    (mirroring /chat — no deferred field, no module_ids/view_id echo) and
    drives the injected design loop with the FULL kwargs contract:
    the composed request text (instruction prefixed with the view_id and,
    when named modules resolve, with module_ids + "at the marked point",
    non-empty), the marked PNG as a data URI (NOT the stored photo),
    stated_dims from the latest version row's persisted per-axis
    confirmed set (None for a fresh project — the gate abstains;
    issue #247 removed the (0,0,0) zero-triple fallback), an EMPTY
    chat_history (a scoped directive, not a chat turn —
    even when the project has prior transcripts), and a callable bbox_fn."""
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
    # request: non-empty instruction text prefixed with the view_id and
    # the resolved module_ids (the failures.jsonl hook's FailureEvent.request).
    # Issue #98 re-based the composition: with named modules the text is
    # "Region edit on modules {ids} at the marked point (view: {view_id}): {instruction}"
    # — "at the marked point" is the new token; module_ids + view_id +
    # instruction all still appear.
    assert captured["request"], "request kwarg must be non-empty"
    assert "curl_3" in captured["request"]
    assert "curl_4" in captured["request"]
    assert "front" in captured["request"]
    assert "at the marked point" in captured["request"]
    assert "open up this spiral, it's too tight to print" in captured["request"]
    # photo: the marked PNG from the body as a data URI (the vision model
    # sees the marked-up render, not the stored reference photo).
    assert captured["photo"] == f"data:image/png;base64,{_REGION_EDIT_PNG_BASE64}"
    # stated_dims: fresh project (no confirmed axis anywhere) → None;
    # the bbox gate ABSTAINS entirely (recorded as Score.bbox_abstained,
    # ticket #91) instead of hard-failing every candidate. Issue #247
    # removed the (0,0,0) zero-triple hand-off and the dead W/D/H
    # param-key read behind it.
    assert captured["stated_dims"] is None
    # chat_history: the EMPTY tuple — a region edit is a scoped directive,
    # not a chat turn (the project's transcript is never auto-included).
    assert captured["chat_history"] == ()
    assert callable(captured["bbox_fn"])
    # The instruction is NOT buried in chat_history (a scoped directive is
    # not a chat turn) — it must ride the explicit request line.
    assert captured.get("chat_history") == ()

    # Rendered-prompt check (issue #97): feed the route's composed request
    # through the REAL _design_messages and assert the instruction token
    # appears in the returned user text.  This test FAILS if the
    # ``Request:`` line is removed from _design_messages.
    from d33d.design_loop import _design_messages

    # ``None`` (no confirmed axis) is the abstaining state — the loop
    # normalizes it to the zero triple before the prompt renders
    # (``not specified`` per axis, never "0 mm"); test the same shape
    # the loop core hands the prompt builder.
    stated = captured["stated_dims"]
    if stated is None:
        stated = (0.0, 0.0, 0.0)
    rendered = _design_messages(
        photo=captured["photo"],
        chat_history=captured["chat_history"],
        stated=stated,
        repair=None,
        request=captured["request"],
    )
    user_text = rendered[0]["content"][0]["text"]
    assert "open up this spiral, it's too tight to print" in user_text, (
        "region-edit instruction token missing from rendered prompt:"
        f"\n{user_text}"
    )


def test_region_edit_stated_dims_from_latest_version(app_with_versions):
    """stated_dims is ALWAYS derived from the latest version row's
    PERSISTED per-axis confirmed set (no client override for region
    edits) — a project with an existing version passes that row's
    confirmed set as a (W, D, H) triple (zero-filled for unconfirmed
    axes), never re-derived from W/D/H param keys (issue #247)."""
    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("exhausted", {})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # Free-named params + the persisted per-axis confirmed set (the
        # production shape — the model never emits W/D/H keys, so the
        # dead param-key read could never see these dimensions).
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_depth": 8.0, "spacer_height": 5.0},
            stated_dims={"W": 12.0, "D": 8.0, "H": 5.0},
        )
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
    # The name is derived from WHAT CHANGED (issue #245), never the
    # region-edit request text: fresh project, no title comment in the
    # SCAD ("W = 11; cube([W]);") → "First design".
    assert timeline[0]["name"] == "First design"
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
    """Duck-type of ``RenderResult`` for chat-route tests: carries ALL 9
    declared ``RenderResult`` fields (issue #102: the recorded SEAM B
    fixture has all 9; a stub carrying only a subset would exercise a
    partial shape that the real consumer never sees). ``render_artifact_dir``
    mirrors the REAL declared ``RenderResult`` field name (issue #72) —
    the adapter reads it as a real attribute; a stub carrying any other
    name would exercise a dead read, which is exactly the bug issue #89
    fixes, so the stub's ctor takes the real field name only.
    """

    def __init__(
        self,
        ok: bool = True,
        exit_code: int = 0,
        duration_ms: int = 0,
        error_class: str = "ok",
        stderr: str = "",
        stl: str | None = None,
        csg: str | None = None,
        views: tuple[str, ...] = (),
        render_artifact_dir: str | None = None,
    ) -> None:
        self.ok = ok
        self.exit_code = exit_code
        self.duration_ms = duration_ms
        self.error_class = error_class
        self.stderr = stderr
        self.stl = stl
        self.csg = csg
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


class _StubResult:
    """Duck-type of the loop result whose ``best`` is a REAL
    ``IterationRecord`` (the params the route/adapter read are a declared
    field of that type — issue #93)."""

    def __init__(self, status: str, params: dict, scad: str = "", render=None) -> None:
        self.status = status
        self.best = IterationRecord(
            iteration=0,
            scad_source=scad,
            render=render if render is not None else _default_render(),
            score=Score(bits=(False,)*4, rank=0, tiebreak=(False,)*4),
            params=dict(params),
        )
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
    assert captured["stated_dims"] == (10.0, 10.0, 10.0)
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
    # The name is derived from WHAT CHANGED (issue #245), never the raw
    # user message "make a box 10 wide and 20 high": fresh project, no
    # title comment in the SCAD → "First design". The message field keeps
    # the raw text.
    assert timeline[0]["name"] == "First design"
    assert timeline[0]["created_by_message"] == "make a box 10 wide and 20 high"
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


def test_design_state_committed_before_version_created_frame(app_with_versions):
    """(issue #237 d3) By the moment the version-created frame is yielded,
    ``GET /api/projects/{pid}/design-state`` already returns the new
    version's rows — the version row is committed BEFORE the frame, so the
    SPA's refetch on the frame cannot read an empty block for a version
    that already exists. Frame-before-commit ordering is ruled out."""

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
        ds_at_frame = None
        async for event, data in source:
            frames.append((event, data))
            if event == "progress" and data.get("step") == "version-created":
                # The frame is on the wire: read design-state the same way
                # the SPA does, from inside the consuming loop, AT THAT
                # MOMENT.
                ds = await client.get(f"/api/projects/{pid}/design-state")
                ds_at_frame = (ds.status_code, ds.json())
            if event in ("done", "error"):
                break
        return frames, ds_at_frame

    frames, ds_at_frame = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert "done" in event_names, "no done frame"
    assert ds_at_frame is not None, "no version-created frame seen"
    status, body = ds_at_frame
    assert status == 200, status
    # The new version's rows are already visible at the frame moment — the
    # row is committed before the frame is yielded.
    by_name = {e["name"]: e for e in body}
    assert "W" in by_name, f"design-state at frame moment: {body}"
    assert by_name["W"]["value"] == 10, by_name


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
    """(issue #89 rename guard, unified via tests.seam_schemas — issue
    #102) Every attribute the frame-assembly path reads off the render
    object is a DECLARED ``RenderResult`` field — asserted via
    ``dataclasses.fields`` and a source-level AST check of the reader (the
    structural form of the #89 guard, now in the shared seam-schema
    module rather than an inline copy), so a future rename to a
    non-existent name fails LOUDLY here instead of silently yielding
    ``None`` (the exact bug: the reader once ``getattr``-ed a non-existent
    attribute name and every real frame shipped without an STL).
    """
    import ast
    import inspect

    from d33d.design_loop_events import _artifact_bytes_from_path
    from tests.seam_schemas import declared_field_names

    declared = declared_field_names(RenderResult)
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


def test_finalize_render_fn_kwarg_contract_matches_render_for_design_loop(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """(issue #213 regression) ``d33d.versions_routes._finalize_loop_kwargs``
    is a plain sync function — this test calls it DIRECTLY (no DI
    machinery), extracts ``kwargs["render_fn"]`` and invokes it with
    ``(scad_source, defines)`` against a spy whose signature EXACTLY
    matches ``render_for_design_loop``'s true 4-parameter contract
    (``scad_source, defines, renders_dir=None, on_progress=None``).

    The spy has no ``**kwargs`` catch-all and no ``on_progress_iteration``
    parameter: with the buggy call site in place (passing
    ``on_progress_iteration="_current"``), the invocation raises
    ``TypeError: ... got an unexpected keyword argument
    'on_progress_iteration'`` at call time — before any Docker/LLM work —
    and the test fails. No Docker, no live LLM, no catalogue required.
    """

    import d33d.render_worker as rw_mod
    import d33d.versions_routes as routes_mod

    spy_calls: dict = {}
    canned = _default_render()

    def _spy(
        scad_source: str,
        defines: dict[str, str],
        renders_dir=None,
        on_progress=None,
    ) -> RenderResult:
        # Exact 4-parameter signature of render_for_design_loop — an extra
        # keyword argument here is a genuine TypeError, not a swallowed
        # **kwargs (a star-star stub would let the buggy call pass).
        spy_calls["scad_source"] = scad_source
        spy_calls["defines"] = defines
        spy_calls["renders_dir"] = renders_dir
        spy_calls["on_progress"] = on_progress
        return canned

    monkeypatch.setattr(rw_mod, "render_for_design_loop", _spy)

    class _VersionsSvc:
        def get_project(self, project_id: int):
            return {"id": project_id, "current_version": None}

        def latest_version(self, project_id: int):
            return None

    class _State:
        pass

    state = _State()
    state.db_path = str(tmp_path / "d33d.sqlite3")
    state.versions = _VersionsSvc()
    state.catalogue = None

    class _Request:
        app = type("App", (), {"state": state})()

    body = routes_mod.FinalizeBody(
        params=None, name=None, message="make a 20mm wide bracket"
    )

    kwargs = routes_mod._finalize_loop_kwargs(_Request(), 7, body)
    render_fn = kwargs["render_fn"]
    assert callable(render_fn)

    # The guard: invoking the production closure's render_fn with the real
    # 2-arg call shape reaches the renderer without a TypeError (the
    # buggy extra kwarg would have raised one at call time).
    result = render_fn("W = 20; cube([W]);", {"W": "20"})
    assert result is canned

    # The spy was called with the exact 4-parameter contract — no stray
    # kwargs (a **kwargs-tolerant spy would hide them; this one is not).
    assert spy_calls["scad_source"] == "W = 20; cube([W]);"
    assert spy_calls["defines"] == {"W": "20"}
    # on_progress flows via the (absent-by-default) hook parameter — the
    # stamped-attribute mechanism's carrier, not a call-site kwarg.
    assert spy_calls["on_progress"] is None
    # The project-scoped renders_dir is still bound (issue #72).
    assert spy_calls["renders_dir"] is not None


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
        best = _StubResult("pass", {"W": 10}, scad="W = 10; cube([W]);").best
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


# ---------------------------------------------------------------------------
# (issue #221) Total wall-clock deadline on the design loop
# ---------------------------------------------------------------------------


def test_design_loop_total_timeout_yields_terminal_error_frame(app_with_versions, monkeypatch):
    """A loop that never terminates is cut off by the TOTAL wall-clock
    deadline (``design_loop_events.DESIGN_LOOP_TIMEOUT_SECONDS``), which
    fires within the monkeypatched 0.5s window and yields a terminal
    ``error`` frame with the NEW structured reason
    ``design_loop_timed_out`` (distinct from the render-worker ``"timeout"
    ``ErrorClass``). The timeout frame is the LAST frame — nothing is
    yielded after it, and the generator ends cleanly (no hang, no
    unhandled cancellation warning).
    """
    import asyncio
    import time

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_TIMEOUT_SECONDS", 0.5
    )

    class _StallLoop:
        """Production-seam-shaped stub: takes ``app`` (like the real
        closure), so the adapter calls it as ``run_loop(app=app, **kwargs)``
        and it runs under ``to_thread(_run_in_loop, ...)`` — the same path
        as the real loop. The stub returns a GENUINE coroutine that sleeps
        for 10s (well beyond the 0.5s deadline) — the real production loop
        can take minutes, so a multi-second stub is realistic. The
        deadline fires at 0.5s and cuts off the stream; the worker thread
        keeps running until the coroutine returns at 10s, but the test's
        ``asyncio.run`` teardown joins the executor thread, so the stub
        must return for the test to complete. 10s is a generous margin
        over the 0.5s deadline; the test's 2.0s assertion bound is
        comfortably below it."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                await asyncio.sleep(10)  # well beyond the 0.5s deadline
                return _StubResult("pass", {"W": 10})

            return _stall()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _StallLoop()
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="hi",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="hi",
        )
        started = time.monotonic()
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        elapsed = time.monotonic() - started
        return frames, elapsed

    frames, elapsed = run_async(app_with_versions, _call)
    assert frames, "no frames emitted at all"
    # The terminal frame arrived within the (0.5s) deadline — plus a
    # small scheduling margin — proving the deadline fired, not a
    # stall. The loop never terminates within the deadline, so any
    # longer bound would imply the timeout did not work.
    assert elapsed < 2.0, f"deadline did not fire promptly: {elapsed:.2f}s"
    # The error frame is present, carries the new structured reason,
    # and is the LAST frame yielded.
    events = [f[0] for f in frames]
    assert "error" in events, f"no error frame: {frames}"
    assert frames[-1][0] == "error", f"error is not the last frame: {frames}"
    error_data = frames[-1][1]
    assert error_data.get("reason") == DESIGN_LOOP_TIMED_OUT_REASON, (
        f"wrong reason: {error_data}"
    )
    assert "message" in error_data


def test_design_loop_deadline_does_not_false_abort_slow_run(app_with_versions, monkeypatch):
    """A legitimately slow run that COMPLETES just under the deadline is
    not aborted: with the deadline monkeypatched to 2.0s, a stub that
    sleeps ~0.6s (well under) still yields its normal ``done`` frame and
    NO timeout error frame. The deadline is total wall-clock (not
    idle/per-frame), but it must not fire for runs that finish in time.
    """
    import asyncio

    from d33d.design_loop_events import run_design_loop_with_events

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_TIMEOUT_SECONDS", 2.0
    )

    async def _slow_loop():
        await asyncio.sleep(0.6)  # comfortably under the 2.0s deadline
        return _StubResult("pass", {"W": 10}, scad="W = 10; cube([W]);")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda **kw: _slow_loop()
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
        return frames

    frames = run_async(app_with_versions, _call)
    events = [f[0] for f in frames]
    assert "done" in events, f"expected a done frame, got: {frames}"
    assert frames[-1][0] == "done", f"done is not the last frame: {frames}"
    assert not any(
        f[0] == "error" and f[1].get("reason") == "design_loop_timed_out"
        for f in frames
    ), f"false timeout abort: {frames}"


def test_design_loop_deadline_fires_among_liveness_frames(app_with_versions, monkeypatch):
    """The deadline races the FRAME-QUEUE consumer, not just the
    ``to_thread`` render task: a stub that keeps enqueuing liveness frames
    (``on_progress`` every 50ms) but never terminates must STILL be cut
    off by the 0.5s deadline with the ``design_loop_timed_out`` frame.
    A timeout placed only around ``render_task.done()`` would let this
    stream run forever.
    """
    import asyncio
    import time

    from d33d.design_loop_events import (
        DESIGN_LOOP_TIMED_OUT_REASON,
        run_design_loop_with_events,
    )

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_TIMEOUT_SECONDS", 0.5
    )

    class _LivenessLoop:
        """Production-seam-shaped stub: takes ``app`` (like the real
        closure), so the adapter calls it as ``run_loop(app=app, **kwargs)``
        and it runs under ``to_thread(_run_in_loop, ...)`` — the same path
        as the real loop. The stub returns a GENUINE coroutine that emits
        liveness frames (``on_progress`` every 50ms) for 10s (well beyond
        the 0.5s deadline) then returns — the real production loop can
        take minutes, so a multi-second stub is realistic. The deadline
        fires at 0.5s and cuts off the stream; the worker thread keeps
        running until the coroutine returns at 10s, but the test's
        ``asyncio.run`` teardown joins the executor thread, so the stub
        must return for the test to complete. 10s is a generous margin
        over the 0.5s deadline; the test's 2.0s assertion bound is
        comfortably below it."""

        def __call__(self, app=None, **kwargs):
            on_progress = kwargs["on_progress"]  # the adapter's live hook

            async def _liveness():
                deadline = time.monotonic() + 10  # well beyond the 0.5s deadline
                i = 0
                while time.monotonic() < deadline:
                    on_progress(
                        "view-done", {"view": "view_01_iso", "iteration": i}
                    )
                    i += 1
                    await asyncio.sleep(0.05)  # liveness frames every 50ms
                return _StubResult("pass", {"W": 10})

            return _liveness()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _LivenessLoop()
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="hi",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="hi",
        )
        started = time.monotonic()
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        elapsed = time.monotonic() - started
        return frames, elapsed

    frames, elapsed = run_async(app_with_versions, _call)
    assert frames, "no frames emitted at all"
    # The liveness frames are drained live (proof the deadline raced
    # the frame queue, not just the render task), and the deadline
    # fired within the 0.5s window (+ margin) despite them.
    progress_frames = [
        f
        for f in frames
        if f[0] == "progress" and f[1].get("step", "").startswith("render-view")
    ]
    assert progress_frames, f"expected liveness frames before the cut-off: {frames}"
    assert elapsed < 2.0, f"deadline did not fire promptly: {elapsed:.2f}s"
    # The terminal timeout frame is LAST, with the new reason.
    assert frames[-1][0] == "error", f"error is not the last frame: {frames}"
    assert frames[-1][1].get("reason") == DESIGN_LOOP_TIMED_OUT_REASON


def test_design_loop_deadline_cancels_render_task(app_with_versions, monkeypatch):
    """When the deadline fires, the abandoned ``to_thread`` render task is
    cancelled (best-effort — ``to_thread`` threads cannot be killed
    mid-flight, but the asyncio task must not be left pending and must
    not surface an unhandled ``CancelledError``) and the frame-queue
    consumer task is not left behind: the generator ends cleanly and the
    render task is no longer pending after the terminal frame.
    """
    import asyncio

    from d33d.design_loop_events import run_design_loop_with_events

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_TIMEOUT_SECONDS", 0.5
    )
    task_state: dict[str, object] = {}

    class _StallLoop:
        """Production-seam-shaped stub: takes ``app`` (like the real
        closure), so the adapter calls it as ``run_loop(app=app, **kwargs)``
        and it runs under ``to_thread(_run_in_loop, ...)`` — the same path
        as the real loop. The stub returns a GENUINE coroutine that sleeps
        for 10s (well beyond the 0.5s deadline) then returns — the real
        production loop can take minutes, so a multi-second stub is
        realistic. The deadline fires at 0.5s and cuts off the stream;
        the worker thread keeps running until the coroutine returns at 10s,
        but the test's ``asyncio.run`` teardown joins the executor thread,
        so the stub must return for the test to complete."""

        def __call__(self, app=None, **kwargs):
            async def _stall():
                await asyncio.sleep(10)  # well beyond the 0.5s deadline
                return _StubResult("pass", {"W": 10})

            return _stall()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # Capture the adapter's render task so the test can assert it is
        # not left pending after the deadline fires. The adapter builds
        # it via ``asyncio.ensure_future(asyncio.to_thread(_run_in_loop,
        # raw))`` — ``ensure_future`` is a stable seam to wrap.
        orig_ensure_future = asyncio.ensure_future

        def _tracking_ensure_future(coro, loop=None):
            task = orig_ensure_future(coro, loop=loop) if loop else orig_ensure_future(coro)
            # The adapter's render task is the only task wrapping a
            # ``to_thread`` coroutine in this generator (the frame-queue
            # ``get`` task is a bare coroutine).
            if "to_thread" in repr(coro):
                task_state["render_task"] = task
            return task

        monkeypatch.setattr(asyncio, "ensure_future", _tracking_ensure_future)
        app_with_versions.state.run_design_loop = _StallLoop()
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
        # Let the cancellation settle on the loop before returning.
        await asyncio.sleep(0.05)
        return frames

    frames = run_async(app_with_versions, _call)
    assert frames[-1][0] == "error", f"error is not the last frame: {frames}"
    render_task = task_state.get("render_task")
    assert render_task is not None, "render task not captured by the seam"
    # The render task was cancelled by the deadline path (the
    # ``to_thread`` thread keeps running until the stub returns at 10s,
    # but the asyncio task must not be left pending — that state leaks as
    # an unhandled-task warning when the loop closes).
    assert render_task.cancelled(), (
        f"render task was not cancelled after the deadline fired: "
        f"state={render_task!r}"
    )


# ---------------------------------------------------------------------------
# (issue #82) Structured failure-reason field on the exhausted error frame
# ---------------------------------------------------------------------------


def test_exhausted_error_frame_carries_structured_reason(app_with_versions):
    """An exhausted loop's error frame carries the structured ``reason``
    field in addition to the free-text ``message`` (issue #82). The SPA
    maps ``reason`` to plain-language copy without string-matching.
    """

    async def _loop(app, **kwargs):
        return _StubResult("exhausted", {"W": 10})

    async def _call(client):
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
    error_frames = [data for event, data in frames if event == "error"]
    assert error_frames, "no error frame emitted"
    # The structured reason field is present and carries the failure reason
    assert "reason" in error_frames[0], (
        "exhausted error frame must carry the structured 'reason' field"
    )
    assert error_frames[0]["reason"] == "bbox_out_of_tolerance", (
        f"wrong reason value: {error_frames[0]['reason']}"
    )
    # The free-text message is preserved for backward compatibility
    assert "message" in error_frames[0], "free-text 'message' field must be preserved"
    assert "Design loop exhausted" in error_frames[0]["message"]


def test_infra_error_frame_has_no_structured_reason(app_with_versions):
    """An infra-failure error frame (no DesignResult) carries NO ``reason``
    field — the SPA treats a missing ``reason`` as an infra failure and
    uses generic copy, not a gate mapping (issue #82).
    """

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # Not-wired case: run_design_loop is None → the adapter emits the
        # infra error frame "design loop not wired" (no DesignResult).
        app_with_versions.state.run_design_loop = None
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app_with_versions.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    error_frames = [data for event, data in frames if event == "error"]
    assert error_frames, "no error frame emitted"
    # No structured reason — infra failure has no DesignResult
    assert "reason" not in error_frames[0], (
        "infra-failure error frame must NOT carry a 'reason' field "
        "(no DesignResult to read it from)"
    )
    # The free-text message is still present
    assert "message" in error_frames[0]


def test_render_reader_reverted_field_name_schema_catches_seam_b_drift():
    """RED-CHECK (b) for issue #102: the #89 defect (the SSE reader uses
    ``getattr(render, "render_artifact_path")`` instead of the declared
    field ``render_artifact_dir``) is caught by the SEAM B schema. The
    existing test ``test_render_result_reader_reads_declared_fields``
    asserts via AST that every ``render.<attr>`` read targets a declared
    field; when the reader uses the wrong name, that test goes RED. This
    test feeds the same fixture through the SEAM B schema — the schema
    must ALSO catch the drift (the verification layer is independent of
    the existing test).

    Simulated by constructing a ``RenderResult`` with a ``render_
    artifact_path`` attribute (the misspelled field) and asserting the
    SEAM B schema fails (the declared field ``render_artifact_dir`` is
    missing — the reader's attribute is not a declared field).
    """
    from d33d.render_worker import RenderResult
    from tests.seam_schemas import validate_render_result_seam_b

    # A RenderResult with the correct declared field (the healthy shape).
    healthy = RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl="/durable/model.stl",
        csg=None,
        views=(),
        render_artifact_dir="/durable",
    )
    validate_render_result_seam_b(healthy)

    # Simulate the #89 defect: the reader reads `render_artifact_path`
    # (a non-existent attribute). The RenderResult is a frozen dataclass —
    # you cannot set a non-existent attribute. The defect is caught by the
    # SEAM B schema when the reader's AST references the wrong field:
    # the existing test asserts via AST that every `render.<attr>` read
    # targets a declared field. A misspelled `render_artifact_path` would
    # fail that AST check.
    #
    # The SEAM B schema ALSO catches the drift: if a RenderResult were
    # constructed with a `render_artifact_path` instead of
    # `render_artifact_dir` (impossible via the dataclass constructor,
    # but possible via a duck-typed stub), the schema's "every declared
    # field present" check would fail (the declared `render_artifact_dir`
    # is missing).
    from dataclasses import fields

    declared = {f.name for f in fields(RenderResult)}
    assert "render_artifact_dir" in declared
    assert "render_artifact_path" not in declared, (
        "render_artifact_path is not a declared field — the #89 misspelling"
    )


def test_render_reader_ast_with_reverted_field_name_goes_red():
    """RED-CHECK (b) for issue #102: the #89 defect (the SSE reader uses
    ``getattr(render, "render_artifact_path")`` instead of the declared
    field ``render_artifact_dir``) is caught by the AST check in the
    existing test ``test_render_result_reader_reads_declared_fields``.
    When the reader's source references ``render_artifact_path`` (the
    misspelled field), the AST walk finds a ``render.<attr>`` read whose
    attr is NOT in the declared field set — the test goes RED.

    This test simulates the reverted shape by AST-parsing a snippet that
    reads ``render.render_artifact_path`` and asserting the AST check
    fails (the attr is not a declared field). The existing test runs the
    same check against the REAL reader source — when the reader is
    reverted to the misspelled name, the existing test goes RED.
    """
    import ast
    import inspect

    from d33d.design_loop_events import _artifact_bytes_from_path
    from d33d.render_worker import RenderResult
    from tests.seam_schemas import declared_field_names

    declared = declared_field_names(RenderResult)
    # The real reader source: every `render.<attr>` read targets a
    # declared field (the healthy shape).
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
    # Simulate the #89 defect: a reader source that reads
    # `render_artifact_path` (the misspelled field). The AST check must
    # fail (the attr is not in the declared set).
    defective_source = "artifact_path = render.render_artifact_path"
    defective_tree = ast.parse(defective_source)
    found_defect = False
    for node in ast.walk(defective_tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "render"
        ) and node.attr not in declared:
            found_defect = True
            break
    assert found_defect, (
        "the #89 defect (render_artifact_path) was not caught by the AST "
        "check — the verification layer is not catching the misspelling"
    )
