"""Issue #93: no version is ever created and no version-created SSE frame
is ever emitted — ``_resolve_version_create`` read ``getattr(best,
"params", None)`` but ``IterationRecord`` had NO ``params`` field, so the
read was ``None`` for every real candidate on every turn; on a fresh
project the latest-version fallback was also empty, the function returned
``None`` and the browser never received the generated model.

The fix (per the PM's settled decisions):

* ``IterationRecord`` gains a REAL declared ``params`` field, populated at
  BOTH construction sites from the same defines map the render used
  (``_dim_params(stated_dims, defines)``);
* values that parse as numbers are stored as ``float`` (W/D/H must be
  numeric — ``latest_version_stated_dims`` and the region-edit route
  ``float()`` them on read);
* an axis the caller never stated is OMITTED from params entirely (never a
  stored ``0`` — a stored zero is a false fact);
* a passing loop ALWAYS materialises a version, with whatever params are
  known (possibly none) — ``create_version`` accepts an empty params dict;
* the finalize route's identical dead ``getattr`` is fixed in the same
  change and its semantic flip (a fresh-project pass that used to 502 may
  now return 201) is pinned below;
* the rename guard (dataclasses.fields + AST, mirroring the #89 guard)
  keeps this defect class from silently returning.

Fast layer: SPA-shaped bodies through the REAL adapter, real
``IterationRecord`` / ``RenderResult`` objects (never stubs with a
hand-set ``params`` — that is exactly why the bug survived), no Docker.
"""

from __future__ import annotations

import asyncio
import base64
import dataclasses
import uuid

from d33d.design_loop import (
    IterationRecord,
    _dim_params,
    _params_for_record,
)
from d33d.design_loop_events import (
    _resolve_version_create,
    latest_version_stated_dims,
)
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    run_async,
)

# ---------------------------------------------------------------------------
# Loop-level: the real IterationRecord carries the render's defines
# ---------------------------------------------------------------------------


def _ok_render(scad_text: str) -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v1", "v2", "v3", "v4", "v5", "v6"),
    )


def _stub_llm(scad: str):
    class _LLMResult:
        def __init__(self):
            self.tool_calls = []
            self.content = f"```scad\n{scad}\n```"
            self.prompt_hash = "h"
            self.status = "ok"

    async def _llm(role, messages, system):
        return _LLMResult()

    return _llm


def test_loop_pass_record_carries_numeric_stated_dims() -> None:
    """The REAL ``IterationRecord`` from a passing loop carries the
    defines map the render was made with, with numeric W/D/H (the stored
    JSON shape is asserted exactly — a future coercion change fails
    loudly)."""
    import asyncio

    from d33d.design_loop import run_design_loop_async

    scad = "W = 20; D = 15; H = 10; cube([W, D, H]);"

    from d33d.design_loop import BboxInfo

    # The bbox must match the stated triple (or the gate fails and the
    # loop exhausts) — a real render's extents equal the stated dims.
    bbox = BboxInfo(x=20.0, y=15.0, z=10.0, volume=3000.0)

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    coro = run_design_loop_async(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=(20.0, 15.0, 10.0),
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
        defines={},
    )
    result = asyncio.run(coro)
    assert result.status == "pass"
    # The EXACT stored JSON shape: numeric W/D/H, in the declared field.
    assert result.best.params == {"W": 20.0, "D": 15.0, "H": 10.0}
    assert isinstance(result.best.params["W"], float)
    assert isinstance(result.best.params["D"], float)
    assert isinstance(result.best.params["H"], float)
    # The loop itself is unchanged: the score bits and abstention are the
    # same as before the fix (the record just carries more data now).
    assert result.best.score.bits == (True, True, True, True)


def test_loop_pass_record_carries_caller_defines_as_numeric() -> None:
    """Caller-supplied non-dimension defines (e.g. FDM clearances) are
    carried through as-is — numeric ones as floats, stringly ones as
    strings — they are real parameters of the render."""
    scad = "W = 20; D = 20; H = 20; cube([W, D, H]);"
    import asyncio

    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    coro = run_design_loop_async(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
        defines={"clearance": "0.4", "layer": "PLA"},
    )
    result = asyncio.run(coro)
    assert result.status == "pass"
    assert result.best.params == {
        "W": 20.0,
        "D": 20.0,
        "H": 20.0,
        "clearance": 0.4,
        "layer": "PLA",
    }


def test_loop_abstained_pass_record_omits_zero_axes() -> None:
    """A dimensionless (abstained) pass: the record's params OMIT W/D/H
    entirely — never a stored ``0`` (a stored zero is a false fact;
    absent correctly means "unknown")."""
    scad = "x = 20; cube([x, x, x]);"
    import asyncio

    from d33d.design_loop import BboxInfo, run_design_loop_async

    # The bbox gate abstains on the (0,0,0) triple, so any extent passes.
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    coro = run_design_loop_async(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=None,  # "no dimensions known"
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
        defines={},
    )
    result = asyncio.run(coro)
    assert result.status == "pass"
    assert result.best.score.bbox_abstained is True
    # No W/D/H at all — and no zero values anywhere.
    assert result.best.params == {}
    for value in result.best.params.values():
        assert value != 0


def _resolve_version_create_test(
    app,
    project_id: int,
    result,
    user_message: str = "hi",
):
    """Drive ``_resolve_version_create`` and collect the outcome.

    Returns a dict with ``version_id`` (None when no version was created)
    and ``call_count`` / ``calls`` (the create_version invocations)."""
    calls = []

    class _Svc:
        def __init__(self):
            self.latest_version = lambda pid: None

        async def create_version(self, pid, params, *, name=None, message=""):
            calls.append({"params": params, "name": name, "message": message})
            return {"id": 7}

    app.state.versions = _Svc()

    import asyncio

    vid = asyncio.run(_resolve_version_create(app, project_id, result, user_message))
    return {
        "version_id": vid,
        "call_count": len(calls),
        "calls": calls,
    }


def _make_app_stub():
    class _State:
        pass

    class _App:
        def __init__(self):
            self.state = _State()

    return _App()


def test_resolve_version_create_passing_always_creates_version(app_with_versions):
    """PM decision (settled): a passing result ALWAYS materialises a
    version, with whatever params are known — possibly NONE. An empty
    params dict is used as-is (create_version accepts it), never
    suppressed."""
    app = _make_app_stub()
    result = _make_result(best_params={})
    outcome = _resolve_version_create_test(app, 1, result)
    assert outcome["version_id"] == 7
    assert outcome["call_count"] == 1
    assert outcome["calls"][0]["params"] == {}


def test_resolve_version_create_numeric_params(app_with_versions):
    """Numeric params (the stored JSON shape) are used as-is."""
    app = _make_app_stub()
    result = _make_result(best_params={"W": 20.0, "D": 15.0, "H": 10.0})
    outcome = _resolve_version_create_test(app, 1, result)
    assert outcome["version_id"] == 7
    assert outcome["calls"][0]["params"] == {"W": 20.0, "D": 15.0, "H": 10.0}


def test_resolve_version_create_no_best_returns_none(app_with_versions):
    """A loop result that carries no candidate at all (``best is None`` —
    a contract violation) returns None (no version) — the guard shape is
    preserved, only the id must be resolvable."""
    app = _make_app_stub()

    class _Svc:
        def __init__(self):
            self.latest_version = lambda pid: {"id": 3, "params": {"W": 1.0}}

        async def create_version(self, pid, params, *, name=None, message=""):
            raise AssertionError("create_version must not be called")

    app.state.versions = _Svc()

    class _Result:
        status = "pass"
        best = None

    outcome = _resolve_version_create_test(app, 1, _Result())
    assert outcome["version_id"] is None
    assert outcome["call_count"] == 0


def test_resolve_version_create_empty_params_creates_version(app_with_versions):
    """A candidate whose ``params`` is an empty dict (the abstained case —
    a dimensionless pass with no known parameters) still creates a version
    (a pass always materialises a version). The empty params are used
    as-is (create_version accepts an empty dict), never suppressed."""
    app = _make_app_stub()

    class _Svc:
        def __init__(self):
            self.latest_version = lambda pid: {
                "id": 3,
                "params": {"W": 99.0, "D": 88.0, "H": 77.0},
            }

        async def create_version(self, pid, params, *, name=None, message=""):
            return {"id": 7}

    app.state.versions = _Svc()
    # A result whose best has an empty params dict (the abstained case).
    class _EmptyBest:
        def __init__(self):
            self.params = {}  # empty dict — the abstained case
            self.scad_source = "x = 20; cube([x]);"

    class _Result:
        status = "pass"
        best = _EmptyBest()

    outcome = _resolve_version_create_test(app, 1, _Result())
    assert outcome["version_id"] == 7
    # The empty params are used as-is (the empty dict is the loop's
    # authoritative answer, not a signal to fall back).
    assert outcome["calls"][0]["params"] == {}


def _make_result(best_params):
    """A minimal loop-result duck carrying an IterationRecord-shaped best
    with the declared params field (NOT a stub with a hand-set attribute
    outside the dataclass — the record is built via the real type below
    where the full shape is needed; here a minimal dict-carrying object
    exercises the resolver's precedence logic in isolation)."""

    class _Best:
        def __init__(self, params):
            self.params = params
            self.scad_source = "x = 20; cube([x]);"
            self.render = None

    class _Result:
        def __init__(self):
            self.status = "pass"
            self.best = _Best(best_params)
            self.failure_reason = None

    return _Result()


# ---------------------------------------------------------------------------
# The DECISIVE test: a real passing loop through the real adapter
# ---------------------------------------------------------------------------


def _write_durable_artifacts(root, *, partial_views: bool = False):
    """A live durable artifact directory (the issue #72 seam): a real
    on-disk ``model.stl`` + the 6 fixed VIEWS view PNGs under ``root``."""

    from d33d.render_worker import VIEWS

    d = root / f"artifacts-{uuid.uuid4().hex[:8]}"
    d.mkdir(parents=True)
    (d / "model.stl").write_bytes(b"\x84\xab\x50\x53fake-stl-bytes")
    for name, _cam in VIEWS:
        if partial_views and name == "view_05_iso.png":
            continue
        (d / name).write_bytes(b"\x89PNG-fake-view-bytes")
    return str(d)


def test_chat_pass_real_iteration_record_creates_version_and_frame(
    app_with_versions, tmp_path
):
    """DECISIVE (issue #93): drive a passing chat turn through the REAL
    adapter with an SPA-shaped body on a FRESH project. The injected loop
    seam is the REAL ``run_design_loop_async`` core (so the result is a
    REAL ``IterationRecord``, not a stub with a hand-set ``params``
    attribute — that is exactly why the bug survived: every existing stub
    carried ``.params``, so no test ever proved the production shape).

    Asserts:
    - EXACTLY ONE versions row (not >= 1);
    - its params equal the expected converted map (numeric W/D/H, the
      exact stored JSON shape);
    - a version-created frame was emitted carrying version_id,
      stl_data_uri and views.
    """
    from pathlib import Path

    from d33d.design_loop import BboxInfo, run_design_loop_async

    scad = "W = 20; D = 20; H = 20; cube([W, D, H]);"
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    def _render_with_artifacts(artifact_dir):
        """A render that carries the DURABLE artifact directory as a real
        ``RenderResult.render_artifact_dir`` attribute — the declared field
        the adapter reads for ``stl_data_uri`` + ``views`` (a render without
        it omits both, the issue #89 dead-getattr lesson)."""

        async def _render(scad_source, defines):
            return RenderResult(
                ok=True,
                exit_code=0,
                duration_ms=1,
                error_class="ok",
                stderr="",
                stl=str(Path(artifact_dir) / "model.stl"),
                csg=None,
                views=("v1", "v2", "v3", "v4", "v5", "v6"),
                render_artifact_dir=artifact_dir,
            )

        return _render

    def _loop_impl(scad_text, bbox_, render_fn):
        async def _loop(app, **kwargs):
            return await run_design_loop_async(
                photo=kwargs["photo"],
                chat_history=kwargs["chat_history"],
                stated_dims=kwargs["stated_dims"],
                render_fn=render_fn,
                llm_fn=_stub_llm(scad_text),
                bbox_fn=lambda r: bbox_,
            )

        return _loop

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        artifact_dir = _write_durable_artifacts(tmp_path)
        fixture = Path("tests/fixtures/stl/box_20mm.stl")
        (Path(artifact_dir) / "model.stl").write_bytes(fixture.read_bytes())

        # The injected seam: a REAL design-loop async core (the same shape
        # the production closure drives), with a stub LLM (no live model)
        # and a render that carries the durable artifact dir as a real
        # RenderResult field. The RESULT is a real IterationRecord built
        # by the real core.
        app_with_versions.state.run_design_loop = _loop_impl(
            scad, bbox, _render_with_artifacts(artifact_dir)
        )

        # The SPA-shaped body: ONLY message + chat_history.
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "Create a 20mm cube", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        assert source is not None, "event source not registered before 202"
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return r, frames, timeline, fixture

    r, frames, timeline, fixture = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text

    # EXACTLY ONE versions row.
    assert len(timeline) == 1, (
        f"expected exactly ONE version, got {len(timeline)}: {timeline}"
    )
    assert timeline[0]["name"] == "design"
    # The params equal the expected converted map — numeric W/D/H, the
    # exact stored JSON shape (a future coercion change fails loudly).
    assert timeline[0]["params"] == {"W": 20.0, "D": 20.0, "H": 20.0}
    assert isinstance(timeline[0]["params"]["W"], float)
    assert isinstance(timeline[0]["params"]["D"], float)
    assert isinstance(timeline[0]["params"]["H"], float)

    # A version-created frame WAS emitted, carrying version_id,
    # stl_data_uri and views.
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    assert vc[0]["version_id"] == timeline[0]["id"]
    stl_uri = vc[0].get("stl_data_uri")
    assert stl_uri is not None, "stl_data_uri missing on version-created frame"
    payload = base64.b64decode(stl_uri.split(";base64,", 1)[1])
    assert payload == fixture.read_bytes(), "STL bytes mismatch vs on-disk fixture"
    view_map = vc[0].get("views")
    from d33d.render_worker import VIEWS

    assert set(view_map.keys()) == {name for name, _cam in VIEWS}

    # The terminal frame is a done (not an error).
    assert frames[-1][0] == "done"


# ---------------------------------------------------------------------------
# Abstained pass: version created, no W/D/H, next turn abstains
# ---------------------------------------------------------------------------


def test_chat_abstained_pass_creates_version_without_dims_and_next_turn_abstains(
    app_with_versions,
):
    """ABSTAINED PASS (issue #93, PM decision): a passing turn with
    unknown dimensions (no W/D/H stated anywhere) STILL creates a version
    (a pass with unknown dimensions is a legitimate state — the user must
    still get their model), its params contain NO W/D/H (not a stored
    zero), and the NEXT turn's ``latest_version_stated_dims`` returns
    None (not a zero triple) — asserted end to end.
    """
    scad = "x = 20; cube([x, x, x]);"
    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    def _loop_impl(scad_text, bbox_):
        async def _render(scad_source, defines):
            return _ok_render(scad_source)

        async def _loop(app, **kwargs):
            return await run_design_loop_async(
                photo=kwargs["photo"],
                chat_history=kwargs["chat_history"],
                stated_dims=kwargs["stated_dims"],
                render_fn=_render,
                llm_fn=_stub_llm(scad_text),
                bbox_fn=lambda r: bbox_,
            )

        return _loop

    captured_turn2: dict = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # Turn 1: a dimensionless prompt — the loop abstains and PASSES.
        app_with_versions.state.run_design_loop = _loop_impl(scad, bbox)
        r1 = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make something rounded", "chat_history": []},
        )
        source1 = app_with_versions.state.event_sources.get(pid)
        assert source1 is not None
        frames1 = []
        async for event, data in source1:
            frames1.append((event, data))
            if event in ("done", "error"):
                break
        timeline1 = (await client.get(f"/api/projects/{pid}/versions")).json()

        # Turn 2: the loop seam now just captures the stated_dims it
        # receives (the real core is no longer needed — we are asserting
        # the route's dimension resolution, not the loop's scoring).
        def _capture_loop(app, **kwargs):
            captured_turn2.update(kwargs)

            class _R:
                status = "pass"
                failure_reason = None
                best = None

            return _R()

        app_with_versions.state.run_design_loop = _capture_loop
        # Release the in-flight flag (the SSE endpoint's finally would do
        # this in production, but we drove the generator directly — the
        # flag is not cleared when the consumer stops early).
        app_with_versions.state.design_loop_inflight.discard(pid)
        r2 = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make it a bit rounder", "chat_history": []},
        )
        source2 = app_with_versions.state.event_sources.get(pid)
        assert source2 is not None
        async for event, data in source2:
            if event in ("done", "error"):
                break
        return r1, r2, frames1, timeline1

    r1, r2, frames1, timeline1 = run_async(app_with_versions, _call)
    assert r1.status_code == 202, r1.text
    # A version WAS created despite the abstained (dimensionless) pass.
    assert len(timeline1) == 1, (
        f"an abstained pass must still create a version, got {len(timeline1)}"
    )
    # Its params contain NO W/D/H — and no zero values anywhere.
    v0 = timeline1[0]
    for axis in ("W", "D", "H"):
        assert axis not in v0["params"], (
            f"abstained axis {axis} must be OMITTED, not stored as "
            f"{v0['params'].get(axis)!r}"
        )
    for value in v0["params"].values():
        assert value != 0, "a stored zero is a false fact"
    # The version-created frame was emitted.
    vc = [
        d for e, d in frames1 if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame on an abstained pass"

    # Next turn: the latest-version fallback yields None (abstain), not a
    # zero triple — asserted end to end through the real route.
    assert r2.status_code == 202, r2.text
    assert captured_turn2["stated_dims"] is None, (
        f"next turn's stated_dims must be None (abstain), got "
        f"{captured_turn2['stated_dims']}"
    )


def test_latest_version_stated_dims_omitted_axes_yields_none() -> None:
    """The helper directly: a version whose params OMIT W/D/H (the
    abstained-pass case) yields None — never a zero triple."""

    class _Svc:
        def latest_version(self, project_id):
            return {"params": {}}

    assert latest_version_stated_dims(_Svc(), 1) is None


# ---------------------------------------------------------------------------
# Follow-up turn: the latest-version fallback actually supplies the dims
# ---------------------------------------------------------------------------


def test_chat_follow_up_latest_version_fallback_supplies_dims(
    app_with_versions,
):
    """FOLLOW-UP TURN (issue #93 cascade): a project that NOW HAS a
    version (created by a passing first turn) — the latest-version
    fallback actually supplies the dims (currently a no-op because no
    version ever exists). The loop seam captures the stated_dims it
    receives."""
    scad = "W = 20; D = 20; H = 20; cube([W, D, H]);"
    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    def _loop_impl(scad_text, bbox_):
        async def _render(scad_source, defines):
            return _ok_render(scad_source)

        async def _loop(app, **kwargs):
            return await run_design_loop_async(
                photo=kwargs["photo"],
                chat_history=kwargs["chat_history"],
                stated_dims=kwargs["stated_dims"],
                render_fn=_render,
                llm_fn=_stub_llm(scad_text),
                bbox_fn=lambda r: bbox_,
            )

        return _loop

    captured_turn2: dict = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # Turn 1: a passing turn with KNOWN dimensions creates a version.
        app_with_versions.state.run_design_loop = _loop_impl(scad, bbox)
        r1 = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "Create a 20mm cube", "chat_history": []},
        )
        source1 = app_with_versions.state.event_sources.get(pid)
        assert source1 is not None
        async for event, data in source1:
            if event in ("done", "error"):
                break
        timeline1 = (await client.get(f"/api/projects/{pid}/versions")).json()

        # Turn 2: a follow-up message with NO stated dimensions — the
        # latest version's W/D/H must supply the triple (the fallback
        # that was a no-op before the fix because no version ever existed).
        def _capture_loop(app, **kwargs):
            captured_turn2.update(kwargs)

            class _R:
                status = "pass"
                failure_reason = None
                best = None

            return _R()

        app_with_versions.state.run_design_loop = _capture_loop
        # Release the in-flight flag (the SSE endpoint's finally would do
        # this in production, but we drove the generator directly — the
        # flag is not cleared when the consumer stops early).
        app_with_versions.state.design_loop_inflight.discard(pid)
        r2 = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make it a bit rounder", "chat_history": []},
        )
        source2 = app_with_versions.state.event_sources.get(pid)
        assert source2 is not None
        async for event, data in source2:
            if event in ("done", "error"):
                break
        return r1, r2, timeline1

    r1, r2, timeline1 = run_async(app_with_versions, _call)
    assert r1.status_code == 202, r1.text
    assert len(timeline1) == 1, "turn 1 must have created a version"
    # The fallback supplies the dims — not None, not (0,0,0).
    assert r2.status_code == 202, r2.text
    assert captured_turn2["stated_dims"] == (20.0, 20.0, 20.0)
    assert captured_turn2["stated_dims"] != (0.0, 0.0, 0.0)


# ---------------------------------------------------------------------------
# Exhausted loop: no version, no version-created frame
# ---------------------------------------------------------------------------


def test_chat_exhausted_creates_no_version_and_no_frame(app_with_versions):
    """EXHAUSTED LOOP (issue #93): a loop that exhausts its 3 iterations
    (the real core, real IterationRecord) must still create NO version and
    emit NO version-created frame."""
    from d33d.design_loop import BboxInfo

    # A bbox that does NOT match the stated (20,20,20) — the gate fails.
    bbox = BboxInfo(x=20.0, y=50.0, z=20.0, volume=20000.0)

    scad = "W = 20; cube([W, 1, 1]);"  # D/H are not named parameters

    def _loop_impl(scad_text, bbox_):
        async def _render(scad_source, defines):
            return _ok_render(scad_source)

        async def _loop(app, **kwargs):
            from d33d.design_loop import run_design_loop_async

            # The bbox gate fails (the render's extents do not match the
            # stated triple), so the loop cannot pass and exhausts.
            return await run_design_loop_async(
                photo=kwargs["photo"],
                chat_history=kwargs["chat_history"],
                stated_dims=kwargs["stated_dims"],
                render_fn=_render,
                llm_fn=_stub_llm(scad_text),
                bbox_fn=lambda r: bbox_,
            )

        return _loop

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop_impl(scad, bbox)
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "Create a 20mm cube", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        assert source is not None
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return r, frames, timeline

    r, frames, timeline = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # No version was created.
    assert timeline == [], "exhausted loop must not create a version"
    # No version-created frame.
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert not vc, "exhausted loop must not emit a version-created frame"
    # The terminal frame is an error.
    assert frames[-1][0] == "error"


# ---------------------------------------------------------------------------
# Region-edits route: version-creation behaviour unchanged (regression guard)
# ---------------------------------------------------------------------------


def test_region_edit_pass_creates_version_unchanged(app_with_versions, tmp_path):
    """REGRESSION GUARD (issue #93): the region-edits route shares the
    adapter — a passing loop still creates a version with the best
    candidate's params and emits the version-created frame. The fix must
    not change this behaviour.

    Note: the region-edits route passes (0,0,0) for a fresh project (the
    bbox gate then abstains). The version's params are the render's defines
    map (W/D/H from the stated dims) — which is empty for a fresh project
    (the abstained case). This test asserts the version IS created (the
    region-edit pass path is unchanged) and the version-created frame IS
    emitted (the adapter's version-creation behaviour is preserved)."""
    scad = "W = 11; D = 22; H = 33; cube([W, D, H]);"
    from pathlib import Path

    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=11.0, y=22.0, z=33.0, volume=8000.0)

    def _loop_impl(scad_text, bbox_, render_fn):
        async def _loop(app, **kwargs):
            return await run_design_loop_async(
                photo=kwargs["photo"],
                chat_history=kwargs["chat_history"],
                stated_dims=kwargs["stated_dims"],
                render_fn=render_fn,
                llm_fn=_stub_llm(scad_text),
                bbox_fn=lambda r: bbox_,
            )

        return _loop

    def _render_with_artifacts(artifact_dir):
        async def _render(scad_source, defines):
            return RenderResult(
                ok=True,
                exit_code=0,
                duration_ms=1,
                error_class="ok",
                stderr="",
                stl=str(Path(artifact_dir) / "model.stl"),
                csg=None,
                views=("v1", "v2", "v3", "v4", "v5", "v6"),
                render_artifact_dir=artifact_dir,
            )

        return _render

    _REGION_EDIT_PNG_BASE64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAA"
        "YAAAAAEABCAwE="
    )

    body = {
        "module_ids": ["curl_3"],
        "view_id": "front",
        "marked_png_base64": _REGION_EDIT_PNG_BASE64,
        "polygon": [
            {"x": 10.0, "y": 10.0},
            {"x": 50.0, "y": 10.0},
            {"x": 30.0, "y": 40.0},
        ],
        "instruction": "open up this spiral",
    }

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        artifact_dir = _write_durable_artifacts(tmp_path)
        # The region-edit route passes (0,0,0) for a fresh project — the
        # gate abstains. To make the loop PASS with a real IterationRecord,
        # the bbox must match the (0,0,0) triple — but the abstain gate
        # passes for ANY bbox when all targets are zero, so any bbox works.
        app_with_versions.state.run_design_loop = _loop_impl(
            scad, bbox, _render_with_artifacts(artifact_dir)
        )
        r = await client.post(f"/api/projects/{pid}/region-edits", json=body)
        source = app_with_versions.state.event_sources.get(pid)
        assert source is not None
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        return r, frames, timeline

    r, frames, timeline = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # A version WAS created (the region-edit pass path is unchanged).
    assert len(timeline) == 1, f"region-edit pass must create a version, got {len(timeline)}"
    assert timeline[0]["name"] == "design"
    # The version's params are the render's defines map (empty for a fresh
    # project — the abstained case: W/D/H were unknown, so they are
    # omitted, never stored as zeros). The version IS still created (a
    # passing loop always materialises a version).
    assert timeline[0]["params"] == {}
    # The version-created frame carries the version id.
    vc = [
        d for e, d in frames if e == "progress" and d.get("step") == "version-created"
    ]
    assert vc, "no version-created progress frame"
    assert vc[0]["version_id"] == timeline[0]["id"]
    # The terminal frame is a done.
    assert frames[-1][0] == "done"


# ---------------------------------------------------------------------------
# Finalize route: the new precedence and the semantic flip
# ---------------------------------------------------------------------------


def test_finalize_real_iteration_record_precedes_seed(app_with_versions):
    """FINALIZE PRECEDENCE (issue #93): with a REAL IterationRecord
    (carrying a populated ``params`` field), the best candidate's params
    WIN over the body's params (the loop's result is authoritative).
    This pins the new precedence that the dead getattr never exercised."""
    scad = "W = 50; D = 40; H = 30; cube([W, D, H]);"
    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=50.0, y=40.0, z=30.0, volume=60000.0)

    def _loop_impl(scad_text, bbox_):
        async def _render(scad_source, defines):
            return _ok_render(scad_source)

        def _loop(app, **kwargs):
            import concurrent.futures

            def _run_in_thread():
                return asyncio.run(run_design_loop_async(
                    photo=kwargs["photo"],
                    chat_history=kwargs["chat_history"],
                    stated_dims=(50.0, 40.0, 30.0),  # the test's stated dims
                    render_fn=_render,
                    llm_fn=_stub_llm(scad_text),
                    bbox_fn=lambda r: bbox_,
                ))

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_run_in_thread)
                return future.result()

        return _loop

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop_impl(scad, bbox)
        # The body seeds params {"W": 1, "D": 1, "H": 1} — the loop's
        # result ({"W": 50.0, "D": 40.0, "H": 30.0}) must win.
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"params": {"W": 1, "D": 1, "H": 1}, "name": "the seed"},
        )
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 201, r.text
    version = r.json()
    # The loop's result overwrote the seed (the route's direct
    # ``result.best.params`` read succeeds for a real IterationRecord —
    # the new precedence that the dead getattr never exercised).
    assert version["params"] == {"W": 50.0, "D": 40.0, "H": 30.0}
    assert version["name"] == "the seed"


def test_finalize_semantic_flip_fresh_project_pass_now_201(app_with_versions):
    """SEMANTIC FLIP (issue #93, stated explicitly): a finalize pass on a
    FRESH project (no body params, no prior version) with a REAL
    IterationRecord — the best candidate's params are now read (they used
    to be dead), so the route returns 201 with the candidate's params
    instead of the 502 ("design loop passed but produced no parameter
    set") that the dead getattr produced.
    """
    scad = "W = 20; D = 20; H = 20; cube([W, D, H]);"
    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    def _loop_impl(scad_text, bbox_):
        async def _render(scad_source, defines):
            return _ok_render(scad_source)

        def _loop(app, **kwargs):
            import concurrent.futures

            def _run_in_thread():
                return asyncio.run(run_design_loop_async(
                    photo=kwargs["photo"],
                    chat_history=kwargs["chat_history"],
                    stated_dims=(20.0, 20.0, 20.0),  # the test's stated dims
                    render_fn=_render,
                    llm_fn=_stub_llm(scad_text),
                    bbox_fn=lambda r: bbox_,
                ))

            with concurrent.futures.ThreadPoolExecutor() as pool:
                future = pool.submit(_run_in_thread)
                return future.result()

        return _loop

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop_impl(scad, bbox)
        # NO body params, NO prior version — the old code 502'd here.
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        return r

    r = run_async(app_with_versions, _call)
    # The flip: 201, not 502.
    assert r.status_code == 201, (
        f"the semantic flip: a fresh-project finalize pass with a real "
        f"IterationRecord must now return 201 (the dead getattr used to "
        f"fall through to the 502), got {r.status_code}: {r.text}"
    )
    version = r.json()
    assert version["params"] == {"W": 20.0, "D": 20.0, "H": 20.0}


def test_finalize_stub_without_params_still_falls_back(app_with_versions):
    """FINALIZE FALLBACK PRESERVED (issue #93): a duck-typed stub WITHOUT
    a ``params`` attribute (the shape the old tests used) still falls
    through to the seed (body params or latest-version snapshot) — the
    fallback path is unchanged in meaning."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]

        # A stub with no params attribute (the old test shape) — the
        # route's ``result.best.params`` read raises AttributeError, which
        # the route maps to the same 502 the old code produced for the
        # missing-param-set case (the fallback path is preserved for
        # stubs).
        class _NoParamsBest:
            pass

        class _R:
            status = "pass"
            best = _NoParamsBest()

        app_with_versions.state.run_design_loop = lambda: _R()
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"params": {"W": 9}},
        )
        return r

    r = run_async(app_with_versions, _call)
    # The stub without a params attribute falls back to the seed (body
    # params) — the fallback path is preserved for stubs. The route
    # returns 201 with the seed params (the same behaviour the old code
    # produced for the missing-param-set case).
    assert r.status_code == 201, r.text
    assert r.json()["params"] == {"W": 9}


# ---------------------------------------------------------------------------
# Rename guard: dataclasses.fields + AST (mirrors the #89 guard)
# ---------------------------------------------------------------------------


def test_resolve_version_create_reads_declared_fields():
    """RENAME GUARD (issue #93): every attribute ``_resolve_version_create``
    reads off the ``best`` candidate is a DECLARED ``IterationRecord``
    field — asserted via ``dataclasses.fields`` and a source-level AST
    check, so a future rename to a non-existent name fails LOUDLY here
    instead of silently yielding ``None`` (the exact bug: the reader once
    ``getattr``-ed a non-existent attribute name and no version was ever
    created).
    """
    import ast
    import inspect

    from d33d.design_loop_events import _resolve_version_create

    declared = {f.name for f in dataclasses.fields(IterationRecord)}
    # The field the fix adds must be declared.
    assert "params" in declared, (
        "params is not a declared IterationRecord field — the resolver "
        "would read a non-existent attribute and no version would be "
        "created (the issue #93 bug)"
    )
    # AST check: every ``best.<attr>`` read in the resolver must target a
    # declared field (a misspelled getattr cannot be caught at runtime —
    # it returns None silently, which is how this bug shipped). Only
    # direct attribute reads count — a getattr's target is a string
    # constant in the AST, not an Attribute node, so the string-literal
    # check below covers that case separately.
    source = inspect.getsource(_resolve_version_create)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Attribute)
            and isinstance(node.value, ast.Name)
            and node.value.id == "best"
        ):
            assert node.attr in declared, (
                f"_resolve_version_create references best.{node.attr!r}, "
                f"which is not a declared IterationRecord field"
            )
    # The resolver must NOT use a duck-typed getattr against a string
    # literal for the best candidate's params (the original bug). The
    # getattr's target is a string constant in the AST, so this greps the
    # source text directly.
    assert 'getattr(best, "params"' not in source and "getattr(best, 'params'" not in source, (
        "the resolver must read best.params as a declared attribute, not "
        "via a duck-typed getattr against a string literal"
    )


def test_iteration_record_has_params_field():
    """RENAME GUARD (issue #93, part a): ``params`` is a declared field of
    ``IterationRecord`` (asserted via ``dataclasses.fields`` — a rename or
    deletion fails loudly)."""
    declared = {f.name for f in dataclasses.fields(IterationRecord)}
    assert "params" in declared, (
        f"params is not a declared IterationRecord field: {sorted(declared)}"
    )


def test_resolve_version_create_no_dead_getattr(app_with_versions):
    """The sibling defect in ``versions_routes.py``'s finalize route: the
    route must read the best candidate's params as a DECLARED
    ``IterationRecord`` field, not via a duck-typed ``getattr`` against a
    string literal. ``getattr(obj, "attr", None)`` and ``obj.attr`` are
    semantically identical when the attribute exists (both return it), so
    the guard checks the AST: a getattr's target is a ``Constant`` (string
    literal) in the AST, while a declared-field read is an ``Attribute``
    node. If the route regresses to a duck-typed getattr, this test fails.
    """
    import ast
    import inspect

    import d33d.versions_routes as routes_mod

    # The AST check above is the definitive guard: a getattr's target is
    # a ``Constant`` (string literal) in the AST, while a declared-field
    # read is an ``Attribute`` node. If the route regresses to a
    # duck-typed getattr against the string literal "params" on
    # result.best, the AST check above fails. A substring check would
    # false-positive on docstrings, so it is intentionally omitted.
    source = inspect.getsource(routes_mod)
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
        ):
            first = node.args[0]
            second = node.args[1]
            # The dead pattern: getattr(result.best, "params", ...)
            is_result_best = (
                isinstance(first, ast.Attribute)
                and isinstance(first.value, ast.Name)
                and first.value.id == "result"
                and first.attr == "best"
            )
            is_params_literal = (
                isinstance(second, ast.Constant) and second.value == "params"
            )
            if is_result_best and is_params_literal:
                raise AssertionError(
                    "versions_routes.finalize uses a duck-typed getattr "
                    "against the string literal 'params' on result.best — "
                    "the issue #93 sibling defect (a dead read that "
                    "silently returns None for a real IterationRecord). "
                    "Read the declared field instead."
                )


# ---------------------------------------------------------------------------
# The conversion helper: _params_for_record
# ---------------------------------------------------------------------------


def test_params_for_record_numeric_conversion():
    """The conversion: numeric strings → float, non-numeric → string."""
    m = _dim_params((20.0, 15.0, 10.0), {"clearance": "0.4", "layer": "PLA"})
    out = _params_for_record(m)
    assert out == {"W": 20.0, "D": 15.0, "H": 10.0, "clearance": 0.4, "layer": "PLA"}
    assert isinstance(out["W"], float)
    assert isinstance(out["clearance"], float)
    assert isinstance(out["layer"], str)


def test_params_for_record_abstained_omits_zero_axes():
    """A zero triple (the abstained case) → empty dict (no stored zeros)."""
    m = _dim_params((0.0, 0.0, 0.0), {})
    out = _params_for_record(m)
    assert out == {}


def test_params_for_record_partial_triple_omits_only_unknown_axes():
    """A partial triple: known axes are stored, unknown axes are omitted."""
    m = _dim_params((20.0, 0.0, 10.0), {})
    out = _params_for_record(m)
    assert out == {"W": 20.0, "H": 10.0}
    assert "D" not in out


def test_params_for_record_caller_defines_override():
    """A caller-supplied W (via defines) is respected (setdefault
    semantics in _dim_params) and converted to a float."""
    m = _dim_params((0.0, 0.0, 0.0), {"W": "50"})
    out = _params_for_record(m)
    assert out == {"W": 50.0}
    assert "D" not in out
    assert "H" not in out
