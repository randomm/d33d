"""Issue #91: the /chat route never supplied stated_dims to the design
loop, so the adapter's ``stated_dims or (0.0, 0.0, 0.0)`` substitution fed
an unsatisfiable ``target <= 0`` bbox target to EVERY browser-driven turn
(the gate hard-failed, the loop exhausted, and the design prompt carried
"W=0, D=0, H=0").

The fix (per the PM's settled decisions, refined by issue #247's per-axis
operator decision):

* Dimension source — ONLY the current turn's input: the user's own
  message, via the EXISTING ``d33d.dimension_protocol`` extraction
  (``stated_dims_from_message`` reuses ``_extract_stated`` — never a new
  parser); else ``None`` — never a zero triple. There is NO fallback to
  the latest version's persisted W/D/H (the ``latest_version_stated_dims``
  helper survives for the 3MF export seam, not the loop-facing chat
  route): a cueless follow-up confirms nothing, so the gate abstains
  rather than enforcing an axis confirmed on an earlier turn.
* Abstain semantics — with no known dimensions the bbox gate ABSTAINS
  (``_bbox_within_tolerance`` returns True on an unknown axis) and the
  abstention is recorded DISTINCTLY in ``Score.bbox_abstained`` — an
  unmeasurable gate neither hard-fails every candidate nor masquerades as
  a measured pass.
* Scope — the abstain is NOT /chat-only: the bbox gate now abstains on an
  unknown (``<= 0``) target for ALL callers, including
  ``create_region_edit``'s deliberate fresh-project ``(0.0, 0.0, 0.0)``
  (whose bbox bit flipped from FAIL to ABSTAIN, recorded as
  ``Score.bbox_abstained`` — regression guards below).

Fast layer: SPA-shaped request bodies through the REAL seam
(``app.state.run_design_loop`` via ``POST /api/projects/{id}/chat``) with
stub loops, plus direct loop drives with stub render/llm; no Docker.
"""

from __future__ import annotations

import pytest

from d33d.design_loop import (
    BboxInfo,
    _design_system,
    no_improvement,
    run_design_loop,
    score,
)
from d33d.design_loop_events import (
    latest_version_stated_dims,
)
from d33d.dimension_protocol import stated_axes_from_message, stated_dims_from_message
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)

# ---------------------------------------------------------------------------
# The decisive seam tests: SPA-shaped bodies through the REAL /chat route
# ---------------------------------------------------------------------------


async def _drive_chat(app, client, pid, body):
    """POST ``body`` to /chat and drive the registered event source to its
    terminal frame (the same pattern the issue #54 chat tests use — the SSE
    endpoint is the sole driver in production; here the test drives it).
    Must be awaited from inside an async ``_call`` closure."""
    captured: dict = {}

    def _loop(app, **kwargs):
        captured.update(kwargs)

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    app.state.run_design_loop = _loop
    r = await client.post(f"/api/projects/{pid}/chat", json=body)
    source = app.state.event_sources.get(pid)
    frames = []
    assert source is not None, "event source not registered before 202 response"
    async for event, data in source:
        frames.append((event, data))
        if event in ("done", "error"):
            break
    return r, captured, frames


def test_chat_spa_shape_message_with_dims_extracts_stated_dims(app_with_versions):
    """DECISIVE (issue #91): a body shaped EXACTLY as the SPA sends it —
    only ``message`` + ``chat_history``, no ``stated_dims`` — with the
    message stating a dimension. The loop receives the usable (non-zero)
    triple extracted from the message itself, never (0, 0, 0)."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "Create a 20mm cube", "chat_history": []},
        )

    r, captured, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # Usable dimensions — the bbox gate is satisfiable; never (0, 0, 0).
    assert captured["stated_dims"] == (20.0, 20.0, 20.0)
    assert captured["stated_dims"] != (0.0, 0.0, 0.0)
    assert frames[-1][0] == "done"
    # NOTE: the drive stub in this test returns a pass result without a
    # best record, so no version row is created — the adapter's version
    # persistence is covered by the dedicated capture tests below (the
    # gate-triple assertions above are the point of this test).


def test_chat_spa_shape_message_without_dims_abstains_not_zero(app_with_versions):
    """DECISIVE (issue #91): a SPA-shaped body whose message states NO
    dimensions ("make it rounder") on a project with no versions — the
    loop receives ``None`` (the abstaining state), never (0, 0, 0)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "make it rounder", "chat_history": []},
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # No dimensions are known anywhere → None (abstain), NOT (0, 0, 0).
    assert captured["stated_dims"] is None
    assert captured["stated_dims"] != (0.0, 0.0, 0.0)


def test_chat_follow_up_cueless_message_abstains_no_persisted_fallback(app_with_versions):
    """Follow-up turn: a project WITH a latest version whose persisted
    ``stated_dims`` column carries a full W/D/H triple and a message that
    states no dimensions — the loop receives ``None`` (the gate
    abstains): the gate enforces only the axes the current turn's input
    confirmed, and a cueless follow-up confirms nothing (issue #247's
    operator decision — no carry-forward of a stale persisted triple
    into the gate), never the stale persisted set and never (0, 0, 0)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # A version whose PERSISTED per-axis confirmed set is a full triple
        # (the stated_dims column — issue #246), not W/D/H param keys.
        await app_with_versions.state.versions.create_version(
            pid,
            {"spacer_width": 12.0, "spacer_height": 5.0, "spacer_depth": 8.0},
            stated_dims={"W": 12.0, "D": 8.0, "H": 5.0},
        )
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "make it a bit rounder", "chat_history": []},
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] is None


def test_chat_latest_version_zero_dims_abstains_not_zero(app_with_versions):
    """A latest version with zero W/D/H must NOT reintroduce the zero
    triple — the gate abstains (None), it does not receive (0, 0, 0)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 0, "D": 0, "H": 0})
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "make it rounder", "chat_history": []},
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] is None


def test_chat_latest_version_null_dims_abstains_not_zero(app_with_versions):
    """A latest version whose params lack W/D/H (null/absent) → the
    fallback yields None (abstain), never a zero triple."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"note": "no dimension params"})
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "make it rounder", "chat_history": []},
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] is None


def test_chat_body_stated_dims_passed_verbatim(app_with_versions):
    """When the request body DOES carry stated_dims (a future client), it
    is passed to the loop verbatim — the extraction/fallback never
    overrides an explicit triple."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {
                "message": "make it",
                "chat_history": [],
                "stated_dims": [30.0, 40.0, 50.0],
            },
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] == (30.0, 40.0, 50.0)


# ---------------------------------------------------------------------------
# The helper: latest_version_stated_dims
# ---------------------------------------------------------------------------


def test_latest_version_stated_dims_helper_partial_version_yields_none() -> None:
    """A version whose persisted ``stated_dims`` column has only SOME of
    W/D/H → None (abstain), not a partially-zero triple that would still
    hard-fail the gate."""

    class _Svc:
        def latest_version(self, project_id):
            return {"params": {"W": 10.0}, "stated_dims": {"W": 10.0}}

    assert latest_version_stated_dims(_Svc(), 1) is None


def test_latest_version_stated_dims_helper_no_version_yields_none() -> None:
    class _Svc:
        def latest_version(self, project_id):
            return None

    assert latest_version_stated_dims(_Svc(), 1) is None


def test_latest_version_stated_dims_helper_complete_version() -> None:
    """A version whose persisted ``stated_dims`` column has all three
    axes → the full triple (W, D, H)."""

    class _Svc:
        def latest_version(self, project_id):
            return {
                "params": {"spacer_width": 10.0},
                "stated_dims": {"W": 10.0, "D": 8.0, "H": 6.0},
            }

    assert latest_version_stated_dims(_Svc(), 1) == (10.0, 8.0, 6.0)


def test_latest_version_stated_dims_helper_null_stated_dims_yields_none() -> None:
    """Issue #247: a version with W/D/H param keys but a NULL
    ``stated_dims`` column (a pre-#246 row) → None (abstain), NOT a
    triple re-derived from the param keys. The dead W/D/H param-key
    read is gone."""

    class _Svc:
        def latest_version(self, project_id):
            return {"params": {"W": 12.0, "D": 8.0, "H": 5.0}, "stated_dims": None}

    assert latest_version_stated_dims(_Svc(), 1) is None


# ---------------------------------------------------------------------------
# The loop: abstain semantics (no Docker — stub render/llm)
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


def _render_fn_with_bbox(bbox: BboxInfo | None):
    async def _render(scad, defines):
        return _ok_render(scad)

    _render._bbox = bbox  # type: ignore[attr-defined]

    def _fn(scad: str, defines: dict):
        return _render(scad, defines)

    return _fn


def test_loop_no_dims_reaches_pass_with_distinct_abstention() -> None:
    """No dimensions known (stated_dims=None) and a render whose bbox is
    arbitrary: the loop REACHES pass (an unmeasurable gate must not fail)
    and the pass is recorded DISTINCTLY as abstained — not a vacuous,
    unmarked pass."""
    scad = "x = 20; cube([x, x, x]);"
    bbox = BboxInfo(x=7.0, y=9.0, z=11.0, volume=1.0)  # arbitrary extents

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    result = run_design_loop(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=None,  # "no dimensions known"
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
    )
    assert result.status == "pass"
    assert result.best.score.bits == (True, True, True, True)
    # The abstention is recorded distinctly: the bbox bit is True ONLY
    # because no dimension was known.
    assert result.best.score.bbox_abstained is True


def test_loop_measured_pass_is_not_abstained() -> None:
    """Control: with KNOWN dimensions the passing bbox bit is a MEASURED
    pass — ``bbox_abstained`` is False, so an abstention is never
    indistinguishable from a verified pass."""
    scad = "x = 20; cube([x, x, x]);"
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    result = run_design_loop(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
    )
    assert result.status == "pass"
    assert result.best.score.bbox_abstained is False


def test_score_abstained_bitvector_ordering_unchanged() -> None:
    """The abstention is a SEPARATE field: the bit ordering, the
    tiebreak tuple, and GATE_REASON_BITS names are unchanged."""
    from d33d.design_loop import GATE_REASON_BITS, is_best

    r = _ok_render("x = 20; cube([x]);")
    bbox = BboxInfo(x=99.0, y=99.0, z=99.0, volume=1.0)
    abstained = score(r, (0.0, 0.0, 0.0), bbox=bbox, scad_source="x = 20; cube([x]);")
    measured_pass = score(
        r, (20.0, 20.0, 20.0), bbox=BboxInfo(20.0, 20.0, 20.0, 1.0),
        scad_source="x = 20; cube([x]);",
    )
    # Same 4-bit vector and rank — the ordering is unchanged.
    assert abstained.bits == measured_pass.bits
    assert abstained.rank == measured_pass.rank == 4
    assert abstained.tiebreak == measured_pass.tiebreak
    # And the flag is what distinguishes them.
    assert abstained.bbox_abstained is True
    assert measured_pass.bbox_abstained is False
    # GATE_REASON_BITS is still the 4-name tuple in bit order.
    assert GATE_REASON_BITS == (
        "error_class_not_ok",
        "views_blank_or_missing",
        "bbox_out_of_tolerance",
        "stated_dims_not_named_parameters",
    )
    # Comparison helpers are unaffected by the new field.
    assert is_best(measured_pass, abstained) is False
    assert is_best(abstained, measured_pass) is False
    assert no_improvement(measured_pass, abstained) is True


def test_score_bbox_abstains_on_partial_zero_triple() -> None:
    """A partially-known triple (one axis zero) also abstains — no axis may
    hard-fail on an unknown target."""
    r = _ok_render("x = 20; cube([x]);")
    s = score(r, (20.0, 0.0, 20.0), bbox=BboxInfo(20.0, 99.0, 20.0, 1.0),
              scad_source="x = 20; cube([x]);")
    assert s.bits[2] is True
    assert s.bbox_abstained is True


def test_score_partial_triple_measured_axes_pass_still_flagged() -> None:
    """Round-2 regression guard: a partial triple whose KNOWN axes happen to
    match the render (bit 2 True for the measured axes) still carries
    ``bbox_abstained=True`` — the unknown axis was never measured, so the
    pass must not be indistinguishable from a fully measured one (the
    vacuous-pass reachability hole of round 1: the flag used to depend on
    bit 2's happenstance rather than on any axis being unknown)."""
    r = _ok_render("x = 20; cube([x]);")
    # Known axes (x, z) match exactly; y unknown (target 0) and the render
    # reports an arbitrary y extent.
    s = score(r, (20.0, 0.0, 20.0), bbox=BboxInfo(20.0, 7.0, 20.0, 1.0),
              scad_source="x = 20; cube([x]);")
    assert s.bits == (True, True, True, True)
    assert s.bbox_abstained is True
    # All-known control: the same render against a complete triple is a
    # MEASURED pass, never flagged.
    m = score(r, (20.0, 20.0, 20.0), bbox=BboxInfo(20.0, 7.0, 20.0, 1.0),
              scad_source="x = 20; cube([x]);")
    assert m.bits[2] is False  # y out of tolerance


# ---------------------------------------------------------------------------
# The shorthand regression guards (ticket #91 round 2 — CRITICAL finding):
# a single stated number must NEVER fabricate a 3-axis triple unless the
# text also names an equal-axis shape (cube/box/sphere/ball) with an
# explicit "mm" — otherwise the gate runs against a made-up envelope
# (the exact fabricate-don't-measure anti-pattern this ticket removes).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "message",
    [
        "make a 20mm hole in the lid",
        "a 20mm tall vase",
        "add a 5mm fillet",
        "mount a 6mm bolt",
        "a 3 m beam",  # a bare "m" (meters) is never mm
        "make me a 42 millimeter thing",  # spelled-out unit
        "make it 15mm taller",
    ],
)
def test_single_number_without_equal_axis_shape_abstains(message: str) -> None:
    """A single number that is a feature size, a one-axis measurement, or a
    non-mm unit yields ``None`` (abstain) — never a fabricated
    (n, n, n) triple that would feed the bbox gate a wrong reference."""
    assert stated_dims_from_message(message) is None


@pytest.mark.parametrize(
    "message,expected",
    [
        ("a 20mm cube", (20.0, 20.0, 20.0)),
        ("a 10mm box", (10.0, 10.0, 10.0)),
        ("a 15mm sphere", (15.0, 15.0, 15.0)),
        ("an 8mm ball", (8.0, 8.0, 8.0)),
        ("a 20 mm cube", (20.0, 20.0, 20.0)),
    ],
)
def test_equal_axis_shape_shorthand_still_extracts(message, expected) -> None:
    """The one defensible single-number source: an equal-axis shape with an
    explicit mm — "Create a 20mm cube" stays satisfiable on a bare first
    turn (no latest version yet)."""
    assert stated_dims_from_message(message) == expected


def test_shorthand_does_not_fire_from_prior_turn_history() -> None:
    """The shorthand now only fires on a turn that itself has no
    axis-prefixed extraction; a prior turn's "a 20mm cube" is still
    history-scanned (the axis pass's pre-existing design), so it still
    yields the triple — but a prior turn's "a 5mm fillet" must NOT leak a
    fabricated (5, 5, 5) into a later turn's gate target."""
    # Prior turn named an equal-axis shape: still extracted (pre-existing
    # history-scanning semantics, unchanged).
    assert stated_dims_from_message(
        "make a 5mm fillet", ["a 20mm cube"]
    ) == (20.0, 20.0, 20.0)
    # Prior turn stated a feature size only: nothing leaks.
    assert stated_dims_from_message(
        "make it rounder", ["make a 5mm fillet"]
    ) is None


def test_axis_prefixed_turn_shorthand_does_not_complete_triple() -> None:
    """Round-2 minor: a turn stating "W: 30" does NOT let the shorthand
    from the same turn fill D/H (the shorthand only runs on a turn with
    no axis-prefixed extraction) — the gate abstains rather than mixing
    an axis-prefixed value with a shorthand value within one turn."""
    assert (
        stated_dims_from_message("W: 30, make it a 20mm cube") is None
    )


# ---------------------------------------------------------------------------
# Issue #246: the per-axis variant (stated_axes_from_message)
# ---------------------------------------------------------------------------


def test_stated_axes_partial_counts_per_axis() -> None:
    """A message stating only one axis yields a PARTIAL dict for that
    axis (issue #246: partial statements count for the axes they state) —
    while the full-triple ``stated_dims_from_message`` returns None for
    the same message (its contract is unchanged for existing callers)."""
    axes = stated_axes_from_message("make it 40 mm wide, W: 40")
    assert axes == {"W": 40.0}
    # The full-triple contract is intact: partial → None.
    assert stated_dims_from_message("make it 40 mm wide, W: 40") is None


def test_stated_axes_no_statement_yields_empty_dict() -> None:
    """No statement at all → an EMPTY dict (never a fabricated axis, never
    None) — the design-state seam persists this as NULL (abstain)."""
    assert stated_axes_from_message("a spacer to lift a shelf 12 mm") == {}
    assert stated_axes_from_message("make it rounder") == {}


def test_stated_axes_equal_shape_shorthand_fills_all_three() -> None:
    """An equal-axis shape ("a 20mm cube") names a part with three equal
    20 mm edges — all three axes are stated (honest: the text names all
    three). The bare single-number case ("a 5mm fillet") fills NOTHING —
    it is a feature size, never an envelope."""
    assert stated_axes_from_message("a 20mm cube") == {"W": 20.0, "D": 20.0, "H": 20.0}
    assert stated_axes_from_message("a 5mm fillet") == {}


def test_stated_axes_matches_full_triple_when_complete() -> None:
    """A complete statement yields the same three values both functions
    report (the per-axis dict is the full-triple's per-axis view)."""
    msg = "W: 30, D: 20, H: 10"
    axes = stated_axes_from_message(msg)
    triple = stated_dims_from_message(msg)
    assert axes == {"W": 30.0, "D": 20.0, "H": 10.0}
    assert triple == (30.0, 20.0, 10.0)


def test_stated_axes_explicit_partial_ranks_highest() -> None:
    """A caller-supplied partial ``explicit`` map (the body's
    ``stated_dims`` per-axis view) is the protocol's highest-priority
    source — the per-axis variant surfaces it as-is."""
    axes = stated_axes_from_message("make it", explicit={"W": 60.0, "H": 80.0})
    assert axes == {"W": 60.0, "H": 80.0}


def test_explicit_body_partial_bypass_yields_flagged_abstained_pass() -> None:
    """Round-2 Q1: a client CAN bypass the message protocol with an
    explicit partial triple (``stated_dims: [30, 0, 50]``). The loop
    reaches pass (the unknown axis abstains) and the pass is flagged —
    the only vacuous-pass reachability found in the review, and it is
    now reliably flagged (the flag no longer depends on bit 2 having
    happened to be True)."""
    scad = "x = 20; cube([x, x, x]);"
    bbox = BboxInfo(x=30.0, y=99.0, z=50.0, volume=1.0)

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    result = run_design_loop(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=(30.0, 0.0, 50.0),
        render_fn=_render,
        llm_fn=_stub_llm(scad),
        bbox_fn=lambda r: bbox,
    )
    assert result.status == "pass"
    assert result.best.score.bbox_abstained is True


# ---------------------------------------------------------------------------
# The prompt: no zero ground-truth line (asserted on the function the /chat
# loop actually calls — _design_system, not design_prompts.design_prompt)
# ---------------------------------------------------------------------------


def test_design_system_never_contains_zero_ground_truth() -> None:
    """A no-dimensions turn (normalized to the zero triple by the loop)
    must NOT feed the design system prompt "W=0, D=0, H=0" — it renders
    each unknown axis as "not specified"."""
    system = _design_system((0.0, 0.0, 0.0))
    assert "W=0, D=0, H=0" not in system
    assert "not specified" in system
    # A partial triple: known axes keep their values, unknown abstain.
    partial = _design_system((20.0, 0.0, 0.0))
    assert "W=0, D=0, H=0" not in partial
    assert "W=20" in partial
    assert "D=not specified" in partial
    # Known dimensions are rendered exactly as before.
    known = _design_system((20.0, 25.0, 30.0))
    assert "W=20, D=25, H=30" in known


def test_loop_pass_prompt_captured_never_carries_zero_dims() -> None:
    """End-to-end at the loop seam: a no-dimensions turn drives
    ``run_design_loop`` with ``stated_dims=None`` and the design system
    prompt the LLM edge actually receives never carries the zero
    ground-truth line."""
    scad = "x = 20; cube([x, x, x]);"
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)
    systems: list[str] = []

    class _LLMResult:
        def __init__(self):
            self.tool_calls = []
            self.content = f"```scad\n{scad}\n```"
            self.prompt_hash = "h"
            self.status = "ok"

    async def _llm(role, messages, system):
        systems.append(system or "")
        return _LLMResult()

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    result = run_design_loop(
        photo="data:image/png;base64,x",
        chat_history=(),
        stated_dims=None,
        render_fn=_render,
        llm_fn=_llm,
        bbox_fn=lambda r: bbox,
    )
    assert result.status == "pass"
    assert systems, "the LLM edge was never called"
    for system in systems:
        assert "W=0, D=0, H=0" not in system


# ---------------------------------------------------------------------------
# Regression guards: the region-edits route and the abstain change
# ---------------------------------------------------------------------------

_REGION_EDIT_PNG_BASE64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAA"
    "YAAAAAEABCAwE="
)


def _region_edit_body() -> dict:
    return {
        "module_ids": ["curl_3", "curl_4"],
        "view_id": "front",
        "marked_png_base64": _REGION_EDIT_PNG_BASE64,
        "point": {"x": 300.0, "y": 200.0},
        "instruction": "open up this spiral, it's too tight to print",
    }


def test_region_edit_fresh_project_passes_none_not_zero_triple(app_with_versions):
    """A region edit on a fresh project (no confirmed axis anywhere) hands
    the loop ``None`` — the gate abstains ENTIRELY (ticket #91's
    ``Score.bbox_abstained``). Issue #247 removed the deliberate
    ``(0.0, 0.0, 0.0)`` zero-triple hand-off (and the dead W/D/H param-key
    read behind it): ``None`` is the abstaining state, and a zero triple
    is never a gate target."""
    from tests.versioning.test_design_loop_finalize import _StubResult

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
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] is None


def test_region_edit_latest_version_abstains_no_persisted_fallback(
    app_with_versions,
):
    """A region edit with an existing version hands the loop ``None`` —
    a region edit carries NO dimension statement, so the gate abstains,
    as before issue #247: there is no persisted fallback for the gate
    (the 3MF export route still reads the column via
    ``latest_version_stated_dims``; the loop-facing region-edit route
    does not)."""
    from tests.versioning.test_design_loop_finalize import _StubResult

    captured: dict = {}

    async def _loop(app, **kwargs):
        captured.update(kwargs)
        return _StubResult("exhausted", {})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # Free-named params + the persisted per-axis confirmed set (the
        # production shape — the model never emits W/D/H keys).
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
    assert captured["stated_dims"] is None


def test_region_edit_fresh_project_bbox_gate_abstains_not_fails(
    app_with_versions,
) -> None:
    """PINNED NEW BEHAVIOUR (ticket #91 / issue #247): a region edit on a
    FRESH project (no confirmed axis anywhere, so the route hands the
    loop ``None``) drives the loop so that the bbox bit is True via
    ABSTENTION — ``Score.bbox_abstained`` is True — not via a measurement.

    Before ticket #91 this same candidate scored the bbox bit False
    (``target <= 0`` hard-failed) and the loop could never pass on a fresh
    project. This is a deliberate, named assertion of the new behaviour —
    not an incidental side effect of another test: the loop here is the
    REAL ``run_design_loop`` (injected into the route's seam), so the
    asserted score is what production scoring computes."""
    app = app_with_versions
    bbox = BboxInfo(x=21.0, y=19.0, z=25.0, volume=1.0)  # arbitrary extents

    async def _render(scad_source, defines):
        return _ok_render(scad_source)

    results: list = []
    captured: dict = {}

    async def _loop(app, **kwargs):
        from d33d.design_loop import run_design_loop_async

        captured.update(kwargs)
        # The seam passes hook-only extras (request/model/prompt_version) and
        # render_fn=None by contract (the production closure supplies its
        # own) — the adapter's adapter-passed values must never reach the
        # loop core. Drive it with an explicit keyword list instead.
        kwargs.pop("request", None)
        kwargs.pop("model", None)
        kwargs.pop("prompt_version", None)
        kwargs.pop("render_fn", None)
        kwargs.pop("llm_fn", None)
        kwargs.pop("bbox_fn", None)
        # Drive the REAL async core directly — the adapter runs the seam's
        # coroutine via asyncio.run on a worker thread, so the sync
        # run_design_loop wrapper (itself an asyncio.run) cannot nest.
        result = await run_design_loop_async(
            photo=kwargs["photo"],
            chat_history=kwargs["chat_history"],
            stated_dims=kwargs["stated_dims"],
            render_fn=_render,
            llm_fn=_stub_llm("x = 20; cube([x, x, x]);"),
            bbox_fn=lambda r: bbox,
        )
        results.append(result)
        return result

    async def _call(client):
        proj = await create_project(client)  # fresh project — no versions
        pid = proj["id"]
        app.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body()
        )
        source = app.state.event_sources.get(pid)
        assert source is not None, "event source not registered before 202"
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    r, frames = run_async(app, _call)
    assert r.status_code == 202, r.text
    assert frames[-1][0] == "done", f"expected done frame, got {frames[-1]}"
    # The pass creates a version via the loop result's best iteration
    # (the stubbed render has no durable artifacts — the frame omits them).
    # The route hands over None — no confirmed axis anywhere abstains
    # entirely (issue #247 removed the deliberate (0,0,0) hand-off).
    assert captured["stated_dims"] is None
    # The REAL loop's own score: the bbox bit is True AND the pass is
    # recorded distinctly as an abstention (before #91 this same candidate
    # scored the bbox bit False — target <= 0 hard-failed — and the loop
    # could never pass on a fresh project).
    assert len(results) == 1
    assert results[0].status == "pass"
    assert results[0].best.score.bits == (True, True, True, True)
    assert results[0].best.score.bbox_abstained is True, (
        "the bbox bit is True ONLY because no axis is confirmed "
        "(the target is unknown) — the abstention must be recorded distinctly"
    )


# ---------------------------------------------------------------------------
# The wire: ``Score.bbox_abstained`` must reach the consumer (ticket #91
# round 2 — a flag that stops at the Score object is not a safeguard).
# ---------------------------------------------------------------------------


def test_done_frame_carries_bbox_abstained_true(app_with_versions):
    """An abstained pass (no dimensions known anywhere) surfaces
    ``bbox_abstained: true`` on the terminal ``done`` frame — the client
    can never mistake it for a verified pass."""

    class _R:
        status = "pass"
        failure_reason = None

        class _Best:
            params = None
            scad_source = "x = 20; cube([x]);"
            render = None

            class _Score:
                bbox_abstained = True

            score = _Score()

        best = _Best()

    async def _loop(app, **kwargs):
        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make it rounder", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    done = [d for e, d in frames if e == "done"]
    assert done, "no done frame"
    assert done[0]["bbox_abstained"] is True


def test_done_frame_carries_bbox_abstained_false(app_with_versions):
    """A measured pass (dimensions known, bbox verified) carries
    ``bbox_abstained: false`` — the two pass kinds are distinguishable on
    the wire."""

    class _R:
        status = "pass"
        failure_reason = None

        class _Best:
            params = None
            scad_source = "x = 20; cube([x]);"
            render = None

            class _Score:
                bbox_abstained = False

            score = _Score()

        best = _Best()

    async def _loop(app, **kwargs):
        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "Create a 20mm cube", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    done = [d for e, d in frames if e == "done"]
    assert done, "no done frame"
    assert done[0]["bbox_abstained"] is False
