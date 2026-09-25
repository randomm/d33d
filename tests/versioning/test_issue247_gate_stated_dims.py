"""Issue #247 (regression fix) + issue #261 (carry-forward): the
loop-facing routes (chat, finalize) feed the bbox gate the EFFECTIVE
per-axis set (the #261 carry-forward merge helper — issue #247's
``no persisted fallback`` rule dies to it): the set starts as the
LATEST version's persisted ``stated_dims`` and is adjusted by THIS
turn's cues — a relative/global cue RELEASES its axis (the #247
regression: "make it taller" must not enforce the stale H), an absolute
cue OVERRIDES, and uncued axes CARRY forward ("add a hole" keeps the
gate enforcing H=12). The effective set is the SINGLE value both the
gate (``axes_to_gate_triple`` — a (W, D, H) triple with 0.0 for
unconfirmed axes, the #91 zero-means-unknown convention) and the new
version row's persisted ``stated_dims`` consume — the region-edit route
abstains (gate ``None``) but persists the carried set unchanged.

Tests drive the REAL routes (stub only the loop callable, capture the
kwargs it receives):

- chat "make it 20mm tall, H: 20" on a fresh project → (0.0, 0.0, 20.0)
  (the dimension protocol's axis-prefixed forms — ``H: 12`` / "a 20mm
cube" — are the statement shapes it confirms; a bare single number like
  "12 mm tall" is deliberately NOT a stated envelope, the #91
  fabricate-don't-measure rule, and the per-axis path inherits that)
- chat follow-up "make it taller" after a version whose ``stated_dims``
  is ``{"H": 12}`` → None (the gate ABSTAINS — the regression test;
  #261's carry-forward releases H via the relative cue, so no stale H)
- chat follow-up "add a hole" on the same project → (0.0, 0.0, 12.0)
  (#261's carry-forward — H carries forward, the gate enforces it)
- chat follow-up "H: 20" on the same project → (0.0, 0.0, 20.0)
  (the cue OVERRIDES the carried value — precedence)
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
from d33d.design_loop_events import _carried_axes
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


def test_chat_relative_cue_releases_carry_forward_axis(app_with_versions):
    """REGRESSION (issue #247; #261's carry-forward keeps it green):
    a follow-up with a RELATIVE cue on a carried axis ("make it taller"
    on a project whose latest row persists ``{"H": 12}``) — the loop
    receives ``None`` (the gate abstains), NOT the stale (0.0, 0.0,
    12.0): the lexicon classifies "taller" as a relative H cue, the
    carry-forward merge RELEASES H (a relative cue removes the axis), so
    nothing is enforced. Enforcing the old H against a candidate the
    user just asked to make taller is exactly the failure that
    exhausted the loop — #247's regression."""

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


def test_carried_axes_frame_field_bbox_only() -> None:
    """The terminal error frame's ``carried_axes`` field (issue #261 fix
    batch): the gate's enforced per-axis set is carried ONLY on a
    bbox-gate failure (the SPA uses it to name the held value in the
    failure copy) — omitted for every other failure reason and when the
    gate set is empty (omit-not-null). The values are positive floats
    (non-positive / non-numeric entries are dropped, as the merge
    helper does)."""

    class _R:
        def __init__(self, failure_reason: str) -> None:
            self.failure_reason = failure_reason

    assert _carried_axes(_R("bbox_out_of_tolerance"), {"D": 12.0, "W": 20.0}) == {
        "D": 12.0,
        "W": 20.0,
    }
    # Non-bbox failure: no field (the SPA must not name a held value for
    # an empty-model or syntax failure).
    assert _carried_axes(_R("empty_model"), {"D": 12.0}) is None
    assert _carried_axes(_R(None), {"D": 12.0}) is None
    # No gate set / empty set: no field (nothing was enforced).
    assert _carried_axes(_R("bbox_out_of_tolerance"), None) is None
    assert _carried_axes(_R("bbox_out_of_tolerance"), {}) is None
    # Non-positive entries are dropped; an all-dropped (or empty) set →
    # omit (a zero axis was abstained, not enforced). A numeric string
    # coerces (the merge helper's rule) — kept, never a crash.
    assert _carried_axes(_R("bbox_out_of_tolerance"), {"H": 0.0}) is None
    assert _carried_axes(_R("bbox_out_of_tolerance"), {"H": -3.0}) is None
    assert _carried_axes(_R("bbox_out_of_tolerance"), {"H": True}) is None
    assert _carried_axes(_R("bbox_out_of_tolerance"), {"H": "12"}) == {"H": 12.0}
    assert _carried_axes(_R("bbox_out_of_tolerance"), {"H": 12.0, "W": 0.0}) == {
        "H": 12.0
    }


def test_chat_cueless_follow_up_carries_forward(app_with_versions):
    """CARRY-FORWARD (issue #261, one axis): a follow-up message with NO
    axis cue at all ("add a hole") on a project whose latest version row
    persists ``stated_dims`` ``{"H": 12}`` — the loop receives (0.0, 0.0,
    12.0): the effective set starts as the latest row's stated set and
    no cue changes it, so the gate enforces the carried H=12 (and the
    new version row persists {H: 12}). #247's "no persisted fallback"
    rule (the loop receives ``None`` here) is superseded by #261's
    carry-forward: a carried axis is the user's own prior statement, not
    a stale value — only a relative/global cue releases it."""

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
            {"message": "add a hole", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 0.0, 12.0)


def test_chat_feature_clause_does_not_override_carried_axis(app_with_versions):
    """CARRY-FORWARD (issue #261 fix batch, the feature-noun rule):
    v1 has stated D=40 (persisted on the latest row). A follow-up that
    contains a FEATURE NOUN — "add a 10 mm deep hole" — must NOT state
    D=10 (the 10 is the hole's depth, a feature size, not the part's):
    the clause states nothing, nothing releases or overrides the
    carried axis, so the gate enforces the CARRIED D=40 (0.0, 40.0,
    0.0). Pre-fix the lexicon stated D=10 from the feature clause and
    the carry-forward merged it over the carried 40 — the gate then
    enforced the hole's size against the whole part."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"base_depth": 40.0, "base_width": 20.0},
            stated_dims={"D": 40.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "add a 10 mm deep hole", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (0.0, 40.0, 0.0)


def test_chat_global_cue_releases_all_axes(app_with_versions):
    """CARRY-FORWARD (issue #261, global cue): "make it bigger" on a
    project whose latest row persists a FULL {W: 12, D: 8, H: 5} set —
    the loop receives ``None`` (the global cue releases ALL THREE axes,
    the gate abstains entirely) — never the persisted triple."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_depth": 8.0, "spacer_height": 5.0},
            stated_dims={"W": 12.0, "D": 8.0, "H": 5.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "make it bigger", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


def test_chat_follow_up_reconfirms_axis(app_with_versions):
    """A follow-up message that OVERRIDES a carried axis ("H: 20") on a
    project whose latest row persists ``{"H": 12}``: the current turn's
    explicit cue wins — (0.0, 0.0, 20.0), the carried 12 is replaced
    (precedence: explicit cues > the carried set — the assertion is
    unchanged from #247; only the mechanism moved from "no merge" to
    "override")."""

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
    """A current message that states no dimensions on a FRESH project →
    None (abstain entirely), never a zero triple — there is no version
    row to carry from, and the statement confirms nothing."""

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


def test_chat_two_axes_carry_forward_except_released(app_with_versions):
    """CARRY-FORWARD (issue #261, two axes): a message that confirms
    NOTHING ("add a hole") on a project whose latest row persists
    {W: 12.0, H: 12.0}: the gate gets BOTH carried axes — (12.0, 0.0,
    12.0) — no axis released, no axis overridden. And the same project
    with a RELATIVE cue on one axis ("make it taller") keeps the other
    (W=12 enforced — (12.0, 0.0, 0.0) — H released, never the stale H).
    (The gate-triple capture is the loop kwarg; the new version row
    persists the same effective set — the single-value contract.)"""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_height": 12.0},
            stated_dims={"W": 12.0, "H": 12.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "add a hole", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (12.0, 0.0, 12.0)


def test_chat_relative_releases_one_of_two_carried_axes(app_with_versions):
    """CARRY-FORWARD (issue #261, two axes, one released): "make it
taller" on a project whose latest row persists {W: 12.0, H: 12.0} —
    the gate enforces W=12 ONLY (12.0, 0.0, 0.0): the relative H cue
    releases H, the un-cued W carries forward, and the new version
    row's persisted set is {W: 12.0} (no stale H)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_height": 12.0},
            stated_dims={"W": 12.0, "H": 12.0},
        )
        return await _drive_chat_capture(
            app_with_versions, client, pid,
            {"message": "make it taller", "chat_history": []},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] == (12.0, 0.0, 0.0)


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
# Finalize route: per-axis source, no persisted fallback (real route,
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


def test_finalize_cueless_message_carries_forward(app_with_versions):
    """CARRY-FORWARD (issue #261): a finalize message with no axis cue
    on a project whose latest row's persisted ``stated_dims`` is a full
    triple → the loop receives the PERSISTED triple (12.0, 8.0, 5.0):
    the SAME merge helper the chat route uses starts the effective set
    from the latest row and carries it forward (uncued axes carry) —
    #247's "no carry-forward" abstain here is superseded by #261."""

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


def test_finalize_no_dimensions_in_current_message_abstains(app_with_versions):
    """No dimensions in the current message on a FRESH project → None
    (abstain), never (0.0, 0.0, 0.0) from the removed param-key
    fallback — no version row to carry from either."""

    async def _call(client):
        proj = await create_project(client)
        return await _drive_finalize_capture(
            app_with_versions, client, proj["id"],
            {"message": "finalize it"},
        )

    captured = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None


# ---------------------------------------------------------------------------
# Region-edit route: always abstains (None) (real route, stub loop)
# ---------------------------------------------------------------------------


async def _drive_region_edit_capture(app, client, pid: int) -> dict:
    captured: dict[str, Any] = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)

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


def test_region_edit_persists_carried_stated_dims_unchanged(app_with_versions):
    """A region edit persists the latest version's ``stated_dims``
    UNCHANGED on the new version row (issue #261's region-edit rule —
    the SAME merge helper the chat and finalize routes use, computed
    with NO cues: nothing released, nothing added, the carried set
    comes back as-is). A region edit on a project whose latest row
    persists ``{"H": 12, "W": 30}`` therefore feeds the loop
    ``stated_axes == {"H": 12, "W": 30}`` — which ``_resolve_version_create``
    persists verbatim as the new row's ``stated_dims`` — while the gate
    input (``stated_dims``) stays ``None`` (abstain, #247). A region
    edit on a fresh project (no carried row) feeds an empty set — the
    new row persists NULL, as before."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_height": 12.0, "spacer_width": 30.0},
            stated_dims={"H": 12.0, "W": 30.0},
        )
        captured = await _drive_region_edit_capture(app_with_versions, client, pid)

        # The new version row the loop pass creates (the adapter's
        # ``_resolve_version_create`` persists the kwargs ``stated_axes``
        # verbatim as the row's ``stated_dims``).
        new_version = app_with_versions.state.versions.latest_version(pid)
        return captured, new_version

    captured, new_version = run_async(app_with_versions, _call)
    # The gate input abstains — the region edit confirms nothing.
    assert captured["stated_dims"] is None
    # The carried set is persisted unchanged (not NULL, not released).
    assert new_version["stated_dims"] == {"H": 12.0, "W": 30.0}


def test_region_edit_fresh_project_persists_nothing(app_with_versions):
    """A region edit on a project with NO carried ``stated_dims`` feeds
    the merge helper an empty carry: the adapter's ``stated_axes`` is
    ``{}`` — which ``_resolve_version_create`` maps to a NULL row
    (``stated_dims=stated_axes`` falsy → NULL; today's no-row behaviour
    is unchanged, never a fabricated axis). Verified via the merge
    helper's exact input shape: ``_latest_stated_dict`` on the fresh
    project is ``None`` and ``effective_stated_dims(None)`` is ``{}``,
    so the adapter receives an empty set and the gate input (the loop
    kwargs' ``stated_dims``) stays ``None``."""

    async def _call(client):
        proj = await create_project(client)
        captured = await _drive_region_edit_capture(
            app_with_versions, client, proj["id"]
        )
        from d33d.dimension_protocol import effective_stated_dims
        from d33d.projects import _latest_stated_dict

        carry = _latest_stated_dict(app_with_versions.state.versions, proj["id"])
        merged = effective_stated_dims(carry)
        return captured, merged

    captured, merged = run_async(app_with_versions, _call)
    assert captured["stated_dims"] is None
    assert merged == {}


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


# ---------------------------------------------------------------------------
# Render defines: only confirmed axes become defines (issue #247)
# ---------------------------------------------------------------------------


def test_dim_params_partial_triple_only_confirmed_axes_become_defines() -> None:
    """``_dim_params`` injects only the CONFIRMED (``> 0``) axes as W/D/H
    defines — an unconfirmed axis contributes no define at all (never
    ``-DW=0``, a fabricated zero in the render's named-parameter channel).
    (0.0, 0.0, 12.0) → H only."""
    from d33d.design_loop import _dim_params

    defines = _dim_params((0.0, 0.0, 12.0), {})
    assert defines == {"H": "12.0"}
    # A full triple still injects all three.
    assert _dim_params((20.0, 25.0, 30.0), {}) == {
        "W": "20.0",
        "D": "25.0",
        "H": "30.0",
    }
    # Explicit caller defines keep priority (setdefault semantics).
    assert _dim_params((20.0, 25.0, 30.0), {"W": "99"})["W"] == "99"


# ---------------------------------------------------------------------------
# Input validation: negative / non-finite stated_dims are rejected (422)
# ---------------------------------------------------------------------------


def test_chat_negative_stated_dims_422(app_with_versions):
    """A chat body with a NEGATIVE ``stated_dims`` element → 422 (malformed
    input; 0 stays legal = unconfirmed axis)."""

    async def _call(client):
        proj = await create_project(client)
        return await client.post(
            f"/api/projects/{proj['id']}/chat",
            json={
                "message": "make it",
                "chat_history": [],
                "stated_dims": [-5.0, 0.0, 12.0],
            },
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 422, r.text


def test_finalize_negative_stated_dims_422(app_with_versions):
    """A finalize body with a NEGATIVE ``stated_dims`` element → 422
    (the parser mirrors the chat validator: finite, >= 0; 0 = unconfirmed
    stays legal)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await client.post(
            f"/api/projects/{pid}/finalize",
            json={"message": "finalize", "stated_dims": [-5.0, 0.0, 12.0]},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 422, r.text


def test_finalize_nonfinite_stated_dims_422(app_with_versions):
    """A finalize body with a non-finite ``stated_dims`` element → 422
    (the parser's ``math.isfinite`` check — ``1e999`` parses to inf in
    the JSON float parse)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        raw = b'{"message": "finalize", "stated_dims": [1e999, 0, 12]}'
        return await client.post(
            f"/api/projects/{pid}/finalize",
            content=raw,
            headers={"content-type": "application/json"},
        )

    r = run_async(app_with_versions, _call)
    assert r.status_code == 422, r.text
