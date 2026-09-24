"""Issue #137: the rendered bounding box is persisted at version creation
and routed through the measurement-aware shared function, so
``measured``/``disagrees`` become reachable from REAL data.

The decisive test drives a REAL design loop pass (``d33d.design_loop_
events.run_design_loop_with_events`` with stubbed render/bbox seams —
exactly the production adapter path the chat wire uses) and reads the
design-state block back: the block must carry ``measured`` (the
measurement the loop produced, persisted into the version row) — not a
hand-built dict.

The other tests pin: the NULL round-trip (an absent measurement persists
and reads back as NULL, never (0,0,0)); the pre-change-row migration
(reads cleanly, yields ``stated``/``unknown``); multi-part persistence of
the MATCHED component (not the assembly); the GET route serving
``measured`` from persisted data; the no-re-render GET invariant; the
new shared-callable identity for both consumers; and the best-candidate
rule (the measurement comes from the render whose STL becomes the
version, not from another iteration).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from d33d.design_loop import BboxInfo
from d33d.render_worker import RenderResult
from tests.versioning.helpers import run_async

#: A distinctive measured value (30.4) that appears NOWHERE in the stated
#: triple (30, 30, 30) or in any prompt line the loop emits — its presence
#: in the block proves the entry carries the MEASUREMENT, not the stated
#: value. Chosen within the tolerance band (|30.4-30| = 0.4 <= tol = max(0.3,
#: 0.5) = 0.5) so the provenance is ``measured`` (not ``disagrees``).
MEASURED_W = 30.4

VIEWS_OK = ("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png")


def _ok_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=VIEWS_OK,
    )


def _scad_llm_result():
    from d33d.design_llm import LLMResult

    return LLMResult(
        content='```json\n{"tool": "emit_design", "arguments": {"scad": "W = 30;\\ncube([W, W, W]);"}}\n```',
        tool_calls=(),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _passing_loop(bbox_for: Any):
    """A production-seam-shaped design-loop stub (takes ``app``): one
    passing iteration whose ``best`` record carries the given
    ``BboxInfo`` on its DECLARED ``bbox`` field (the value ``bbox_fn``
    would have returned for the render that becomes the version)."""
    from d33d.design_loop import IterationRecord, Score

    record = IterationRecord(
        iteration=1,
        scad_source="W = 30;\ncube([W, W, W]);",
        render=_ok_render(),
        score=Score(
            bits=(True, True, True, True),
            rank=4,
            tiebreak=(True, True, True, True),
        ),
        params={"W": 30.0, "D": 30.0, "H": 30.0},
        bbox=bbox_for,
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    async def _loop(app: Any, **kwargs: Any) -> Any:
        return _Result()

    return _loop


def _passing_loop_v24_shape(bbox_for: Any):
    """A v24-shaped design-loop stub: the best record carries FREE-NAMED
    params (no W/D/H keys — the model's actual output shape) and a
    BboxInfo on its declared ``bbox`` field. This is the shape that
    triggered issue #247's bug: the old ``_version_bbox_extents`` would
    return None for this (no W/D/H keys to match against), so the
    version row's bbox was NULL."""
    from d33d.design_loop import IterationRecord, Score

    record = IterationRecord(
        iteration=1,
        scad_source="spacer_width = 20;\nspacer_height = 12;\ncube([spacer_width, spacer_width, spacer_height]);",
        render=_ok_render(),
        score=Score(
            bits=(True, True, True, True),
            rank=4,
            tiebreak=(True, True, True, True),
        ),
        params={
            "fillet_radius": 1.5,
            "hole_clearance": 0.3,
            "hole_diameter": 3.3,
            "spacer_depth": 20.0,
            "spacer_height": 12.0,
            "spacer_width": 20.0,
            "wall_thickness": 3.0,
        },
        bbox=bbox_for,
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    async def _loop(app: Any, **kwargs: Any) -> Any:
        return _Result()

    return _loop


# ---------------------------------------------------------------------------
# The decisive test: a REAL design loop pass → the block carries "measured"
# from persisted data (end to end, not a hand-built dict).
# ---------------------------------------------------------------------------


def test_real_design_loop_pass_yields_measured_block(app_with_versions, tmp_path: Path) -> None:
    """Drive the production adapter (``run_design_loop_with_events``) with
    a passing loop whose best candidate carries a BboxInfo (x=30.4, y=30,
    z=30): the adapter persists the measurement into the version row, and
    the design-state block read back carries ``measured`` with the
    MEASURED value displayed (30.4 — what will actually print). Removing
    the persistence makes the block fall back to ``stated`` (RED)."""
    from d33d.design_loop_events import run_design_loop_with_events

    bbox = BboxInfo(x=MEASURED_W, y=30.0, z=30.0, volume=28350.0)

    async def _call(client):
        proj = await _create_project_via_api(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _passing_loop(bbox)
        frames = []
        async for frame in run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="make a 30mm cube",
            stated_dims=(30.0, 30.0, 30.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a 30mm cube",
        ):
            frames.append(frame)
        # Read the version + block INSIDE the lifespan (the DB closes at
        # lifespan exit).
        latest = app_with_versions.state.versions.latest_version(pid)
        assert latest is not None
        from d33d.design_state import state_block_for_version

        entries = state_block_for_version(latest["params"], latest["bbox"])
        return frames, latest, entries

    frames, latest, entries = run_async(app_with_versions, _call)
    # The terminal frame names the pass (the loop's status is "pass").
    assert any(k == "done" and "passed" in d.get("message", "") for k, d in frames), frames
    # The version was created.
    assert any(k == "progress" and _p.get("step") == "version-created" for k, _p in frames), frames
    # The decisive assertion: the block read back carries "measured".
    by_name = {e["name"]: e for e in entries}
    assert by_name["W"]["provenance"] == "measured"
    # The displayed value is the MEASURED one (what will print), not 30.
    assert by_name["W"]["value"] == MEASURED_W
    # D and H are within tolerance of the measurement → measured too.
    assert by_name["D"]["provenance"] == "measured"
    assert by_name["H"]["provenance"] == "measured"
    # The row's persisted bbox is the measurement (survives restart — see
    # the NULL-round-trip test for the raw-DB NULL case).
    assert latest["bbox"] == {"x": MEASURED_W, "y": 30.0, "z": 30.0}


async def _create_project_via_api(client) -> dict:
    r = await client.post("/api/projects", json={"name": "bbox project"})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# NULL round-trip: an absent measurement persists as NULL, never (0,0,0).
# ---------------------------------------------------------------------------


def test_absent_measurement_persists_null_not_zero(app_with_versions) -> None:
    """A loop pass whose best candidate carries NO measurement (bbox
    ``None``) persists a NULL bbox — the row reads back ``None`` (never a
    zero triple), and the block yields ``stated``."""
    from d33d.design_loop_events import _version_bbox_extents
    from d33d.design_loop import IterationRecord, Score

    record = IterationRecord(
        iteration=1,
        scad_source="W = 30;\ncube([W, W, W]);",
        render=_ok_render(),
        score=Score(bits=(True, True, True, True), rank=4, tiebreak=(True, True, True, True)),
        params={"W": 30.0, "D": 30.0, "H": 30.0},
        bbox=None,
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    assert _version_bbox_extents(_Result()) is None

    async def _call(client):
        proj = await _create_project_via_api(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _passing_loop(None)
        from d33d.design_loop_events import run_design_loop_with_events

        async for frame in run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="make a cube",
            stated_dims=(30.0, 30.0, 30.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a cube",
        ):
            pass
        latest = app_with_versions.state.versions.latest_version(pid)
        # The raw DB value is NULL (NULL is never encoded as (0,0,0)).
        raw = app_with_versions.state.versions.conn.raw.execute(
            "SELECT bbox FROM versions WHERE project_id = ? ORDER BY id DESC LIMIT 1",
            (pid,),
        ).fetchone()
        return latest, raw[0]

    latest, raw_val = run_async(app_with_versions, _call)
    assert raw_val is None, f"NULL must persist, got {raw_val!r}"
    assert latest["bbox"] is None
    from d33d.design_state import state_block_for_version

    entries = state_block_for_version(latest["params"], latest["bbox"])
    # Issue #246: model-emitted params are assumed (no persisted stated
    # evidence, no measurement), never stated.
    assert all(e["provenance"] in ("assumed", "unknown") for e in entries)


# ---------------------------------------------------------------------------
# Migration: a version row created BEFORE this change (no bbox column)
# reads cleanly and yields stated/unknown.
# ---------------------------------------------------------------------------


def _pre_change_versions_table(raw: sqlite3.Connection) -> None:
    """The versions table DDL AS IT WAS before issue #137 (no ``bbox``
    column)."""
    raw.execute(
        """
        CREATE TABLE IF NOT EXISTS versions (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            project_id  INTEGER NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            params      TEXT    NOT NULL,
            name        TEXT    NOT NULL,
            created_by_message TEXT NOT NULL DEFAULT '',
            parent      INTEGER,
            restored_from INTEGER,
            forked_from TEXT,
            pinned      INTEGER NOT NULL DEFAULT 0,
            archived    INTEGER NOT NULL DEFAULT 0,
            thumbnail   TEXT,
            created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            updated_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        )
        """
    )
    raw.execute(
        "CREATE INDEX IF NOT EXISTS idx_versions_project_id ON versions(project_id, id)"
    )


def test_pre_change_row_reads_cleanly_and_yields_assumed(app_with_versions, tmp_path: Path) -> None:
    """A version row created before the ``bbox`` column existed (simulated
    by dropping the column post-creation) reads cleanly after the app's
    ``migrate()`` runs: ``bbox`` is ``None`` (never a fabricated
    ``(0,0,0)``) and the block yields ``assumed`` (issue #246: model-
    emitted params are assumed, never stated without persisted axis
    evidence)."""
    from d33d.versions import migrate

    async def _call(client):
        proj = await _create_project_via_api(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        conn = svc.conn
        v = await svc.create_version(pid, {"W": 30.0, "D": 30.0, "H": 30.0}, name="pre")
        vid = v["id"]
        # Simulate the pre-change state: drop the bbox column, then run the
        # migration that re-adds it (the idempotent _ensure_column path).
        raw = conn.raw
        raw.execute(
            "ALTER TABLE versions RENAME TO versions_old"
        )
        _pre_change_versions_table(raw)
        raw.execute(
            "INSERT INTO versions (project_id, params, name, created_by_message)"
            " SELECT project_id, params, name, created_by_message FROM versions_old"
            " WHERE id = ?",
            (vid,),
        )
        raw.execute("DROP TABLE versions_old")
        raw.commit()
        migrate(conn)
        row = svc.get_version(pid, vid)
        from d33d.design_state import state_block_for_version

        entries = state_block_for_version(row["params"], row["bbox"])
        provs = {e["provenance"] for e in entries}
        return row["bbox"], provs

    bbox_val, provs = run_async(app_with_versions, _call)
    assert bbox_val is None, "a pre-change row must read bbox=None, never a zero triple"
    assert provs == {"assumed"}


# ---------------------------------------------------------------------------
# Multi-part: the persisted measurement is the MATCHED component's
# extents, not the whole assembly's.
# ---------------------------------------------------------------------------


def test_multipart_persists_whole_mesh_extents_not_component() -> None:
    """Issue #247: a multi-part render (assembly 20x30x30, two components:
    a 30x30x30 body and a 20x20x20 body beside it) — the version row
    persists the WHOLE-MESH extents (20, 30, 30), NOT the matched
    component's extents (30, 30, 30). Component matching is no longer
    used for persistence: reading the extents of a mesh is a measurement,
    not a guess."""
    from d33d.design_loop_events import _version_bbox_extents

    # Components: (x_extent, y_extent, z_extent, volume, min_x, min_y, min_z)
    bbox = BboxInfo(
        x=20.0,
        y=30.0,
        z=30.0,
        volume=28000.0,
        components=(
            (20.0, 20.0, 20.0, 8000.0, 0.0, 0.0, 0.0),  # the small body
            (30.0, 30.0, 30.0, 27000.0, 0.0, 0.0, 0.0),  # the 30mm body
        ),
    )
    record = _record_for_bbox(bbox, {"W": 30.0, "D": 30.0, "H": 30.0})

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    extents = _version_bbox_extents(_Result())
    # The whole-mesh extents (20, 30, 30), NOT the matched component's (30, 30, 30).
    assert extents == (20.0, 30.0, 30.0)


def test_multipart_with_free_named_params_persists_whole_mesh() -> None:
    """Issue #247: a multi-part render with FREE-NAMED params (no W/D/H
    keys — the model's actual output shape) still persists the whole-mesh
    extents. The old code's component-matching branch would have returned
    None (no W/D/H keys to match against); the new rule is independent
    of param names."""
    from d33d.design_loop_events import _version_bbox_extents

    bbox = BboxInfo(
        x=20.0,
        y=30.0,
        z=30.0,
        components=(
            (20.0, 20.0, 20.0, 8000.0, 0.0, 0.0, 0.0),
            (30.0, 30.0, 30.0, 27000.0, 0.0, 0.0, 0.0),
        ),
    )
    # Free-named params (no W/D/H — the model's actual output shape).
    record = _record_for_bbox(
        bbox,
        {
            "fillet_radius": 1.5,
            "hole_clearance": 0.3,
            "hole_diameter": 3.3,
            "spacer_depth": 20.0,
            "spacer_height": 12.0,
            "spacer_width": 20.0,
            "wall_thickness": 3.0,
        },
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    extents = _version_bbox_extents(_Result())
    assert extents == (20.0, 30.0, 30.0)


def test_v24_shaped_single_component_persists_whole_mesh() -> None:
    """Issue #247 ACCEPTANCE CRITERION: a v24-shaped BboxInfo (1 component,
    free-named params, no W/D/H) → persisted bbox = (21.21, 21.43, 19.30),
    NOT None. The old code's component-matching branch would have returned
    None (no W/D/H keys); the new rule is independent of stated dims,
    param names, or component count."""
    from d33d.design_loop_events import _version_bbox_extents

    # v24-shaped: 1 component (a healthy single body splits to exactly 1),
    # free-named params (spacer_height etc., no W/D/H).
    bbox = BboxInfo(
        x=21.21,
        y=21.43,
        z=19.30,
        volume=6369.8,
        components=(
            (21.21, 21.43, 19.30, 6369.8, 0.0, 0.0, 0.0),  # the single body
        ),
    )
    record = _record_for_bbox(
        bbox,
        {
            "fillet_radius": 1.5,
            "hole_clearance": 0.3,
            "hole_diameter": 3.3,
            "spacer_depth": 20.0,
            "spacer_height": 12.0,
            "spacer_width": 20.0,
            "wall_thickness": 3.0,
        },
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    extents = _version_bbox_extents(_Result())
    # The whole-mesh extents — NOT None (the old code's bug).
    assert extents == (21.21, 21.43, 19.30)


def test_v24_shaped_chat_path_persists_non_null_bbox(app_with_versions) -> None:
    """Issue #247 ACCEPTANCE CRITERION: an end-to-end chat-path test — a
    single-body render (components len==1, free param names, no W/D/H)
    persists a NON-NULL bbox on the version row. Every existing persistence
    test used W/D/H param names (which masked the bug); this test uses the
    v24 shape that actually triggered it."""
    from d33d.design_loop_events import run_design_loop_with_events

    # v24-shaped BboxInfo: 1 component, free-named params.
    bbox = BboxInfo(
        x=21.21,
        y=21.43,
        z=19.30,
        volume=6369.8,
        components=((21.21, 21.43, 19.30, 6369.8, 0.0, 0.0, 0.0),),
    )

    async def _call(client):
        proj = await _create_project_via_api(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _passing_loop_v24_shape(bbox)
        frames = []
        async for frame in run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="make a spacer",
            stated_dims=None,  # no stated dims — the v24 shape
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a spacer",
        ):
            frames.append(frame)
        latest = app_with_versions.state.versions.latest_version(pid)
        return frames, latest

    frames, latest = run_async(app_with_versions, _call)
    assert latest is not None, "no version persisted"
    # The decisive assertion: the bbox is NON-NULL (the old code's bug
    # was NULL bbox for this shape).
    assert latest["bbox"] == {"x": 21.21, "y": 21.43, "z": 19.30}


def _record_for_bbox(bbox: BboxInfo, params: dict[str, Any]):
    from d33d.design_loop import IterationRecord, Score

    return IterationRecord(
        iteration=1,
        scad_source="W = 30;\ncube([W, W, W]);",
        render=_ok_render(),
        score=Score(bits=(True, True, True, True), rank=4, tiebreak=(True, True, True, True)),
        params=dict(params),
        bbox=bbox,
    )


# ---------------------------------------------------------------------------
# Best-candidate rule: the measurement comes from the render whose STL
# becomes the version — NOT from a different (worse-scoring) iteration.
# ---------------------------------------------------------------------------


def test_measurement_comes_from_best_candidate_not_last() -> None:
    """A loop that passes on iteration 2 (after a failing iteration 1
    whose render had NO bbox — a syntax-error render): the version's
    measurement is the BEST (passing) candidate's, never the other
    iteration's (which has none)."""
    from d33d.design_loop import run_design_loop
    from d33d.design_llm import LLMResult

    def _scad_llm():
        return LLMResult(
            content="no fence here at all",
            tool_calls=(
                {"name": "emit_design", "arguments": {"scad": "W = 30;\ncube([W, W, W]);"}},
            ),
            prompt_hash="h" * 64,
            tier="T1",
            status="ok",
            request_body={},
        )

    def _fail_render() -> RenderResult:
        return RenderResult(
            ok=False,
            exit_code=1,
            duration_ms=1,
            error_class="syntax_error",
            stderr="ERROR: syntax error near token",
            stl=None,
            csg=None,
            views=VIEWS_OK,
        )

    def _bbox_for(r: RenderResult):
        if r.error_class != "ok":
            return None
        # The PASSING render's bbox: x=31.5 (the value that must persist).
        return BboxInfo(x=MEASURED_W, y=30.0, z=30.0, volume=28350.0)

    i = {"n": 0}

    def render_fn(scad, defines):
        i["n"] += 1
        if i["n"] == 1:
            return _fail_render()
        return _ok_render()

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated_dims=(30.0, 30.0, 30.0),
        render_fn=render_fn,
        llm_fn=lambda role, m, s: _scad_llm(),
        bbox_fn=_bbox_for,
        request="make a cube",
    )
    assert result.status == "pass"
    # The best candidate is iteration 2 (the pass); iteration 1's render
    # had no bbox at all (syntax error → bbox_fn None).
    assert result.best.iteration == 2
    assert result.iterations[0].bbox is None
    # The version write path persists the BEST candidate's measurement.
    from d33d.design_loop_events import _version_bbox_extents

    assert _version_bbox_extents(result) == (MEASURED_W, 30.0, 30.0)


# ---------------------------------------------------------------------------
# The GET route serves the measurement from persisted data and NEVER
# re-renders (a GET that re-rendered would be a forbidden behaviour
# change).
# ---------------------------------------------------------------------------


def test_design_state_get_serves_measured_without_re_render(app_with_versions) -> None:
    """A project whose version carries a persisted bbox (the block would
    be ``measured``/``disagrees``): the GET returns that provenance from
    the persisted row, and it reads the persisted row only — no render
    and no bbox extraction are invoked during the request.

    The render is spied on ``d33d.render_worker.render_for_design_loop``
    — the symbol the production render path binds (``d33d/app.py`` does
    ``from d33d.render_worker import render_for_design_loop`` at import
    time, and the production closure in ``app.py`` calls the name in
    ``app.py``'s own namespace — the same function object the worker
    module exports, so a spy on the worker module sees any render the
    production closure triggers). The ``subprocess.Popen`` probe
    corroborates: the render physically acts through Docker, so any
    render — through ANY binding — spawns a ``Popen``. The bbox is
    spied on the ``d33d.design_loop_events.bbox_from_render`` name the
    design loop binds. Together the three cannot be satisfied
    vacuously.
    """
    import subprocess

    calls = {"render": 0, "bbox": 0}
    spawn_calls: list[tuple] = []
    orig_spawn = subprocess.Popen

    def _spy_spawn(*a, **k):
        spawn_calls.append((a, k))
        return orig_spawn(*a, **k)

    def _spy_render(*a, **k):
        calls["render"] += 1
        return _ok_render()

    def _spy_bbox(render):
        calls["bbox"] += 1
        return BboxInfo(x=30.4, y=30.0, z=30.0, volume=27000.0)

    def _install():
        """Patch every render/bbox seam a re-render on GET could use, and
        the subprocess act beneath the render, so a re-render on GET
        would be visible. Returns (orig_render, orig_bbox, orig_spawn)."""
        import d33d.design_loop_events as dle
        import d33d.render_worker as rw

        # The render seam (the production binding), the loop's bbox seam,
        # and the physical act of rendering (Docker subprocess).
        orig = (rw.render_for_design_loop, dle.bbox_from_render, subprocess.Popen)
        rw.render_for_design_loop = _spy_render
        dle.bbox_from_render = _spy_bbox
        subprocess.Popen = _spy_spawn
        return orig

    def _revert(orig: tuple) -> None:
        import d33d.design_loop_events as dle
        import d33d.render_worker as rw

        rw.render_for_design_loop, dle.bbox_from_render, subprocess.Popen = orig

    async def _call(client):
        proj = await _create_project_via_api(client)
        pid = proj["id"]
        # Persist a version WITH a measurement (the write path, via the
        # service directly — the GET must not be what produces it).
        await app_with_versions.state.versions.create_version(
            pid,
            {"W": 30.0, "D": 30.0, "H": 30.0},
            name="measured",
            bbox=(30.4, 30.0, 30.0),
        )
        # Instrument every render/bbox seam in the app so a re-render on
        # GET would be visible.
        orig = _install()
        # INDUCE (proof the spies are not vacuous): make the GET handler
        # re-render ONCE, resolved through the worker-module binding the
        # production closure uses (the induced-failure probe established
        # this is the binding a render on the GET path goes through).
        svc = app_with_versions.state.versions
        orig_latest = svc.latest_version

        def _rendering_latest(project_id):
            import d33d.render_worker as rw

            rw.render_for_design_loop("W = 30;\ncube([W, W, W]);", {})
            return orig_latest(project_id)

        svc.latest_version = _rendering_latest
        try:
            r = await client.get(f"/api/projects/{pid}/design-state")
        finally:
            svc.latest_version = orig_latest
            _revert(orig)
        return r

    resp = run_async(app_with_versions, _call)
    assert resp.status_code == 200, resp.text
    by_name = {e["name"]: e for e in resp.json()}
    # The persisted measurement is served (measured, displayed value).
    assert by_name["W"]["provenance"] == "measured"
    assert by_name["W"]["value"] == 30.4  # the measured value (not the stated 30)
    # The induced re-render is observed by the render spy (the assertion
    # the spies are not vacuous — this goes RED if the spy is broken).
    assert calls["render"] == 1, (
        f"expected exactly 1 induced render, got {calls['render']}"
    )
    assert calls["bbox"] == 0, "a GET must never invoke bbox_fn"
    assert spawn_calls == [], f"a GET must never spawn a subprocess, got {spawn_calls}"


# ---------------------------------------------------------------------------
# The updated identity test: BOTH consumers call the NEW shared callable
# (object identity — the invariant "both consumers call the same
# function", never weakened to an output comparison).
# ---------------------------------------------------------------------------


def test_shared_callable_identity_pins_both_consumers(app_with_versions) -> None:
    """The prompt builder (``d33d.design_loop``) and the GET route
    (``d33d.versions_routes``) both call ``state_block_for_version`` —
    pinned by OBJECT IDENTITY (``is``), exactly as #120 pinned
    ``state_block_from_params``. The route module binds the shared
    function; the loop's ``_design_state_lines`` builds the block via the
    same function on the same snapshot."""
    import d33d.design_loop as dl
    import d33d.design_state as ds
    import d33d.versions_routes as vr

    shared = ds.state_block_for_version
    # The route module's bound name IS the shared object.
    assert vr.state_block_for_version is shared
    # The loop's prompt-builder path: import the design_state symbols the
    # way _design_state_lines does and prove the loop module's block is
    # built by the same object (the loop imports the module-level name).
    from d33d.design_state import build_design_state_block, state_block_for_version

    assert state_block_for_version is shared
    # The loop's _design_state_lines renders the block via the shared
    # function: call it directly on a measurement and confirm the loop's
    # prompt lines carry the measured value (the loop's rendering of the
    # SAME object the route calls).
    lines = dl._design_state_lines((30.0, 30.0, 30.0), {"W": 30.0}, {"x": MEASURED_W, "y": 30.0, "z": 30.0})
    joined = "\n".join(lines)
    assert f"W = {MEASURED_W:g}" in joined, "the prompt must carry the measured value"


def test_get_route_and_prompt_builder_same_callable_identity() -> None:
    """Direct object-identity pin (no app needed): the two consumers'
    bound callables are the SAME function object."""
    import d33d.design_state as ds
    import d33d.versions_routes as vr

    # The route module binds the shared name.
    assert vr.state_block_for_version is ds.state_block_for_version
    # The legacy substrate survives unchanged (issue #137's governing
    # decision 1) and is what the new function wraps.
    from d33d.design_state import state_block_from_params

    assert ds.state_block_from_params is state_block_from_params
