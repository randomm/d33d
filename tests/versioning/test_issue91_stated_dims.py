"""Issue #91: the /chat route never supplied stated_dims to the design
loop, so the adapter's ``stated_dims or (0.0, 0.0, 0.0)`` substitution fed
an unsatisfiable ``target <= 0`` bbox target to EVERY browser-driven turn
(the gate hard-failed, the loop exhausted, and the design prompt carried
"W=0, D=0, H=0").

The fix (per the PM's settled decisions):

* Dimension source, strict precedence —
  (a) the user's own message, via the EXISTING
  ``d33d.dimension_protocol`` extraction (``stated_dims_from_message``
  reuses ``_extract_stated`` — never a new parser);
  (b) else the latest version's W/D/H (``latest_version_stated_dims``,
  the same fallback the finalize seam uses);
  (c) else ``None`` — never a zero triple.
* Abstain semantics — with no known dimensions the bbox gate ABSTAINS
  (``_bbox_within_tolerance`` returns True on an unknown axis) and the
  abstention is recorded DISTINCTLY in ``Score.bbox_abstained`` — an
  unmeasurable gate neither hard-fails every candidate nor masquerades as
  a measured pass.
* Scope — the abstain path serves the /chat caller path only;
  ``create_region_edit``'s deliberate fresh-project ``(0.0, 0.0, 0.0)``
  behaviour is unchanged (regression guards below).

Fast layer: SPA-shaped request bodies through the REAL seam
(``app.state.run_design_loop`` via ``POST /api/projects/{id}/chat``) with
stub loops, plus direct loop drives with stub render/llm; no Docker.
"""

from __future__ import annotations

from d33d.design_loop import (
    BboxInfo,
    _design_system,
    no_improvement,
    run_design_loop,
    score,
)
from d33d.design_loop_events import latest_version_stated_dims
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


def test_chat_follow_up_uses_latest_version_fallback(app_with_versions):
    """Follow-up turn: a project WITH a latest version and a message that
    states no dimensions — the latest version's W/D/H supplies the triple
    (the fallback the finalize seam uses), not (0, 0, 0) and not None."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 12.0, "D": 8.0, "H": 5.0})
        return await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "make it a bit rounder", "chat_history": []},
        )

    r, captured, _frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert captured["stated_dims"] == (12.0, 8.0, 5.0)


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
    """A version with only SOME of W/D/H stated → None (abstain), not a
    partially-zero triple that would still hard-fail the gate."""

    class _Svc:
        def latest_version(self, project_id):
            return {"params": {"W": 10.0}}

    assert latest_version_stated_dims(_Svc(), 1) is None


def test_latest_version_stated_dims_helper_no_version_yields_none() -> None:
    class _Svc:
        def latest_version(self, project_id):
            return None

    assert latest_version_stated_dims(_Svc(), 1) is None


def test_latest_version_stated_dims_helper_complete_version() -> None:
    class _Svc:
        def latest_version(self, project_id):
            return {"params": {"W": 10.0, "D": 8.0, "H": 6.0}}

    assert latest_version_stated_dims(_Svc(), 1) == (10.0, 8.0, 6.0)


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
# Regression guards: the region-edits route is behaviourally unchanged
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
        "polygon": [
            {"x": 10.0, "y": 10.0},
            {"x": 50.0, "y": 10.0},
            {"x": 30.0, "y": 40.0},
        ],
        "instruction": "open up this spiral, it's too tight to print",
    }


def test_region_edit_fresh_project_still_passes_zero_triple(app_with_versions):
    """REGRESSION GUARD: ``create_region_edit``'s deliberate fresh-project
    ``(0.0, 0.0, 0.0)`` behaviour is unchanged — a fresh project still
    passes the zero triple verbatim (the dimension gate measures rather
    than fabricates)."""
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
    assert captured["stated_dims"] == (0.0, 0.0, 0.0)


def test_region_edit_latest_version_still_uses_version_dims(app_with_versions):
    """REGRESSION GUARD: a region edit with an existing version still
    passes that version's W/D/H (the region-edits fallback is untouched)."""
    from tests.versioning.test_design_loop_finalize import _StubResult

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
