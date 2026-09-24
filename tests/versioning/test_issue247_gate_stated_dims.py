"""Issue #247 (regression fix): the loop-facing routes (chat, finalize)
feed the bbox gate ONLY the axes confirmed in the CURRENT run's own input
— a (W, D, H) triple with 0.0 for unconfirmed axes (the #91
zero-means-unknown convention) — and the region-edit route abstains
entirely (a region edit carries no dimension statement). There is
deliberately NO fallback to a persisted version row: a follow-up message
with no explicit dimension cue ("make it taller") confirms nothing, so
the gate abstains — it must not enforce a stale H from an earlier turn
against a candidate the user just asked to make taller (the regression
that exhausted the loop). Pre-fix the helper fell back to the latest
row's persisted ``stated_dims`` when the current message confirmed
nothing; that fallback is gone.

Tests drive the REAL routes (stub only the loop callable, capture the
kwargs it receives):

- chat "make it 20mm tall, H: 20" on a fresh project → (0.0, 0.0, 20.0)
  (the dimension protocol's axis-prefixed forms — ``H: 12`` / "a 20mm
cube" — are the statement shapes it confirms; a bare single number like
  "12 mm tall" is deliberately NOT a stated envelope, the #91
  fabricate-don't-measure rule, and the per-axis path inherits that)
- chat follow-up "make it taller" after a version whose ``stated_dims``
  is ``{"H": 12}`` → None (the gate ABSTAINS — the regression test)
- chat follow-up "H: 20" on the same project → (0.0, 0.0, 20.0)
- chat with no dimensions in the current message → None (abstain)
- chat "W: 60, D: 45, H: 80" → (60.0, 45.0, 80.0)
- the REAL ``score()`` with the captured partial triple: a bbox of z=19.3
  (the v24 7 mm error) FAILS the bbox bit — the per-axis gate now catches
  it (end-to-end-ish; the loop callable stays stubbed)
- finalize: per-axis source (body stated_dims axes / message extraction),
  no persisted fallback, partial → zero-filled triple, none → None
- region-edit: always None (a region edit carries no dimension
  statement → the gate abstains, as before issue #247)
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
# Helper unit tests: axes_to_gate_triple
# ---------------------------------------------------------------------------


def test_axes_to_gate_triple_zero_fills_unconfirmed_axes() -> None:
    from d33d.design_loop_events import axes_to_gate_triple

    # A partial set is zero-filled on the unconfirmed axes (the #91
    # zero-means-unknown convention) — the helper reads NO version row,
    # so a confirmed axis from an earlier turn can never leak in.
    assert axes_to_gate_triple({"H": 12.0}) == (0.0, 0.0, 12.0)
    assert axes_to_gate_triple({"W": 60.0, "D": 45.0, "H": 80.0}) == (
        60.0, 45.0, 80.0,
    )


def test_axes_to_gate_triple_none_when_nothing_confirmed() -> None:
    from d33d.design_loop_events import axes_to_gate_triple

    assert axes_to_gate_triple({}) is None
    assert axes_to_gate_triple(None) is None
    assert axes_to_gate_triple({"H": 0.0}) is None
    assert axes_to_gate_triple({"H": -5.0}) is None


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


def test_chat_cueless_follow_up_abstains_no_persisted_fallback(
    app_with_versions,
):
    """REGRESSION (issue #247, adversarial vector 5): a follow-up message
    with NO explicit dimension cue on a project whose latest version row
    persists ``stated_dims`` ``{"H": 12}`` — the loop receives ``None``
    (the gate abstains), NOT the stale (0.0, 0.0, 12.0). "Make it
taller" confirms no axis; enforcing H=12 against a candidate the user
    asked to make taller is exactly the failure that exhausted the loop.
    The pre-fix helper fell back to the persisted row here."""

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
            {"message": "make it taller", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


def test_chat_follow_up_reconfirms_axis(app_with_versions):
    """A follow-up message that DOES confirm an axis ("H: 20") on a
    project whose latest row persists ``{"H": 12}``: the current turn's
    axis wins — (0.0, 0.0, 20.0), no carry-forward or merge of the
    stale value."""

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
            {"message": "H: 20", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 20.0)


def test_chat_no_dimensions_in_current_message_abstains(app_with_versions):
    """A current message that states no dimensions → None (abstain
    entirely), never a zero triple — regardless of anything a version
    row persisted."""

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


def test_chat_current_axes_no_merge_with_persisted(app_with_versions):
    """A current message that confirms an axis DIFFERENT from the latest
    row's confirmed set: the gate gets EXACTLY the current turn's
    confirmed axis — no transitive merge with the persisted row (the
    gate enforces only what the current run's input confirmed)."""

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
    protocol's axis-prefixed form) feeds the loop (0.0, 0.0, 12.0) —
    the dead W/D/H param-key fallback that produced (0,0,0) is gone."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_finalize_capture(
            app_with_versions, client, proj["id"],
            {"message": "H: 12", "request": "H: 12"},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_finalize_cueless_message_abstains_no_persisted_fallback(
    app_with_versions,
):
    """REGRESSION (issue #247): a finalize message with no explicit
    dimension cue on a project whose latest row's persisted
    ``stated_dims`` is a full triple → the loop receives ``None``
    (abstain), NOT the persisted triple — no carry-forward of
    confirmed dimensions across turns."""

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
    assert captured["stated_dims"] is None


def test_finalize_no_dimensions_in_current_message_abstains(app_with_versions):
    """No dimensions in the current message → None (abstain), never
    (0.0, 0.0, 0.0) from the removed param-key fallback."""

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


def test_region_edit_always_abstains_even_with_persisted_axes(
    app_with_versions,
):
    """A region edit on a project whose latest row's persisted
    ``stated_dims`` is ``{"H": 12}`` feeds the loop ``None`` — a region
    edit carries NO dimension statement, so the gate abstains, as
    before issue #247 (there is no persisted fallback to the gate)."""

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
    assert captured["stated_dims"] is None


def test_region_edit_no_confirmed_axis_abstains(app_with_versions):
    """A region edit on a fresh project feeds None — never a zero
    triple (a region edit confirms no axis at all)."""

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
