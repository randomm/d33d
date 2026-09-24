"""Issue #247 (blocking gap): the loop-facing routes (chat, finalize,
region-edit) must feed the bbox gate the CURRENT run's per-axis confirmed
set — falling back to the latest version row's persisted per-axis set
(``versions.stated_dims``) when the current message confirms nothing —
as a (W, D, H) triple with 0.0 for unconfirmed axes (the #91
zero-means-unknown convention). The pre-fix defect: the routes handed the
loop a FULL triple-or-None, so a partial confirmation (e.g. "a spacer to
lift a shelf 12 mm tall" → only H) abstained the gate entirely in
production, and a follow-up message on a version whose W/D/H live under
free names (the model never emits W/D/H keys) also abstained — the gate
never measured a confirmed axis for real users.

Tests drive the REAL routes (stub only the loop callable, capture the
kwargs it receives):

- chat "make it 20mm tall, H: 20" on a fresh project → (0.0, 0.0, 20.0)
  (the dimension protocol's axis-prefixed forms — ``H: 12`` / "a 20mm
cube" — are the statement shapes it confirms; a bare single number like
  "12 mm tall" is deliberately NOT a stated envelope, the #91
  fabricate-don't-measure rule, and the per-axis path inherits that)
- chat follow-up with no dimensions after a version whose
  ``stated_dims`` is ``{"H": 12}`` → (0.0, 0.0, 12.0)
- chat with no dimensions anywhere → None (abstain)
- chat "W: 60, D: 45, H: 80" → (60.0, 45.0, 80.0)
- the REAL ``score()`` with the captured partial triple: a bbox of z=19.3
  (the v24 7 mm error) FAILS the bbox bit — the per-axis gate now catches
  it (end-to-end-ish; the loop callable stays stubbed)
- finalize: per-axis source (body stated_dims axes / message extraction)
  with the latest-row fallback, partial → zero-filled triple
- region-edit: latest row's persisted confirmed set (partial →
  zero-filled), no confirmed axis → None (the dead W/D/H param-key read
  is gone)
- consumer: the design-role system prompt (``design_prompt``) renders an
  unconfirmed axis of the zero-filled triple as ``not specified``, never
  "0 mm"
"""

from __future__ import annotations

from typing import Any

from d33d.design_loop import BboxInfo, score
from d33d.design_prompts import design_prompt
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    run_async,
)


def _ok_render(scad_text: str) -> RenderResult:
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
# Helper unit tests: gate_stated_dims
# ---------------------------------------------------------------------------


def test_gate_stated_dims_current_axes_win_no_fallback() -> None:
    from d33d.design_loop_events import gate_stated_dims

    class _Svc:
        def latest_version(self, project_id):
            return {
                "params": {"spacer_height": 5.0},
                "stated_dims": {"W": 5.0, "D": 5.0, "H": 5.0},
            }

    # A non-empty current set short-circuits the fallback entirely.
    assert gate_stated_dims({"H": 12.0}, _Svc(), 1) == (0.0, 0.0, 12.0)


def test_gate_stated_dims_falls_back_on_empty_current() -> None:
    from d33d.design_loop_events import gate_stated_dims

    class _Svc:
        def latest_version(self, project_id):
            return {
                "params": {"spacer_height": 12.0},
                "stated_dims": {"H": 12.0},
            }

    assert gate_stated_dims({}, _Svc(), 1) == (0.0, 0.0, 12.0)
    assert gate_stated_dims(None, _Svc(), 1) == (0.0, 0.0, 12.0)


def test_gate_stated_dims_none_only_when_no_axis_anywhere() -> None:
    from d33d.design_loop_events import gate_stated_dims

    class _Svc:
        def latest_version(self, project_id):
            return None

    assert gate_stated_dims({}, _Svc(), 1) is None
    assert gate_stated_dims({"H": 0.0}, _Svc(), 1) is None

    class _SvcNull:
        def latest_version(self, project_id):
            return {"params": {"a": 1.0}, "stated_dims": None}

    assert gate_stated_dims(None, _SvcNull(), 1) is None


# ---------------------------------------------------------------------------
# Chat route: the per-axis confirmed set reaches the loop (real route,
# stub loop)
# ---------------------------------------------------------------------------


async def _drive_chat_capture(app, client, pid: int, body: dict) -> dict:
    """POST ``body`` to /chat with a capturing stub loop; return the
    captured loop kwargs (drained to the terminal frame)."""
    captured: dict[str, Any] = {}

    def _loop(app, **kwargs):
        captured.update(kwargs)
        return None  # non-pass → terminal error frame; no version write

    app.state.run_design_loop = _loop
    r = await client.post(f"/api/projects/{pid}/chat", json=body)
    assert r.status_code == 202, r.text
    source = app.state.event_sources.get(pid)
    assert source is not None
    async for _event, _data in source:
        if _event in ("done", "error"):
            break
    return captured


def test_chat_partial_statement_feeds_zero_filled_triple(app_with_versions):
    """DECISIVE (issue #247): a message confirming ONLY H ("make it 20mm
tall, H: 20" — the protocol's axis-prefixed forms are its confirmed
    statement shapes) on a FRESH project. The loop receives (0.0, 0.0,
    20.0) — the per-axis confirmed set — so the gate measures H and
    abstains per-axis on W/D. Pre-fix this same message handed the loop
    None (the full-triple extraction fails on a partial statement and the
    fresh project has no version to fall back to) — the gate abstained
    entirely."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_chat_capture(
            app_with_versions, client, proj["id"],
            {"message": "make it 20mm tall, H: 20", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 20.0)


def test_chat_follow_up_falls_back_to_persisted_partial_set(app_with_versions):
    """Follow-up message with NO dimensions on a project whose latest
    version row's persisted ``stated_dims`` is ``{"H": 12}`` (partial) —
    the gate is fed the latest row's per-axis set as a zero-filled
    triple: (0.0, 0.0, 12.0). Pre-fix: the full-triple fallback
    (``latest_version_stated_dims``) yields None on a partial row, so the
    gate abstained entirely in production."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_height": 12.0, "spacer_width": 30.0},
            stated_dims={"H": 12.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "make it a bit rounder", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_chat_no_dimensions_anywhere_abstains(app_with_versions):
    """No dimensions in the message AND no confirmed axis anywhere →
    None (abstain entirely), never a zero triple."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_chat_capture(
            app_with_versions, client, proj["id"],
            {"message": "make it rounder", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


def test_chat_full_statement_feeds_full_triple(app_with_versions):
    """A full "W: 60, D: 45, H: 80" statement still feeds the full
    (W, D, H) triple (the per-axis set and the full triple agree on a
    complete statement)."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_chat_capture(
            app_with_versions, client, proj["id"],
            {"message": "W: 60, D: 45, H: 80", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (60.0, 45.0, 80.0)


def test_chat_current_axes_beat_persisted_fallback(app_with_versions):
    """A current message that confirms an axis DIFFERENT from the
    latest row's confirmed set: the current run's per-axis set wins
    (no transitive merge — the operator decision is "the current run's
    per-axis confirmed set, falling back ONLY when the current message
    confirms nothing")."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0},
            stated_dims={"W": 12.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "make it 20mm tall, H: 20", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 20.0)


# ---------------------------------------------------------------------------
# The real score() on the captured partial triple: the v24 7 mm error is
# caught (end-to-end-ish — the gate's per-axis check on the route's
# resolved triple)
# ---------------------------------------------------------------------------


def test_real_score_partial_triple_catches_v24_seven_mm_z_error() -> None:
    """The end-to-end-ish test: the REAL ``score()`` with the triple the
    chat route now feeds the loop for a "12 mm tall" statement —
    (0.0, 0.0, 12.0) — and a bbox of z=19.3 (the v24 7 mm error). The
    bbox bit is FALSE: H is confirmed, 19.3 misses 12.0 by more than
    max(1%, 0.5 mm), and the per-axis gate measures the confirmed axis
    (W/D abstain) — pre-fix the same candidate abstained (the route
    handed None) and the error went uncaught."""
    render = _ok_render("h = 12; cube([10, 10, h]);")
    bbox = BboxInfo(x=10.0, y=10.0, z=19.3, volume=1.0)
    s = score(render, (0.0, 0.0, 12.0), bbox=bbox, scad_source="h = 12; cube([10, 10, h]);")
    assert s.bits[2] is False, (
        "the bbox bit must FAIL: H is confirmed at 12.0 and the render is "
        "19.3 (a 7.3 mm error) — the v24 defect class"
    )
    # The W/D abstention is recorded distinctly (the partial pass flag).
    assert s.bbox_abstained is False  # the bit itself failed — no abstain
    # Sanity: the same bbox at z=12.1 PASSES (within max(1%, 0.5mm) of 12)
    # and carries the per-axis abstention flag (W/D unchecked).
    bbox_ok = BboxInfo(x=10.0, y=10.0, z=12.1, volume=1.0)
    s_ok = score(render, (0.0, 0.0, 12.0), bbox=bbox_ok, scad_source="h = 12; cube([10, 10, h]);")
    assert s_ok.bits[2] is True
    assert s_ok.bbox_abstained is True


# ---------------------------------------------------------------------------
# Finalize route: per-axis source + latest-row fallback (real route,
# stub loop)
# ---------------------------------------------------------------------------


async def _drive_finalize_capture(app, client, pid: int, body: dict) -> dict:
    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        # exhausted → 422; the capture happened before the result read.
        class _R:
            status = "exhausted"
            failure_reason = "bbox_out_of_tolerance"
            best = None

        return _R()

    app.state.run_design_loop = _loop
    r = await client.post(f"/api/projects/{pid}/finalize", json=body)
    assert r.status_code in (201, 422), r.text
    return captured


def test_finalize_partial_body_dims_feed_zero_filled_triple(app_with_versions):
    """A finalize body with a PARTIAL stated_dims ([0, 0, 12] — the
    client declares only H) feeds the loop (0.0, 0.0, 12.0), never
    (0.0, 0.0, 0.0) from the dead param-key fallback and never None
    (a declared partial set IS a confirmed set)."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_finalize_capture(
            app_with_versions, client, proj["id"],
            {"message": "finalize", "stated_dims": [0.0, 0.0, 12.0]},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_finalize_message_dims_partial_feed_zero_filled_triple(app_with_versions):
    """A finalize message that confirms one axis ("H: 12" — the
    protocol's axis-prefixed form) with no version yet: the per-axis
    message extraction feeds (0.0, 0.0, 12.0) — the dead W/D/H param-key
    fallback that produced (0,0,0) is gone."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_finalize_capture(
            app_with_versions, client, proj["id"],
            {"message": "H: 12", "request": "H: 12"},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_finalize_falls_back_to_persisted_partial_set(app_with_versions):
    """A finalize message with no dimensions on a project whose latest
    row's persisted ``stated_dims`` is a full triple → the full triple
    (the fallback is the latest row's PERSISTED set, not the params'
    W/D/H keys)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_depth": 8.0, "spacer_height": 5.0},
            stated_dims={"W": 12.0, "D": 8.0, "H": 5.0},
        )
        return await _drive_finalize_capture(
            app_with_versions, client, pid,
            {"message": "finalize it"},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (12.0, 8.0, 5.0)


def test_finalize_no_dimensions_anywhere_abstains(app_with_versions):
    """No dimensions in the message and no confirmed axis anywhere →
    None (abstain), never (0.0, 0.0, 0.0) from the removed param-key
    fallback."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_finalize_capture(
            app_with_versions, client, proj["id"],
            {"message": "finalize it"},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


# ---------------------------------------------------------------------------
# Region-edit route: latest row's persisted confirmed set (real route,
# stub loop)
# ---------------------------------------------------------------------------


async def _drive_region_edit_capture(app, client, pid: int) -> dict:
    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return None  # non-pass → terminal error frame; no version write

    app.state.run_design_loop = _loop
    r = await client.post(
        f"/api/projects/{pid}/region-edits",
        json={
            "marked_png_base64": "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1Nw6"
            "6AAAAFElEQVR4nGNgYGD4z0AFSAjD8gAAAABJRU5ErkJggg==",
            "point": {"x": 1.0, "y": 1.0},
            "instruction": "smooth this",
            "view_id": "front",
            "module_ids": [],
        },
    )
    assert r.status_code == 202, r.text
    source = app.state.event_sources.get(pid)
    assert source is not None
    async for _event, _data in source:
        if _event in ("done", "error"):
            break
    return captured


def test_region_edit_partial_persisted_set_feeds_zero_filled_triple(
    app_with_versions,
):
    """A region edit on a project whose latest row's persisted
    ``stated_dims`` is ``{"H": 12}`` feeds the loop (0.0, 0.0, 12.0) —
    the per-axis confirmed set. Pre-fix the route read W/D/H PARAM KEYS
    (which a version with free-named params lacks) and handed (0.0, 0.0,
    0.0) — the confirmed axis never reached the gate."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_height": 12.0, "spacer_width": 30.0},
            stated_dims={"H": 12.0},
        )
        return await _drive_region_edit_capture(app_with_versions, client, pid)

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_region_edit_no_confirmed_axis_abstains(app_with_versions):
    """A region edit with no confirmed axis anywhere (fresh project)
    feeds None — never a zero triple (the dead param-key read is gone)."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_region_edit_capture(app_with_versions, client, proj["id"])

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


# ---------------------------------------------------------------------------
# Consumer audit: the zero-filled triple must never render as "0 mm"
# ---------------------------------------------------------------------------


def test_design_prompt_zero_filled_triple_never_renders_zero_mm() -> None:
    """The consumer audit (issue #247 fix point 3): the design-role
    system prompt on the route's zero-filled partial triple (0.0, 0.0,
    12.0) renders the unconfirmed axes as ``not specified`` — never
    "W=0" / "D=0" (a zero must not read as a dimension)."""
    system, _ = design_prompt(stated_dims=(0.0, 0.0, 12.0))
    line = next(l for l in system.split("\n") if l.startswith("Ground-truth dimensions"))
    assert "W=not specified" in line, line
    assert "D=not specified" in line, line
    assert "H=12" in line, line
    # Never a bare zero dimension.
    assert "W=0" not in line.replace("W=not specified", ""), line
    assert "D=0," not in line.replace("D=not specified", ""), line


def test_design_messages_reference_line_zero_filled_triple() -> None:
    """The loop's ``Reference dimensions`` line (``_dim_axis_list``) on
    the zero-filled partial triple: unconfirmed axes render
    ``not specified``, confirmed axes their value."""
    from d33d.design_loop import _dim_axis_list

    text = _dim_axis_list((0.0, 0.0, 12.0))
    assert text == "W=not specified, D=not specified, H=12"
    # A zero must never read as a dimension value.
    assert "W=0" not in text and "D=0" not in text and "H=0" not in text
