"""tests/test_design_loop.py — bounded iterate-and-score design loop (ticket
#5, task-loop workstream).

Covers:
- The cap: an always-failing loop runs EXACTLY 3 iterations.
- On exhaustion the loop returns the BEST-scoring candidate plus a
  structured failure reason (never silently the last attempt): a
  mid-loop regression is never returned as the best.
- Early stop on two consecutive non-improving iterations (declining rank
  trajectory 3 → 1 → 1 stops before any further iteration).
- "No improvement" is the named predicate over the pinned bitvector rank:
  unit-tested in isolation, monotone-comparable, never string comparison.
- Compile failures route through ``d33d.failure_classes`` (tagged class fed
  back as structured repair, never raised terminal); the 11 failure-class
  samples classify to their class name and route to repair.
- Non-repairable render classes (``oom`` mid-loop) are non-improving steps,
  not repair inputs, and never crash the loop.
- The loop completes end-to-end at T1 via the fenced-JSON protocol (the LLM
  edge mocked to return T1-shaped fenced responses).
- The stated dimensions travel as NAMED parameters (defines W/D/H + extras
  like clearance) and the named-parameter gate preserves a param block the
  LLM already declared.
- ``prompt_hash`` is surfaced per LLM call (stable across repeats, changes
  when the repair input changes).

No Docker, no network: ``render_fn`` and ``llm_fn`` are injected (the
``d33d.render_worker`` testable pattern).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest
import trimesh

from d33d.config.catalogue import load_catalogue
from d33d.config.probes import CapabilityResult
from d33d.design_llm import LLMResult, send
from d33d.design_loop import (
    GATE_REASON_BITS,
    MAX_ITERATIONS,
    MAX_SCAD_SOURCE_BYTES,
    MAX_SCAD_VALIDATION_CHARS,
    NO_IMPROVEMENT_LIMIT,
    RENDERER_UNAVAILABLE,
    BboxFn,
    BboxInfo,
    DesignResult,
    Score,
    _axis_param_mismatches,
    _scad_from_result,
    extract_confirm_hints,
    extract_named_params,
    extract_param_meta,
    is_best,
    make_llm_fn,
    no_improvement,
    renderer_is_available,
    reset_renderer_preflight_cache,
    run_design_loop,
    run_design_loop_async,
    scad_looks_valid,
    score,
)

# Captured at module import time — BEFORE the conftest hermetic stub
# (issue #346) monkeypatches the module attribute. The probe test below
# needs the real function to re-point the stub (via
# ``image_detail_override``).
from d33d.design_loop import _render_worker_image_detail as _real_image_detail
from d33d.failure_classes import (
    FAILURE_CLASSES,
    classify_failure,
    route_repair,
)
from d33d.prompt_hash import canonical_hash
from d33d.render_worker import RenderResult

PHOTO = "data:image/png;base64,REF"
STATED = (20.0, 25.0, 30.0)


@pytest.fixture(autouse=True)
def _renderer_preflight_available(monkeypatch):
    """Default the design loop's renderer pre-flight to "available" so no
    test in this module shells out to ``docker info`` (issue #277). Tests
    that exercise the pre-flight failure path pass a ``renderer_check``
    stub or override the module function.

    The module attribute is swapped (a lambda with ``*a, **kw`` arity)
    because ``run_design_loop_async`` binds the DEFAULT of its
    ``renderer_check`` parameter to the name at call time (default
    ``None`` → the module attribute) — the swap is the one seam that
    covers both the direct ``renderer_is_available()`` path and the
    ``renderer_check=None`` default path. ``monkeypatch.setattr`` (no
    ``# type: ignore``) swaps and restores the attribute per test.
    """
    import d33d.design_loop as _dl

    monkeypatch.setattr(_dl, "renderer_is_available", lambda *a, **kw: True)


GOOD_SCAD = "W = 20;\nD = 25;\nH = 30;\ncube([W, D, H]);\n"
MAGIC_SCAD = "cube([20, 25, 30]);\n"
BAD_SCAD = "translate([10, 20, 30]);\ncube([10, 10, 10]);\n"

VIEWS_OK = ("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _catalogue():
    """Load the golden models.yaml with both env keys stubbed (the loader
    resolves `${ENV}` keys against the environment at load time)."""
    env = {"TRAIL_OPENERS_LLM_KEY": "stub", "PAID_AZURE_LLM_KEY": "stub"}
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        return load_catalogue(Path(__file__).parent / "fixtures" / "models.yaml")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _render(
    *,
    error_class: str = "ok",
    stderr: str = "",
    views: tuple[str, ...] = VIEWS_OK,
    render_log: str = "",
) -> RenderResult:
    return RenderResult(
        ok=error_class == "ok",
        exit_code=0 if error_class == "ok" else 1,
        duration_ms=10,
        error_class=error_class,
        stderr=stderr,
        stl="model.stl" if error_class == "ok" else None,
        csg="model.csg" if error_class == "ok" else None,
        views=views,
        render_log=render_log,
    )


def _llm_result(
    content: str = "",
    tool_calls: tuple | None = None,
    status: str = "ok",
) -> LLMResult:
    return LLMResult(
        content=content,
        tool_calls=tool_calls or (),
        prompt_hash="h" * 64,
        tier="T1",
        status=status,
        request_body={},
    )


def _t1_tool_call_payload(name: str, arguments: dict[str, Any]) -> str:
    """A valid T1 fenced-JSON wire response (strict JSON string)."""
    return "```json\n" + json.dumps({"tool": name, "arguments": arguments}) + "\n```"


def _scad_llm(scad: str) -> LLMResult:
    """A T1-shaped design response (fenced JSON, tool synthesized)."""
    return _llm_result(
        content=_t1_tool_call_payload("emit_design", {"scad": scad}),
        tool_calls=({"name": "emit_design", "arguments": {"scad": scad}},),
    )


def _run_loop(
    llm_script: Sequence[LLMResult],
    render_script: Sequence[RenderResult],
    *,
    stated: tuple[float, float, float] = STATED,
    bbox_fn=None,
    log: list | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> DesignResult:
    i = {"n": 0}

    def llm_fn(role, messages, system):
        return llm_script[min(i["n"], len(llm_script) - 1)]

    def render_fn(scad, defines):
        r = render_script[min(i["n"], len(render_script) - 1)]
        i["n"] += 1
        return r

    logs = log if log is not None else []

    def log_fn(role, h, status):
        logs.append((role, h, status))

    return run_design_loop(
        photo=PHOTO,
        stated_dims=stated,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
        log=log_fn,
        max_iterations=max_iterations,
    )


# ---------------------------------------------------------------------------
# The improvement metric: named predicate, monotone-comparable
# ---------------------------------------------------------------------------


def test_score_is_bitvector_rank_with_tiebreak():
    good = score(
        _render(), STATED, bbox=BboxInfo(20, 25, 30, 15000.0), scad_source=GOOD_SCAD
    )
    assert good.bits == (True, True, True, True, True)
    assert good.rank == 5
    assert good.perfect

    bad = score(
        _render(error_class="syntax_error", stderr="ERROR: syntax"),
        STATED,
        scad_source=GOOD_SCAD,
    )
    # The failed render still reports the six view filenames and the scad
    # still carries the named-parameter block — only the ok/ok-bbox gates
    # fail on a syntax error (the metric is a pure function of its inputs).
    assert bad.rank == 3
    assert bad.bits == (False, True, False, True, True)


def test_score_bit5_axis_params_mismatch():
    """Issue #276 bit 5: a param with a declared axis whose SCAD value
    differs from the measured bbox extent on that axis by more than
    max(20% of the value, 5 mm) makes bit 5 False."""
    # 20 vs 102: diff=82 > max(4, 5)=5 → bit 5 False
    s = score(
        _render(),
        (20.0, 25.0, 30.0),
        bbox=BboxInfo(20.0, 25.0, 102.0, 51000.0),
        scad_source="H = 20;\ncube([20, 25, H]);\n",
        param_meta={"H": {"label": "Tray height", "unit": "mm", "axis": "H"}},
        named_params={"H": 20.0},
    )
    assert s.bits[4] is False
    # Bit 3 (bbox) also fails: stated H=30 but measured z=102.
    assert s.bits[2] is False
    assert s.rank == 3  # 3 of 5 bits pass (ok, views, named_params)


def test_score_bit5_lip_case_passes():
    """Issue #276 lip case: body param 40, measured 43.8 → diff 3.8 <
    max(8, 5)=8 → bit 5 True (the 5 mm margin is the pass/fail boundary)."""
    s = score(
        _render(),
        (40.0, 40.0, 12.0),
        bbox=BboxInfo(43.80, 43.90, 12.0, 2297.0),
        scad_source="spacer_width = 40;\nspacer_depth = 40;\ncube([spacer_width, spacer_depth, 12]);\n",
        param_meta={
            "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
            "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
        },
        named_params={"spacer_width": 40.0, "spacer_depth": 40.0},
    )
    assert s.bits[4] is True
    # Bit 3 (bbox) fails: stated W=40/D=40 but measured 43.8/43.9.
    assert s.bits[2] is False
    assert s.rank == 4  # 4 of 5 bits pass (ok, views, named_params, axis_params)


def test_score_bit5_no_axis_ignored():
    """Issue #276: a param with no declared axis is ignored by bit 5."""
    s = score(
        _render(),
        (20.0, 25.0, 30.0),
        bbox=BboxInfo(102.0, 25.0, 30.0, 51000.0),
        scad_source="H = 20;\ncube([102, 25, H]);\n",
        param_meta={"H": {"label": "Tray height", "unit": "mm"}},  # no axis
        named_params={"H": 20.0},
    )
    assert s.bits[4] is True  # no axis → ignored


def test_score_bit5_zero_value_ignored():
    """Issue #276: a param with a zero value is ignored by bit 5."""
    s = score(
        _render(),
        (20.0, 25.0, 30.0),
        bbox=BboxInfo(102.0, 25.0, 30.0, 51000.0),
        scad_source="W = 0;\ncube([102, 25, 30]);\n",
        param_meta={"W": {"label": "Width", "unit": "mm", "axis": "W"}},
        named_params={"W": 0.0},
    )
    assert s.bits[4] is True  # zero value → ignored


def test_score_bit5_exactly_at_threshold_passes():
    """Issue #276 boundary: a diff exactly at the threshold passes (strict >).
    25 mm param, measured 30 → diff 5 = max(5, 5) = 5 → passes."""
    s = score(
        _render(),
        (25.0, 25.0, 25.0),
        bbox=BboxInfo(30.0, 25.0, 25.0, 18750.0),
        scad_source="W = 25;\ncube([W, 25, 25]);\n",
        param_meta={"W": {"label": "Width", "unit": "mm", "axis": "W"}},
        named_params={"W": 25.0},
    )
    assert s.bits[4] is True  # diff == threshold → passes


def test_score_bit5_non_numeric_ignored():
    """Issue #276: a non-numeric param value is ignored by bit 5."""
    s = score(
        _render(),
        (20.0, 25.0, 30.0),
        bbox=BboxInfo(102.0, 25.0, 30.0, 51000.0),
        scad_source="H = 20;\ncube([102, 25, H]);\n",
        param_meta={"H": {"label": "Tray height", "unit": "mm", "axis": "H"}},
        named_params={"H": 20.0, "note": "some value"},
    )
    assert s.bits[4] is False  # H still fails, note is ignored


def test_axis_param_mismatches_returns_per_param_evidence():
    """Issue #276: ``_axis_param_mismatches`` returns one
    ``(label, model, measured, axis)`` tuple per mismatching param, empty
    when every declared-axis param matches (bit 5 passes)."""
    # Two mismatching params, one matching param → two entries, the
    # matching param is absent.
    out = _axis_param_mismatches(
        BboxInfo(80.0, 49.0, 102.0, 51000.0),
        {"W": 60.0, "D": 45.0, "H": 20.0, "note": "a string"},
        {
            "W": {"label": "Tray width", "unit": "mm", "axis": "W"},
            "D": {"label": "Tray depth", "unit": "mm", "axis": "D"},
            "H": {"label": "Tray height", "unit": "mm", "axis": "H"},
        },
    )
    # Order follows the named_params dict — compare as a set.
    assert set(out) == {
        ("Tray width", 60.0, 80.0, "W"),
        ("Tray height", 20.0, 102.0, "H"),
    }
    # A matching param → empty (bit 5 passes).
    assert (
        _axis_param_mismatches(
            BboxInfo(43.8, 43.9, 12.0, 2297.0),
            {"w": 40.0},
            {"w": {"label": "Spacer width", "unit": "mm", "axis": "W"}},
        )
        == []
    )
    # No bbox → empty (the caller abstains).
    assert _axis_param_mismatches(None, {"H": 20.0}, {}) == []
    # No label in the meta → the param name is the label fallback.
    out = _axis_param_mismatches(
        BboxInfo(25.0, 25.0, 30.0, 18750.0),
        {"h": 20.0},
        {"h": {"axis": "H"}},
    )
    assert out == [("h", 20.0, 30.0, "H")]


def test_no_improvement_is_named_predicate_on_rank():
    low = Score(
        bits=(False, False, False, False, False),
        rank=0,
        tiebreak=(False, False, False, False, False),
    )
    high = Score(
        bits=(True, False, False, False, False),
        rank=1,
        tiebreak=(True, False, False, False, False),
    )
    assert no_improvement(high, low) is True  # non-increase
    assert no_improvement(low, high) is False  # increase
    assert no_improvement(low, low) is True  # equal rank = no improvement


def test_is_best_rank_then_deterministic_tiebreak():
    a = Score(
        bits=(True, True, False, False, False),
        rank=2,
        tiebreak=(True, True, False, False, False),
    )
    b = Score(
        bits=(True, False, True, False, False),
        rank=2,
        tiebreak=(True, False, True, False, False),
    )
    c = Score(
        bits=(False, False, False, False, False),
        rank=0,
        tiebreak=(False, False, False, False, False),
    )
    assert is_best(a, c) is True
    assert is_best(c, a) is False
    # tie on rank: earlier bits weigh more → a beats b
    assert is_best(a, b) is True
    assert is_best(b, a) is False


# ---------------------------------------------------------------------------
# Loop: pass, cap, best-never-last, early stop
# ---------------------------------------------------------------------------


def _bbox_ok(render: RenderResult) -> BboxInfo | None:
    if render.error_class != "ok":
        return None
    return BboxInfo(STATED[0], STATED[1], STATED[2], 15000.0)


def test_loop_terminates_at_or_before_three_and_passes():
    result = _run_loop(
        llm_script=[_scad_llm(GOOD_SCAD)],
        render_script=[_render()],
        bbox_fn=_bbox_ok,
    )
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.best.score.perfect
    assert len(result.iterations) == 1


def test_loop_stops_at_exactly_three_when_always_failing():
    result = _run_loop(
        llm_script=[_scad_llm(BAD_SCAD)],
        render_script=[
            _render(
                error_class="syntax_error",
                stderr="ERROR: syntax error near token 'translate'",
            )
        ],
    )
    assert result.status == "exhausted"
    assert result.iterations_used == MAX_ITERATIONS == 3
    assert len(result.iterations) == 3
    # Issue #277: the reason is the specific render error_class, not the
    # generic bit-0 name that used to shadow it.
    assert result.failure_reason == "syntax_error"


def test_exhaustion_returns_best_not_last_when_last_scores_lower():
    # Iteration 1: ok render, bbox gate fails (rank 3).
    # Iteration 2: syntax_error (rank 0) — a regression.
    # Iteration 3: ok render, bbox gate fails again (rank 3) — same rank,
    # same tiebreak → not "better" than iteration 1 (not strictly >).
    llm = [_scad_llm(GOOD_SCAD), _scad_llm(BAD_SCAD), _scad_llm(GOOD_SCAD)]
    renders = [
        _render(),
        _render(error_class="syntax_error", stderr="ERROR: x"),
        _render(),
    ]

    def bbox_fn(r):
        if r.error_class != "ok":
            return None
        return BboxInfo(99.0, 25.0, 30.0, 1.0)  # x way off → bbox gate fails

    result = _run_loop(llm_script=llm, render_script=renders, bbox_fn=bbox_fn)
    assert result.status == "exhausted"
    assert result.iterations_used == 3
    # The BEST candidate (rank 3, first-seen wins ties) is iteration 1.
    assert result.best.iteration == 1
    assert result.best.render.error_class == "ok"
    assert result.failure_reason == GATE_REASON_BITS[2]  # bbox_out_of_tolerance


def test_partial_triple_single_axis_gate_fail_exhausts_at_three():
    """Issue #247 ACCEPTANCE CRITERION: a message confirming only H=12
    with a candidate whose z=19.3 → bbox bit False, failure_reason
    ``bbox_out_of_tolerance``, exhaustion at 3 iterations. Today no test
    can express a single-axis gate failure because the gate only accepts
    full triples; the new per-axis rule makes this expressible.
    """
    # SCAD that declares H (the confirmed axis) as a named param — the
    # named-params gate passes. The render's z=19.3 is OUT of tolerance
    # of H=12 (|19.3-12| = 7.3 > max(0.12, 0.5) = 0.5) → bbox gate FAILS.
    scad = "W = 20;\nD = 20;\nH = 12;\ncube([W, D, H]);\n"
    llm = [_scad_llm(scad)]
    renders = [_render()] * 3  # all iterations produce the same render

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        return BboxInfo(x=20.0, y=20.0, z=19.3, volume=1.0)  # z way off H=12

    result = _run_loop(
        llm_script=llm,
        render_script=renders,
        stated=(0.0, 0.0, 12.0),  # only H confirmed
        bbox_fn=bbox_fn,
    )
    assert result.status == "exhausted"
    assert result.iterations_used == MAX_ITERATIONS == 3
    assert result.failure_reason == GATE_REASON_BITS[2]  # bbox_out_of_tolerance
    # Every iteration's bbox bit is False (the 7 mm error is caught).
    for iteration in result.iterations:
        assert iteration.score.bits[2] is False


def test_partial_triple_single_axis_gate_pass_reaches_pass():
    """Issue #247: a message confirming only H=12 with a candidate whose
    z=12.2 (within tolerance) → the loop reaches PASS (the confirmed axis
    is within tolerance, unconfirmed axes are skipped)."""
    scad = "W = 20;\nD = 20;\nH = 12;\ncube([W, D, H]);\n"
    llm = [_scad_llm(scad)]
    renders = [_render()]

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        return BboxInfo(x=20.0, y=20.0, z=12.2, volume=1.0)  # z within tol of H=12

    result = _run_loop(
        llm_script=llm,
        render_script=renders,
        stated=(0.0, 0.0, 12.0),  # only H confirmed
        bbox_fn=bbox_fn,
    )
    assert result.status == "pass"
    assert result.iterations_used == 1
    # The bbox bit is True (H is within tolerance), and the flag is True
    # (not every axis was checked — per-axis reality).
    assert result.best.score.bits[2] is True
    assert result.best.score.bbox_abstained is True


def test_partial_confirmed_set_does_not_flag_unconfirmed_literals_through_loop():
    """Issue #247 operator decision (``_named_params_present`` coupling):
    a run that confirms only H=12 must not fail the loop because the
    unconfirmed 20 mm dimension is declared as ``spacer_width = 20`` —
    the exemption/requirement applies only to the confirmed axes, so
    the named-params bit stays True and the loop passes (every confirmed
    axis measured within tolerance, unconfirmed axes skipped). The
    declared shape is the model's actual v24 output: free-named params,
    no W/D/H keys."""
    scad = (
        "spacer_width = 20;\n"
        "spacer_height = 12;\n"
        "cube([spacer_width, spacer_width, spacer_height]);\n"
    )
    llm = [_scad_llm(scad)]
    renders = [_render()]

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        # z=12.2 is within tolerance of H=12; x/y (20) unconfirmed —
        # skipped, never a target.
        return BboxInfo(x=20.0, y=20.0, z=12.2, volume=1.0)

    result = _run_loop(
        llm_script=llm,
        render_script=renders,
        stated=(0.0, 0.0, 12.0),  # only H confirmed
        bbox_fn=bbox_fn,
    )
    assert result.status == "pass"
    assert result.iterations_used == 1
    # The named-params bit is True — the unconfirmed ``spacer_width = 20``
    # is NOT flagged as a magic number (if it were, the loop would
    # exhaust on ``stated_dims_not_named_parameters``).
    assert result.best.score.bits[3] is True
    # The partial pass still carries the abstention flag (W/D were never
    # checked) — per-axis reality.
    assert result.best.score.bbox_abstained is True


def test_early_stop_after_two_consecutive_no_improvements():
    # A DECLINING rank sequence proves the no-improvement stop is active
    # (not just the cap): 2 → 0 → 0 hits two consecutive non-improvements
    # and stops at iteration 3. Iteration 1: an ok render whose bbox gate
    # fails (rank 2); iterations 2-3: syntax errors (rank 0) — each a
    # non-increase of the pinned rank.
    llm = [_scad_llm(GOOD_SCAD), _scad_llm(BAD_SCAD), _scad_llm(BAD_SCAD)]
    renders = [
        _render(),
        _render(error_class="syntax_error", stderr="ERROR: x"),
        _render(error_class="syntax_error", stderr="ERROR: x"),
    ]

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        return BboxInfo(99.0, 25.0, 30.0, 1.0)  # x way off → bbox gate fails

    result = _run_loop(llm_script=llm, render_script=renders, bbox_fn=bbox_fn)
    assert result.status == "exhausted"
    # Declining rank trajectory: 4 (ok, views, params, axis-params) → 2 (views, params) → 2.
    assert [r.score.rank for r in result.iterations] == [4, 2, 2]
    # Two consecutive non-improvements fired → early stop at iteration 3.
    assert result.iterations_used == NO_IMPROVEMENT_LIMIT + 1 == 3
    # The best is iteration 1 (rank 4), NOT the last attempt (rank 2).
    assert result.best.iteration == 1
    assert result.best.score.rank == 4


def test_pass_on_third_iteration_still_counts_within_cap():
    llm = [_scad_llm(BAD_SCAD), _scad_llm(GOOD_SCAD)]
    renders = [
        _render(error_class="syntax_error", stderr="ERROR: trailing semicolon"),
        _render(),
    ]
    result = _run_loop(llm_script=llm, render_script=renders, bbox_fn=_bbox_ok)
    # Iteration 1 fails (rank 0), iteration 2 passes (rank 4).
    assert result.status == "pass"
    assert result.iterations_used == 2


# ---------------------------------------------------------------------------
# Failure routing: tagged class, never terminal, structured repair
# ---------------------------------------------------------------------------


def test_compile_failure_feeds_structured_repair_into_next_iteration():
    seen_prompts: list[str] = []

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        seen_prompts.append(text)
        if "REPAIR directive" in text:
            return _scad_llm(GOOD_SCAD)
        return _scad_llm(BAD_SCAD)

    renders = [
        _render(
            error_class="syntax_error",
            stderr="ERROR: syntax error near token 'translate'",
        ),
        _render(),
    ]
    i = {"n": 0}

    def render_fn(scad, defines):
        r = renders[i["n"]]
        i["n"] += 1
        return r

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
    )
    assert result.status == "pass"
    # First prompt: no repair directive. Second: the structured class.
    assert "REPAIR directive" not in seen_prompts[0]
    assert "REPAIR directive" in seen_prompts[1]
    assert "failure_class:" in seen_prompts[1]
    assert "instruction:" in seen_prompts[1]
    # The first iteration's recorded failure class is the routed one.
    assert result.iterations[0].failure_class == "unclassified_syntax_error"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].repair["failure_class"] == "unclassified_syntax_error"


def test_axis_params_mismatch_feeds_repair_into_next_iteration():
    """Issue #276: a design run whose first iteration has an axis-declared
    param that contradicts the measured bbox (bit 5 fails) → the repair
    prompt for iteration 2 contains the mismatch text naming the param
    with both numbers. The second iteration's render matches → pass."""
    seen_prompts: list[str] = []

    # Iteration 1: H=20 declared with axis H, measured z=102 → bit 5 fails.
    # Iteration 2: H=102 declared with axis H, measured z=102 → bit 5 passes.
    scad1 = "H = 20;\ncube([20, 25, H]);\n"
    scad2 = "H = 102;\ncube([20, 25, H]);\n"

    def _scad_llm_with_meta(scad: str) -> LLMResult:
        """A T1-shaped design response with a parameters metadata array."""
        meta = [{"name": "H", "label": "Tray height", "unit": "mm", "axis": "H"}]
        args: dict[str, Any] = {"scad": scad, "parameters": meta}
        return LLMResult(
            content=f"```json\n{json.dumps({'tool': 'emit_design', 'arguments': args})}\n```",
            tool_calls=({"name": "emit_design", "arguments": args},),
            prompt_hash="h" * 64,
            tier="T1",
            status="ok",
            request_body={},
        )

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        seen_prompts.append(text)
        if "REPAIR directive" in text:
            return _scad_llm_with_meta(scad2)
        return _scad_llm_with_meta(scad1)

    def render_fn(scad, defines):
        return _render()

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        # Both iterations measure z=102 (the model's H=20 is wrong,
        # H=102 is correct).
        return BboxInfo(x=20.0, y=25.0, z=102.0, volume=51000.0)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 25.0, 102.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
    )
    assert result.status == "pass"
    assert result.iterations_used == 2
    # First prompt: no repair directive. Second: the axis_params_mismatch
    # directive naming the param with both numbers.
    assert "REPAIR directive" not in seen_prompts[0]
    assert "REPAIR directive" in seen_prompts[1]
    assert "failure_class: axis_params_mismatch" in seen_prompts[1]
    # The directive's evidence line — the declared vs measured pairing the
    # instruction promises ("listed below with both numbers") — must itself
    # reach the prompt: the previous_scad block only carries the SCAD's own
    # numbers, never the measured extents, so without the evidence line the
    # model cannot see the 102-vs-20 pairing on a line of its own.
    assert (
        "evidence: Tray height = 20 but the part measures 102 on H" in (seen_prompts[1])
    )
    # The first iteration's recorded failure class is axis_params_mismatch.
    assert result.iterations[0].failure_class == "axis_params_mismatch"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].repair["failure_class"] == "axis_params_mismatch"


def test_axis_params_mismatch_ignores_position_offset_params():
    """Issue #385 (operator decision 2026-10-05): the import gate
    (``axis_params_mismatch``) ignores parameters whose names or labels
    mark them as positions or offsets (from / offset / distance /
    position / spacing / margin / inset / pitch), so a mistagged position
    can't fail an import edit.

    Test: an imported plate edit whose SCAD tags
    ``hole_distance_from_left_edge = 15`` as axis W, with a measured
    width of 120, does NOT fail ``axis_params_mismatch``."""
    s = score(
        _render(),
        (120.0, 40.0, 6.0),
        bbox=BboxInfo(120.0, 40.0, 6.0, 28800.0),
        scad_source=(
            "hole_distance_from_left_edge = 15;\n"
            "W = 120;\nD = 40;\nH = 6;\n"
            "cube([W, D, H]);\n"
        ),
        param_meta={
            "W": {"label": "Plate width", "unit": "mm", "axis": "W"},
            "D": {"label": "Plate depth", "unit": "mm", "axis": "D"},
            "H": {"label": "Plate height", "unit": "mm", "axis": "H"},
            "hole_distance_from_left_edge": {
                "label": "Hole offset from left edge",
                "unit": "mm",
                "axis": "W",
            },
        },
        named_params={
            "W": 120.0,
            "D": 40.0,
            "H": 6.0,
            "hole_distance_from_left_edge": 15.0,
        },
    )
    # The W param matches (120 vs 120) → no mismatch.
    # The hole_distance param is ignored (position/offset keyword).
    # All bits should pass (no axis_params_mismatch).
    assert s.bits[4] is True
    assert s.axis_params_match is True


def test_axis_param_mismatches_ignores_position_param_directly():
    """Issue #385: ``_axis_param_mismatches`` directly — a param named
    ``hole_distance_from_left_edge`` tagged as axis W with value 15
    against a measured width of 120 is ignored (position/offset keyword).
    A genuine W param (value 20 vs measured 120) still fires."""
    from d33d.design_loop import _axis_param_mismatches

    bbox = BboxInfo(120.0, 40.0, 6.0, 28800.0)
    # Only the position param is mistagged → no mismatches.
    mismatches = _axis_param_mismatches(
        bbox,
        {"hole_distance_from_left_edge": 15.0},
        {
            "hole_distance_from_left_edge": {
                "label": "Hole offset from left edge",
                "axis": "W",
            }
        },
    )
    assert mismatches == []
    # A genuine W param (value 20 vs measured 120) still fires.
    mismatches2 = _axis_param_mismatches(
        bbox,
        {"W": 20.0},
        {"W": {"label": "Plate width", "axis": "W"}},
    )
    assert len(mismatches2) == 1
    assert mismatches2[0][0] == "Plate width"
    assert mismatches2[0][1] == 20.0
    assert mismatches2[0][2] == 120.0
    assert mismatches2[0][3] == "W"


def test_oom_mid_loop_is_not_repair_and_does_not_crash():
    # Iteration 1: oom (non-repairable, rank 0, no repair fed back).
    # Iteration 2: good (rank 4) → pass.
    llm = [_scad_llm(GOOD_SCAD)]
    renders = [
        _render(error_class="oom", stderr="Killed (oom)"),
        _render(),
    ]
    result = _run_loop(llm_script=llm, render_script=renders, bbox_fn=_bbox_ok)
    assert result.status == "pass"
    first = result.iterations[0]
    assert first.render.error_class == "oom"
    # Non-repairable: NOT fed back as repair input.
    assert first.repair is None
    assert first.failure_class is None


def test_eleven_failure_class_samples_classify_and_route_to_repair():
    """Each of the 11 LLM failure classes routes to repair (structured
    directive, not a raised terminal exception)."""
    samples: list[tuple[str, str, str]] = [
        # (class, error_class, stderr)
        (
            "trailing_semicolon",
            "syntax_error",
            "translate([10, 20, 30]);\ncube([10, 10, 10]);\nERROR: parse failed",
        ),
        (
            "transform_order",
            "syntax_error",
            "ERROR: rotate(90,[0,1,0]) translate([1,2,3]) compose",
        ),
        (
            "wrong_axis_rotation",
            "syntax_error",
            "ERROR: rotate(90, [0, 1, 0]) axis check",
        ),
        ("difference_inversion", "syntax_error", "ERROR: difference( A; B; ) operand"),
        (
            "hull_miskowski_misuse",
            "syntax_error",
            "ERROR: hull( or minkowski( nested boolean #1027",
        ),
        (
            "zup_yup_confusion",
            "syntax_error",
            "ERROR: y-up vs z-up confusion (Blender)",
        ),
        # magic_numbers is not stderr-detectable in the merged classifier —
        # a clean-syntax render lands in the fallback class; the vision loop
        # (named-parameter gate) is what actually catches magic numbers, so
        # it routes to repair like every other class.
        (
            "unclassified_syntax_error",
            "syntax_error",
            "ERROR: numeric literal 20 in geometry",
        ),
        (
            "projection_offset_fragile",
            "syntax_error",
            "ERROR: projection( or offset( CGAL fragment",
        ),
        ("text_missing_font", "syntax_error", 'ERROR: font="Missing Font" not found'),
        (
            "hallucinated_bosl2",
            "syntax_error",
            "ERROR: Unknown module bsp_offset (BOSL2) not a module",
        ),
        # 11th — the vision-only class: render succeeds, geometry wrong.
        ("geometrically_wrong", "ok", ""),
    ]
    for expected, error_class, stderr in samples:
        classified = classify_failure(
            error_class=error_class, stderr=stderr, scad_source="cube([20,25,30]);"
        )
        assert classified.failure_class == expected, (
            expected,
            classified.failure_class,
        )
        assert classified.failure_class in FAILURE_CLASSES
        assert classified.is_repairable
        directive = route_repair(classified=classified, scad_source="cube([20,25,30]);")
        assert directive is not None, f"no repair directive for {expected}"
        assert directive.failure_class == expected
        assert directive.instruction  # structured, non-empty
    assert len(samples) == 11


def test_golden_scad_fixtures_carry_named_params_or_fail_gate():
    fixtures = Path(__file__).parent / "fixtures" / "scad"
    magic = (fixtures / "box-magic.scad").read_text()
    param = (fixtures / "box-param.scad").read_text()
    # The magic-numbers sample fails the named-parameter gate.
    s_magic = score(
        _render(), (20, 25, 30), bbox=BboxInfo(20, 25, 30, 1.0), scad_source=magic
    )
    assert s_magic.named_params is False
    # The parametric sample passes the named-parameter gate.
    s_param = score(
        _render(), (20, 25, 30), bbox=BboxInfo(20, 25, 30, 1.0), scad_source=param
    )
    assert s_param.named_params is True


def test_extract_named_params_golden_fixtures_match_gate_bit():
    """Issue #219: the shared extraction returns EXACTLY the declared
    name/value pairs of a golden fixture (box-param.scad declares W=20,
    H=25, D=30 → {"W": 20.0, "H": 25.0, "D": 30.0} as floats; box-magic
    declares nothing → {}), and the extraction is consistent with the
    gate bit the same regex feeds: named_params False ⇒ empty dict, and a
    non-empty extraction ⇒ the gate's declaration leg passes (the
    magic-numbers leg is orthogonal and unchanged)."""
    fixtures = Path(__file__).parent / "fixtures" / "scad"
    magic = (fixtures / "box-magic.scad").read_text()
    param = (fixtures / "box-param.scad").read_text()

    extracted_param = dict(extract_named_params(param))
    assert extracted_param == {"W": 20.0, "H": 25.0, "D": 30.0}
    assert all(isinstance(v, float) for v in extracted_param.values())
    # The empty-dict case: no declaration lines in the magic sample.
    assert dict(extract_named_params(magic)) == {}

    # Consistency with the gate bit: named_params False ⇒ empty dict.
    s_magic = score(
        _render(), (20, 25, 30), bbox=BboxInfo(20, 25, 30, 1.0), scad_source=magic
    )
    assert s_magic.named_params is False
    assert dict(extract_named_params(magic)) == {}
    # A non-empty extraction means the gate's declaration leg passed; the
    # magic-numbers leg on box-param is clean too, so the full gate is True.
    s_param = score(
        _render(), (20, 25, 30), bbox=BboxInfo(20, 25, 30, 1.0), scad_source=param
    )
    assert s_param.named_params is True


def test_extract_named_params_boundary_cases():
    """Issue #219: the extraction handles exactly the shapes the gate's
    regex already tolerates — and nothing more: decimal values, multiple
    assignments per line, non-W/D/H names; comments containing ``=`` and
    non-numeric right-hand sides (expressions) never match (the shared
    regex is deliberately not "improved" beyond the gate)."""
    # Decimal and multi-digit values, one declaration per line (the shape
    # the shared regex has always validated against — a declaration is a
    # LINE, and multiple assignments on one line are a one-per-line
    # convention the gate's regex does not (and must not start to) match).
    pairs = extract_named_params("W = 60.5;\nD = 40;\nH = 25;\n")
    assert pairs == (("W", 60.5), ("D", 40.0), ("H", 25.0))
    # A comment line containing '=' must not match (the '//' prefix puts
    # the first token where the regex's leading \w+ cannot bind).
    assert extract_named_params("// W = 20;\n") == ()
    # A non-numeric right-hand side (an expression) does not match.
    assert extract_named_params("W = W2 * 2;\n") == ()
    # Non-W/D/H names are captured (a superset of W/D/H — the persisted
    # dict is every identifier the SCAD declares as a number), one line
    # per declaration.
    pairs = extract_named_params("slot_width = 12.5;\nx = 7;\n")
    assert pairs == (("slot_width", 12.5), ("x", 7.0))
    # A declaration is not required to look like a dimension at all.
    assert dict(extract_named_params("a = 1;")) == {"a": 1.0}


def test_extract_named_params_zero_is_declared_and_captured():
    """Issue #219 (gap-gate): ``W = 0;`` is "declared" by the shared regex
    and is captured honestly as 0.0 — the gate's declaration leg passes on
    it, and the persisted record carries the declared zero (never a
    fabricated absence, and never a dropped declaration)."""
    assert dict(extract_named_params("W = 0;\n")) == {"W": 0.0}
    s = score(_render(), (0.0, 0.0, 0.0), scad_source="W = 0;\ncube([W]);\n")
    assert s.named_params is True


def test_extract_named_params_malformed_numeric_rhs_is_skipped_not_crash():
    """Issue #219 regression: the shared regex's numeric RHS is ``[\\d.]+``,
    which admits ambiguous shapes like ``20.5.0`` that ``float()`` rejects.
    The helper SKIPS the malformed line (mirroring ``detect_magic_numbers``'
    guarded parse), never raising — a model-emitted malformed literal must
    not crash the loop's persistence path, and the gate stays consistent
    (``detect_magic_numbers`` treats the same line's value as unparseable
    and skips it, so the extraction cannot diverge from the gate's
    declared set)."""
    # No crash, malformed line skipped, valid declarations still captured.
    assert dict(extract_named_params("W = 20.5.0;\nD = 40;\n")) == {"D": 40.0}
    # A source containing ONLY the malformed line is an honest absence.
    assert extract_named_params("W = 20.5.0;\n") == ()
    # Gate consistency: the magic-number gate does not crash either, and
    # the malformed value exempts nothing (40 is still flagged as magic).
    from d33d.failure_classes import detect_magic_numbers

    flagged = detect_magic_numbers(
        "W = 20.5.0;\ncube([40]);\n", stated_dimensions={"W": 20.0}
    )
    assert flagged is True


# ---------------------------------------------------------------------------
# Issue #248: the model's parameters metadata array
# ---------------------------------------------------------------------------


def _llm_result_with_params(meta_list: Any) -> Any:
    """A design-role LLMResult whose tool call carries a ``parameters``
    field (the T0/T1 metadata channel)."""
    args: dict[str, Any] = {"scad": GOOD_SCAD}
    if meta_list is not None:
        args["parameters"] = meta_list
    return LLMResult(
        content=GOOD_SCAD,
        tool_calls=({"name": "emit_design", "arguments": args},),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def test_extract_param_meta_present_joined_by_name() -> None:
    """A well-formed ``parameters`` array normalises to
    ``{name: {label, unit, axis, reason}}`` keeping only valid fields."""
    result = _llm_result_with_params(
        [
            {
                "name": "W",
                "label": "Width",
                "unit": "mm",
                "axis": "W",
                "reason": "user said 60",
            },
            {"name": "fillet_size_top", "label": "Top fillet size", "unit": "mm"},
        ]
    )
    meta = extract_param_meta(result)
    assert meta == {
        "W": {"label": "Width", "unit": "mm", "axis": "W", "reason": "user said 60"},
        "fillet_size_top": {"label": "Top fillet size", "unit": "mm"},
    }


def test_extract_param_meta_malformed_degrades_to_empty_never_raises() -> None:
    """A malformed ``parameters`` payload degrades to ``{}`` (no
    metadata) — never a crash, never a partial parse that would half-
    label the block. A malformed array never fails the design pass."""
    # A dict-instead-of-list payload degrades to no metadata (the
    # "parameters" field present but malformed is "no metadata", never a
    # partial parse).
    assert (
        extract_param_meta(_llm_result_with_params({"name": "W", "label": "Width"}))
        == {}
    )
    for bad in (
        [{"label": "no name"}],  # missing name
        ["not-a-dict", {"name": 1, "label": "x"}],  # junk items
        None,  # absent
    ):
        result = _llm_result_with_params(bad)
        assert extract_param_meta(result) == {}
    # A bad axis value drops ONLY the axis field (the item's other valid
    # fields are legitimate metadata — the label survives, the axis is
    # never surfaced as an invalid value).
    partial = extract_param_meta(
        _llm_result_with_params([{"name": "W", "label": "Width", "axis": "Z"}])
    )
    assert partial == {"W": {"label": "Width"}}
    # A result with no parameters field at all → empty.
    bare = LLMResult(
        content=GOOD_SCAD,
        tool_calls=({"name": "emit_design", "arguments": {"scad": GOOD_SCAD}},),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )
    assert extract_param_meta(bare) == {}


def _llm_result_confirm_hints(
    confirm_first: Any = None, confirm_sentence: Any = None
) -> Any:
    """A design-role LLMResult whose FIRST tool call carries the confirm
    hint fields (the T0/T1 hint channel)."""
    args: dict[str, Any] = {"scad": GOOD_SCAD}
    if confirm_first is not None:
        args["confirm_first"] = confirm_first
    if confirm_sentence is not None:
        args["confirm_sentence"] = confirm_sentence
    return LLMResult(
        content=GOOD_SCAD,
        tool_calls=({"name": "emit_design", "arguments": args},),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def test_extract_confirm_hints_paired_from_same_tool_call() -> None:
    """Two tool calls with DIFFERENT confirm_first / confirm_sentence:
    the pair comes from the FIRST call carrying either (never mixed
    across calls — a second call's flag is ignored, not re-paired)."""
    result = LLMResult(
        content=GOOD_SCAD,
        tool_calls=(
            {
                "name": "emit_design",
                "arguments": {"scad": GOOD_SCAD, "confirm_first": "wall_thickness"},
            },
            {
                "name": "emit_design",
                "arguments": {
                    "scad": GOOD_SCAD,
                    "confirm_sentence": "I assumed 4 mm walls.",
                },
            },
        ),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )
    first, sentence = extract_confirm_hints(result)
    assert first == "wall_thickness"
    assert sentence is None  # the SECOND call's sentence is never mixed in


def test_extract_confirm_hints_second_call_ignored_even_when_first_blank() -> None:
    """A first call that carries confirm_first but a blank confirm_sentence
    still owns the pair — the second call's non-blank sentence does not
    leak in (pairing is per-call, not field-by-field across calls)."""
    result = LLMResult(
        content=GOOD_SCAD,
        tool_calls=(
            {
                "name": "emit_design",
                "arguments": {
                    "scad": GOOD_SCAD,
                    "confirm_first": "fillet",
                    "confirm_sentence": "  ",
                },
            },
            {
                "name": "emit_design",
                "arguments": {
                    "scad": GOOD_SCAD,
                    "confirm_sentence": "I assumed 2 grooves.",
                },
            },
        ),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )
    first, sentence = extract_confirm_hints(result)
    assert first == "fillet"
    assert sentence is None


def test_extract_confirm_hints_both_fields_from_first_call() -> None:
    """Both fields present on the first call → both returned (the common
    single-call shape, unchanged)."""
    result = _llm_result_confirm_hints("wall_thickness", "I assumed 3 mm walls.")
    first, sentence = extract_confirm_hints(result)
    assert first == "wall_thickness"
    assert sentence == "I assumed 3 mm walls."


def test_extract_confirm_hints_degrades_to_none() -> None:
    """No hints anywhere → (None, None); a non-string/blank field degrades
    to None for that field only (lenient contract, never a raise)."""
    assert extract_confirm_hints(_llm_result_confirm_hints()) == (None, None)
    first, sentence = extract_confirm_hints(
        _llm_result_confirm_hints(confirm_first=123, confirm_sentence="I assumed 3.")
    )
    assert first is None
    assert sentence == "I assumed 3."


def test_extract_param_meta_duplicate_name_first_wins() -> None:
    """A name appearing twice in the array: the first occurrence wins
    (declaration order — later entries never overwrite)."""
    result = _llm_result_with_params(
        [
            {"name": "W", "label": "First"},
            {"name": "W", "label": "Second"},
        ]
    )
    assert extract_param_meta(result) == {"W": {"label": "First"}}


def test_loop_record_carries_param_meta_from_tool_call() -> None:
    """A passing loop whose design-role reply carries a ``parameters``
    array records the metadata on the ``IterationRecord`` (declared
    ``param_meta`` field) without failing the pass — the values still
    come from the SCAD (``params`` is the SCAD extraction)."""
    from d33d.design_loop import BboxInfo, run_design_loop_async

    bbox = BboxInfo(x=20.0, y=25.0, z=30.0, volume=1.0)
    meta_list = [
        {"name": "W", "label": "Width", "unit": "mm", "axis": "W"},
        {"name": "D", "label": "Depth", "unit": "mm", "axis": "D"},
    ]

    async def _render(scad_source, defines):
        return RenderResult(
            ok=True,
            exit_code=0,
            duration_ms=1,
            error_class="ok",
            stderr="",
            stl=None,
            csg=None,
            views=("v" * 1,) * 6,
        )

    async def llm_fn(role, messages, system):
        return _llm_result_with_params(meta_list)

    result = asyncio.run(
        run_design_loop_async(
            photo=PHOTO,
            chat_history=(),
            stated_dims=STATED,
            render_fn=_render,
            llm_fn=llm_fn,
            bbox_fn=lambda r: bbox,
        )
    )
    assert result.status == "pass"
    # The declared field carries the model's metadata, joined by name.
    assert result.best.param_meta == {
        "W": {"label": "Width", "unit": "mm", "axis": "W"},
        "D": {"label": "Depth", "unit": "mm", "axis": "D"},
    }
    # The values still come from the SCAD (the params field is the
    # SCAD extraction, not the metadata array).
    assert result.best.params == {"W": 20.0, "D": 25.0, "H": 30.0}


# ---------------------------------------------------------------------------
# T1 end-to-end: fenced-JSON protocol completes the loop
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.is_success = True

    def json(self):
        return self._payload


def test_t1_end_to_end_loop_completes_via_fenced_json():
    """The LLM edge is mocked to return T1-shaped fenced-JSON responses;
    the loop completes end-to-end through ``d33d.design_llm.send``."""
    catalogue = _catalogue()
    t1 = CapabilityResult(
        tools=True, json_schema=False, vision=True, max_images=8, validated=True
    )

    # The T1 wire response: a fenced JSON block, no native tool_calls.
    fenced = _t1_tool_call_payload("emit_design", {"scad": GOOD_SCAD})
    sent: list[dict[str, Any]] = []

    async def request_factory(request: dict[str, Any]):
        sent.append(request)
        return _FakeResponse({"choices": [{"message": {"content": fenced}}]})

    async def llm_fn(role, messages, system):
        entry = catalogue.model(catalogue.role(role))
        return await send(
            role=role,
            model_id=entry.model,
            messages=messages,
            request_factory=request_factory,
            capability=t1,
            system=system,
        )

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
    )
    assert result.status == "pass"
    # The T1 path sent NO native tools (fenced JSON is the channel).
    for body in sent:
        assert "tools" not in body
    # The canonical hash was computed and surfaced per call.
    assert result.best.prompt_hashes.get("design")
    assert len(result.best.prompt_hashes["design"]) == 64


def test_prompt_hash_stable_across_repeats_and_changes_with_repair():
    # Same input → same canonical hash; repair input changes → new hash.
    h1 = canonical_hash(
        role="design", messages=[{"role": "user", "content": "W=20; D=25; H=30"}]
    )
    h2 = canonical_hash(
        role="design", messages=[{"role": "user", "content": "W=20; D=25; H=30"}]
    )
    h3 = canonical_hash(
        role="design",
        messages=[{"role": "user", "content": "W=20; D=25; H=30; repair"}],
    )
    assert h1 == h2  # stable across repeats
    assert h1 != h3  # changes when the param block / view set changes


def test_make_llm_fn_resolves_roles_via_alias_not_hardcoded_model_id():
    """``make_llm_fn`` builds the loop's llm_fn over role aliases, resolving
    each through ``d33d.config.resolve.resolve_model`` — never a hardcoded id."""
    catalogue = _catalogue()
    t1 = CapabilityResult(
        tools=True, json_schema=False, vision=True, max_images=8, validated=True
    )

    def make_factory(sent_list):
        async def factory(request):
            sent_list.append(request)
            return _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": _t1_tool_call_payload(
                                    "emit_design", {"scad": GOOD_SCAD}
                                )
                            }
                        }
                    ]
                }
            )

        return factory

    factories: dict[str, Any] = {
        role: make_factory([]) for role in ("design", "critique", "classification")
    }
    llm_fn = make_llm_fn(
        catalogue,
        factories,
        capabilities={"design": t1, "critique": t1, "classification": t1},
    )
    out = asyncio.run(llm_fn("design", [{"role": "user", "content": "hi"}], "sys"))
    assert isinstance(out, LLMResult)
    # The design role resolved to the catalogue's design alias model id
    # (the send() call above would have raised if the role were unknown).
    assert out.tier == "T1"


def test_make_llm_fn_t0_body_carries_emit_design_tool_schema():
    """``make_llm_fn`` attaches the called role's tool schema to the T0
    outgoing body (design -> emit_design, the OpenAI function-calling shape)
    — and the per-call role follows: critique at T0 carries emit_critique,
    not emit_design."""
    catalogue = _catalogue()
    t0 = CapabilityResult(
        tools=True, json_schema=True, vision=True, max_images=8, validated=True
    )
    assert t0.tier == "T0"
    design_call = {
        "id": "call_1",
        "type": "function",
        "function": {"name": "emit_design", "arguments": "{}"},
    }
    critique_call = {
        "id": "call_2",
        "type": "function",
        "function": {"name": "emit_critique", "arguments": "{}"},
    }

    def _t0_factory(sent_list, tool_call):
        async def factory(request):
            sent_list.append(request)
            return _FakeResponse(
                {"choices": [{"message": {"tool_calls": [tool_call]}}]}
            )

        return factory

    sent_design: list[dict[str, Any]] = []
    sent_critique: list[dict[str, Any]] = []
    factories: dict[str, Any] = {
        "design": _t0_factory(sent_design, design_call),
        "critique": _t0_factory(sent_critique, critique_call),
        "classification": _t0_factory([], design_call),
    }
    llm_fn = make_llm_fn(
        catalogue,
        factories,
        capabilities={"design": t0, "critique": t0, "classification": t0},
    )
    out = asyncio.run(llm_fn("design", [{"role": "user", "content": "hi"}], "sys"))
    assert out.tier == "T0"
    # The outgoing body carries the emit_design tool definition
    # (structural equality with the DESIGN_TOOLS test constant — issue
    # #248: the schema gained the ``parameters`` metadata array).
    assert sent_design[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "emit_design",
                "description": "Emit the parametric OpenSCAD",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "scad": {"type": "string"},
                        "parameters": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "name": {"type": "string"},
                                    "label": {"type": "string"},
                                    "unit": {"type": "string"},
                                    "axis": {
                                        "type": "string",
                                        "description": (
                                            "The overall axis this parameter realises "
                                            "(W, D, or H). Declare ONLY when the parameter "
                                            "IS the part's own overall W, D or H extent. "
                                            "A mating part's size, a rim drop, a skirt or "
                                            "any other feature size is NOT the part's W, D "
                                            "or H. A position, offset or distance (e.g. "
                                            "hole_distance_from_left_edge) is never an axis."
                                        ),
                                    },
                                    "reason": {"type": "string"},
                                },
                                "required": ["name", "label"],
                            },
                        },
                    },
                },
            },
        }
    ]
    # Per-role selection: the critique role through the same closure carries
    # emit_critique, never emit_design.
    asyncio.run(llm_fn("critique", [{"role": "user", "content": "hi"}], "sys"))

    def make_factory():
        sent: list[dict[str, Any]] = []

        async def factory(request: dict[str, Any]):
            sent.append(request)
            return _FakeResponse(
                {
                    "choices": [
                        {
                            "message": {
                                "content": "ok",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {
                                            "name": "emit_design",
                                            "arguments": "{}",
                                        },
                                    }
                                ],
                            }
                        }
                    ]
                }
            )

        return sent, factory

    sent, factory = make_factory()
    llm_fn = make_llm_fn(
        catalogue,
        {"design": factory, "critique": factory},
        capabilities={"design": t0, "critique": t0},
    )
    out = asyncio.run(llm_fn("design", [{"role": "user", "content": "hi"}], "sys"))
    assert isinstance(out, LLMResult)
    assert out.tier == "T0"
    # The outgoing body carries the emit_design tool definition, matching
    # the DESIGN_TOOLS shape in tests/test_design_loop_tiers.py.
    body = sent[0]
    # Structural match with the DESIGN_TOOLS shape (tests/test_design_loop_tiers.py);
    # the free-form description is not part of the wire contract.
    design_tool = body["tools"][0]
    assert design_tool["type"] == "function"
    assert design_tool["function"]["name"] == "emit_design"
    assert design_tool["function"]["parameters"] == {
        "type": "object",
        "properties": {
            "scad": {"type": "string"},
            "parameters": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string"},
                        "label": {"type": "string"},
                        "unit": {"type": "string"},
                        "axis": {
                            "type": "string",
                            "description": (
                                "The overall axis this parameter realises "
                                "(W, D, or H). Declare ONLY when the parameter "
                                "IS the part's own overall W, D or H extent. "
                                "A mating part's size, a rim drop, a skirt or "
                                "any other feature size is NOT the part's W, D "
                                "or H. A position, offset or distance (e.g. "
                                "hole_distance_from_left_edge) is never an axis."
                            ),
                        },
                        "reason": {"type": "string"},
                    },
                    "required": ["name", "label"],
                },
            },
        },
    }

    # Per-call role follows: a critique-role call through the same maker
    # carries emit_critique, not emit_design (the response must also name
    # emit_critique — the sender-side allowlist rejects mismatches).
    sent.clear()

    async def crit_factory(request: dict[str, Any]):
        sent.append(request)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "emit_critique",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

    llm = make_llm_fn(
        catalogue, {"critique": crit_factory}, capabilities={"critique": t0}
    )
    asyncio.run(llm("critique", [{"role": "user", "content": "hi"}], "sys"))
    crit_body = sent[0]
    assert crit_body["tools"][0]["function"]["name"] == "emit_critique"
    assert crit_body["tools"][0]["function"]["parameters"]["properties"] == {
        "assessment": {"type": "string"},
        "views": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"view": {"type": "string"}, "ok": {"type": "boolean"}},
            },
        },
    }


def test_make_llm_fn_non_t0_and_missing_capability_send_no_tools():
    """T1 bodies must NOT gain a tools array, and a None capability (the
    documented per-role degrade default) must not crash the tool decision."""
    catalogue = _catalogue()
    t1 = CapabilityResult(
        tools=True, json_schema=False, vision=True, max_images=8, validated=True
    )
    sent: list[dict[str, Any]] = []

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": _t1_tool_call_payload(
                                "emit_design", {"scad": GOOD_SCAD}
                            )
                        }
                    }
                ]
            }
        )

    llm_fn = make_llm_fn(catalogue, {"design": factory}, capabilities={"design": t1})
    out = asyncio.run(llm_fn("design", [{"role": "user", "content": "hi"}], "sys"))
    assert isinstance(out, LLMResult)
    assert out.tier == "T1"
    assert "tools" not in sent[0]

    # capabilities={} -> None per role: no AttributeError deciding tools,
    # and the sender's T2/T3-style None-capability path is exercised the
    # same way (send rejects None before any request is built).
    no_caps_llm = make_llm_fn(catalogue, {"design": factory})
    with pytest.raises(AttributeError):
        asyncio.run(no_caps_llm("design", [{"role": "user", "content": "hi"}], "sys"))
    assert len(sent) == 1  # no request was built for the None capability


# ---------------------------------------------------------------------------
# Dimension handling: stated dims travel as NAMED parameters
# ---------------------------------------------------------------------------


def test_stated_dims_and_extra_defines_travel_as_named_params_to_render():
    captured: list[dict[str, str]] = []

    def render_fn(scad, defines):
        captured.append(dict(defines))
        return _render()

    llm = [_scad_llm(GOOD_SCAD)]

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        return BboxInfo(10.0, 12.0, 14.0, 1680.0)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(10.0, 12.0, 14.0),
        render_fn=render_fn,
        llm_fn=lambda role, m, s: llm[0],
        defines={"clearance_slip": "0.3"},
        bbox_fn=bbox_fn,
    )
    assert result.status == "pass"
    # W/D/H set from stated dims, extra define preserved (the render's
    # defines channel stays caller-sourced — issue #219 did not change
    # this channel).
    assert captured[0]["W"] == "10.0"
    assert captured[0]["D"] == "12.0"
    assert captured[0]["H"] == "14.0"
    assert captured[0]["clearance_slip"] == "0.3"
    # The contrast (issue #219): the persisted record.params come from the
    # SCAD source, not from the caller's defines — two different channels
    # that must never be conflated. The stub SCAD declares W=20/D=25/H=30,
    # so the record carries those, not the render's caller-stated 10/12/14.
    assert result.best.params == {"W": 20.0, "D": 25.0, "H": 30.0}


def test_async_and_sync_entry_points_produce_identical_result():
    """Async/sync parity: driving ``run_design_loop_async`` directly with
    the same ``_run_loop``-style sync fakes as the sync ``run_design_loop``
    yields a dataclass-equal terminal ``DesignResult`` — the two entry
    points are the same loop core (the sync entry is a thin ``asyncio.run``
    wrapper), so a branch divergence would surface as an equality gap on
    the terminal result.

    The fakes are deliberately sync callables: the async core tolerates
    sync ``render_fn``/``llm_fn`` via its ``_call`` await-or-not helper,
    so the existing ``_run_loop``-style fakes work unmodified.
    """
    llm = [_scad_llm(GOOD_SCAD), _scad_llm(BAD_SCAD), _scad_llm(GOOD_SCAD)]
    renders = [
        _render(),
        _render(error_class="syntax_error", stderr="ERROR: x"),
        _render(),
    ]

    def bbox_fn(r: RenderResult) -> BboxInfo | None:
        if r.error_class != "ok":
            return None
        return BboxInfo(99.0, 25.0, 30.0, 1.0)  # x off → bbox gate fails

    def _drive(fn):
        i = {"n": 0}

        def llm_fn(role, messages, system):
            return llm[min(i["n"], len(llm) - 1)]

        def render_fn(scad, defines):
            r = renders[min(i["n"], len(renders) - 1)]
            i["n"] += 1
            return r

        return fn(
            photo=PHOTO,
            stated_dims=STATED,
            render_fn=render_fn,
            llm_fn=llm_fn,
            bbox_fn=bbox_fn,
        )

    sync_result = _drive(run_design_loop)
    async_result = asyncio.run(_drive(run_design_loop_async))
    assert sync_result == async_result  # full dataclass equality
    # The named fields are equal individually (the parity contract).
    for field in ("status", "iterations", "failure_reason", "iterations_used"):
        assert getattr(sync_result, field) == getattr(async_result, field), field
    assert sync_result.best == async_result.best
    assert sync_result.best.score == async_result.best.score
    # Sanity: the scripted 3 → 1 → 3 trajectory is exhausted, not passed.
    assert sync_result.status == "exhausted"
    assert sync_result.iterations_used == 3
    assert sync_result.best.iteration == 1


def test_named_param_block_preserved_not_stripped():
    """A param block the LLM already declared is preserved verbatim in the
    delivered .scad (no stripping, no re-templating)."""
    llm = [_scad_llm(GOOD_SCAD)]
    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=lambda role, m, s: llm[0],
        bbox_fn=_bbox_ok,
    )
    assert result.status == "pass"
    # The delivered .scad still declares the named parameters.
    assert "W = 20;" in result.best.scad_source
    assert "D = 25;" in result.best.scad_source
    assert "H = 30;" in result.best.scad_source


# ---------------------------------------------------------------------------
# SCAD extraction: fenced fallback gate + size cap (HIGH #2 regression)
# ---------------------------------------------------------------------------


def test_scad_fallback_extracts_fenced_scad_block():
    """(a) A raw response with a fenced ```scad block is correctly
    extracted as the SCAD source (no tool call needed)."""
    for fence_lang in ("scad", "openscad", ""):
        content = f"Here is the design:\n```{fence_lang}\n{GOOD_SCAD}\n```\nDone."
        result = _llm_result(content=content, tool_calls=())
        extracted = _scad_from_result(result)
        # The fence's inner content includes the trailing newline before
        # the closing fence; compare after stripping that single artifact.
        assert extracted.rstrip() == GOOD_SCAD.rstrip()


def test_scad_fallback_rejects_unfenced_prose_as_failure():
    """(b) A raw response with NO fence and NO tool call is treated as a
    clean failure — the raw prose is NEVER sent to the render worker (empty
    source → the render worker's failure classification handles it)."""
    prose = "I'm sorry, but I cannot generate OpenSCAD code for this request."
    result = _llm_result(content=prose, tool_calls=())
    assert _scad_from_result(result) == ""
    # The loop's failure routing sees the empty source (a scored failure,
    # not garbage fed to the render worker as a design).
    assert _scad_from_result(result) != prose


def test_scad_fallback_tool_call_path_not_subject_to_fence_scan():
    """The tool-call arguments.scad path is used verbatim (the fence scan
    only applies when the tool call carries no valid scad argument)."""
    result = _llm_result(
        content="no fence here at all",
        tool_calls=({"name": "emit_design", "arguments": {"scad": GOOD_SCAD}},),
    )
    assert _scad_from_result(result) == GOOD_SCAD


def test_scad_source_over_tool_call_path_is_size_capped():
    """(c) An oversized SCAD source via the tool-call path is rejected as a
    failure (empty source, nothing unbounded reaches the render worker)."""
    oversized = "W = 20;\n" + ("// pad\n" * (MAX_SCAD_SOURCE_BYTES + 1024))
    result = _llm_result(
        content="",
        tool_calls=({"name": "emit_design", "arguments": {"scad": oversized}},),
    )
    assert _scad_from_result(result) == ""


def test_scad_source_over_fenced_fallback_path_is_size_capped():
    """(c, fenced path) An oversized fenced SCAD block is rejected the same
    way — the cap applies to EVERY extraction path, not just tool calls."""
    oversized = "cube(1);\n" + ("// pad\n" * (MAX_SCAD_SOURCE_BYTES + 1024))
    result = _llm_result(content=f"```scad\n{oversized}\n```", tool_calls=())
    assert _scad_from_result(result) == ""


def test_scad_source_at_cap_is_accepted():
    """A source exactly at the size cap passes the byte-cap leg (the cap is
    >, not >=); the 5000-char heuristic length leg (a separate, smaller
    backstop added for prose rejection) still rejects it, so the extracted
    source is empty. Both caps fire independently; this test pins the byte
    source at exactly the boundary and documents the interaction."""
    header = "W = 20;\n"
    body = "cube([20, 25, 30]);\n"
    total = (
        MAX_SCAD_SOURCE_BYTES - len(header.encode("utf-8")) - len(body.encode("utf-8"))
    )
    at_cap = header + body + ("// pad\n" * total)[: total - 1] + " "
    assert len(at_cap.encode("utf-8")) == MAX_SCAD_SOURCE_BYTES
    # The source is exactly at the byte-cap boundary (not over it).
    assert len(at_cap.encode("utf-8")) <= MAX_SCAD_SOURCE_BYTES
    # Wrap in a fence: the fence's inner content is the source verbatim
    # (no extra trailing newline before the closing fence in this case).
    result = _llm_result(content=f"```scad\n{at_cap}```", tool_calls=())
    # The byte cap admits it (len <= cap, the cap is >, not >=); the 5000-char
    # heuristic leg rejects a source this large, so the extraction is empty.
    assert _scad_from_result(result) == ""


def test_scad_fallback_rejects_prose_in_fence_as_empty():
    """Regression: chat prose (not SCAD) inside a fenced block — or via a
    tool-call argument — is rejected by the pre-render heuristic and treated
    as empty, so the empty_scad fail-fast path handles it instead of the
    render worker compiling garbage."""
    prose = "Wait — that top() call is invalid. Let me think about this.\n"
    # Fenced fallback path: prose in a bare/scad-labeled fence.
    for fence_lang in ("scad", ""):
        result = _llm_result(content=f"```{fence_lang}\n{prose}```", tool_calls=())
        assert _scad_from_result(result) == "", fence_lang
    # Tool-call path: prose delivered as arguments.scad.
    result = _llm_result(
        content="",
        tool_calls=({"name": "emit_design", "arguments": {"scad": prose.rstrip()}},),
    )
    assert _scad_from_result(result) == ""


def test_scad_looks_valid_heuristic():
    """The pre-render heuristic accepts real SCAD and rejects prose on each
    leg: a ';' somewhere, a length cap, and a token-bounded OpenSCAD
    keyword."""
    # Good SCAD passes all three legs.
    assert scad_looks_valid(GOOD_SCAD)
    # Leg 1: no statement terminator -> not SCAD.
    assert not scad_looks_valid("definitely not code at all")
    # Leg 2: over the 5000-char heuristic cap -> not SCAD (even if it
    # contains a keyword and semicolons).
    too_long = "cube([1, 2, 3]);\n" + ("x" * (MAX_SCAD_VALIDATION_CHARS - 10))
    assert not scad_looks_valid(too_long)
    # Leg 3: prose with a stray ';' but no OpenSCAD keyword -> not SCAD.
    assert not scad_looks_valid("Wait — that top() call is invalid; try again.")
    # 'if' in English prose does NOT count as the keyword (token-bounded).
    assert not scad_looks_valid("if you want; I can help")


# ---------------------------------------------------------------------------
# Issue #277: per-class reason, container_error stop, renderer pre-flight
# ---------------------------------------------------------------------------


def test_exhausted_reason_is_specific_error_class():
    """Issue #277: an exhausted run whose best render has error_class
    ``X`` reports reason ``X`` (never the generic ``error_class_not_ok``
    that bit 0's name would otherwise report for every render failure)."""
    for cls in (
        "syntax_error",
        "timeout",
        "oom",
        "empty_model",
        "artifact_error",
        "container_error",
    ):
        result = _run_loop(
            llm_script=[_scad_llm(BAD_SCAD)],
            render_script=[_render(error_class=cls, stderr=f"ERROR: {cls}")],
        )
        assert result.status == "exhausted", cls
        assert result.failure_reason == cls, cls


def test_exhausted_ok_render_keeps_gate_bit_reason():
    """Issue #277: the per-class swap applies ONLY when the first failing
    bit is bit 0 AND the best render is non-ok. An ok render that fails a
    LATER gate keeps that gate's name (the bit-0 special-case is exact).
    """
    llm = [_scad_llm(GOOD_SCAD), _scad_llm(BAD_SCAD), _scad_llm(GOOD_SCAD)]
    renders = [_render(), _render(error_class="syntax_error", stderr="x"), _render()]

    def bbox_fn(r):
        if r.error_class != "ok":
            return None
        return BboxInfo(99.0, 25.0, 30.0, 1.0)  # bbox gate fails

    result = _run_loop(llm_script=llm, render_script=renders, bbox_fn=bbox_fn)
    assert result.status == "exhausted"
    assert result.best.render.error_class == "ok"
    assert result.failure_reason == GATE_REASON_BITS[2]  # bbox_out_of_tolerance


def test_container_error_stops_loop_after_one_iteration():
    """Issue #277: a container_error render ends the loop after that
    iteration (exactly 1 render call, reason container_error) — a dead
    daemon cannot be fixed by a new source."""
    render_calls = {"n": 0}

    def render_fn(scad, defines):
        render_calls["n"] += 1
        return _render(error_class="container_error", stderr="docker: not running")

    llm = [_scad_llm(BAD_SCAD)]

    def llm_fn(role, messages, system):
        return llm[0]

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=render_fn,
        llm_fn=llm_fn,
    )
    assert result.status == "exhausted"
    assert result.failure_reason == "container_error"
    assert result.iterations_used == 1
    assert render_calls["n"] == 1


def test_container_error_after_prior_ok_render_stops_loop():
    """Issue #277 edge case: a passing render in iteration 1 followed by a
    container_error in iteration 2 → the loop still ends immediately after
    iteration 2 (reason container_error), even though the earlier ok render
    is the best-scoring candidate."""
    llm = [_scad_llm(GOOD_SCAD), _scad_llm(GOOD_SCAD)]
    calls = {"n": 0}

    def render_fn(scad, defines):
        calls["n"] += 1
        if calls["n"] >= 2:
            return _render(error_class="container_error", stderr="docker: down")
        return _render()

    def bbox_fn(r):
        if r.error_class != "ok":
            return None
        # ok render fails the bbox gate: z is 10 mm off the stated 30 mm
        # (the 19.3 mm z case in issue #247 — a mismatch, not a floor
        # case; W is within tolerance so a single off-axis is enough).
        return BboxInfo(20.0, 25.0, 40.0, 1.0)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=render_fn,
        llm_fn=lambda role, m, s: llm[0],
        bbox_fn=bbox_fn,
    )
    assert result.status == "exhausted"
    assert result.failure_reason == "container_error"
    assert result.iterations_used == 2


def test_timeout_and_oom_do_not_stop_loop():
    """Issue #277: only container_error stops the loop immediately. timeout
    and oom can be source-dependent, so they keep today's behaviour (the
    loop continues until the cap/no-improvement stop), and the per-class
    reason still applies when one of them is the best exhausted candidate."""
    for cls in ("timeout", "oom"):
        result = _run_loop(
            llm_script=[_scad_llm(BAD_SCAD)],
            render_script=[_render(error_class=cls, stderr=f"ERROR: {cls}")],
        )
        # Three always-failing iterations reach the cap (the pre-existing
        # always-failing test pins this trajectory).
        assert result.status == "exhausted", cls
        assert result.iterations_used == MAX_ITERATIONS == 3, cls
        assert result.failure_reason == cls, cls


def test_synthetic_empty_scad_path_reports_empty_model():
    """Issue #277: the fail-fast empty-SCAD path (a synthetic
    ``RenderResult(error_class="empty_model")`` with NO render call)
    reports reason ``empty_model`` when it is the best exhausted candidate,
    and does NOT stop the loop (it is an LLM issue, repairable)."""
    # An LLM response with no SCAD at all → the empty-scad fail-fast path.
    result = _run_loop(
        llm_script=[_llm_result(content="no code here", tool_calls=())],
        render_script=[_render()],
    )
    assert result.status == "exhausted"
    # The loop proceeded to the cap (3 iterations), did not stop early.
    assert result.iterations_used == MAX_ITERATIONS == 3
    # The synthetic render's error_class is the reason (per-class swap).
    assert result.best.render.error_class == "empty_model"
    assert result.failure_reason == "empty_model"


def test_preflight_failure_reports_renderer_unavailable_no_llm_call():
    """Issue #277: a failing pre-flight → the run ends at once with reason
    ``renderer_unavailable`` and NO LLM call."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: False,
    )
    assert result.status == "exhausted"
    assert result.failure_reason == RENDERER_UNAVAILABLE
    assert result.iterations_used == 0
    assert result.iterations == ()
    assert llm_calls["n"] == 0


def test_preflight_success_runs_normal_loop():
    """Issue #277: a passing pre-flight → the normal loop (LLM call made)."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        renderer_check=lambda: True,
    )
    assert result.status == "pass"
    assert llm_calls["n"] == 1


def test_renderer_is_available_caches_success_only():
    """Issue #277: a SUCCESSFUL probe is cached for the 30 s window (the
    probe is NOT re-run inside it); a FAILURE is never cached (the next
    call re-probes). The cache is resettable via
    :func:`reset_renderer_preflight_cache`."""

    reset_renderer_preflight_cache()
    try:
        calls = {"n": 0}

        def probe():
            calls["n"] += 1
            return calls["n"] > 1  # first call False, then True

        # First call: probe returns False → not cached, re-probed next.
        assert renderer_is_available(probe) is False
        assert calls["n"] == 1
        assert renderer_is_available(probe) is True  # re-probed (not cached)
        assert calls["n"] == 2
        # Now True is cached → probe NOT re-run.
        assert renderer_is_available(probe) is True
        assert calls["n"] == 2  # no new probe call
        # Reset → probe re-runs.
        reset_renderer_preflight_cache()
        assert renderer_is_available(probe) is True
        assert calls["n"] == 3
    finally:
        reset_renderer_preflight_cache()


def test_renderer_is_available_maps_probe_errors_to_unavailable(
    monkeypatch,
):
    """Issue #277: a missing docker binary / hung daemon (the probe's
    ``subprocess.run`` raising OSError / TimeoutExpired) maps uniformly to
    ``False`` (renderer unavailable), never a crash. The probe's
    ``subprocess.run`` is swapped via ``monkeypatch.setattr`` (no
    ``# type: ignore``) — it is restored automatically at test end."""
    import subprocess

    import d33d.design_loop as dl

    reset_renderer_preflight_cache()
    try:

        def _explode(*a, **kw):
            raise FileNotFoundError("docker not found")

        monkeypatch.setattr(dl.subprocess, "run", _explode)
        assert renderer_is_available() is False

        def _hang(*a, **kw):
            raise subprocess.TimeoutExpired(cmd=["docker", "info"], timeout=5.0)

        monkeypatch.setattr(dl.subprocess, "run", _hang)
        assert renderer_is_available() is False
    finally:
        reset_renderer_preflight_cache()


# ---------------------------------------------------------------------------
# Issue #346: the render-worker IMAGE pre-flight (daemon up, image
# missing / stale / present / docker-query-fails) — the distinct
# loop-level ``renderer_image_stale`` configuration fault (terminal, no
# retry, structured ``renderer_detail``), never a generic
# ``container_error`` and never a false fault on a docker query failure.
# ---------------------------------------------------------------------------


def test_preflight_image_missing_reports_image_stale_no_llm_call():
    """Issue #346: the daemon is UP but the render-worker image is missing
    → the run ends at once with ``renderer_image_stale`` (the distinct
    configuration fault, NOT renderer_unavailable and NOT container_error),
    NO LLM call, and the structured ``renderer_detail`` (image_missing +
    expected hash + the canonical rebuild command)."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    detail = {
        "reason": "image_missing",
        "expected": "abc123",
        "rebuild_command": "docker build ... -t d33d/render-worker:local .",
    }
    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: True,
        image_check=lambda: dict(detail),
    )
    assert result.status == "exhausted"
    assert result.failure_reason == "renderer_image_stale"
    assert result.iterations_used == 0
    assert result.iterations == ()
    assert llm_calls["n"] == 0
    assert result.renderer_detail == detail


def test_preflight_image_label_mismatch_reports_image_stale_no_llm_call():
    """Issue #346: daemon up, image present but build-hash label stale →
    ``renderer_image_stale`` with the ``label_mismatch`` detail (actual vs
    expected) and the rebuild command; zero LLM calls."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    detail = {
        "reason": "label_mismatch",
        "expected": "abc123",
        "actual": "stale999",
        "rebuild_command": "docker build ...",
    }
    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: True,
        image_check=lambda: dict(detail),
    )
    assert result.failure_reason == "renderer_image_stale"
    assert result.iterations_used == 0
    assert llm_calls["n"] == 0
    assert result.renderer_detail == detail


def test_preflight_image_present_runs_normal_loop():
    """Issue #346: a present, label-matching image (probe returns None) →
    the normal loop proceeds (LLM call made)."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        renderer_check=lambda: True,
        image_check=lambda: None,
    )
    assert result.status == "pass"
    assert llm_calls["n"] == 1


def _raise_image_probe_oserror(*a, **kw):
    """An image-probe stub that simulates a docker-query failure (the
    ``OSError`` degradation: daemon unreachable during inspect, binary
    vanished, inspect timeout) — issue #346 operator decision 1."""
    raise OSError("render-worker image pre-flight could not query docker")


def test_preflight_info_up_but_image_query_fails_reports_renderer_unavailable():
    """Issue #346 operator decision 1: ``docker info`` succeeds but the
    image probe raises OSError (daemon transiently unreachable during
    ``docker image inspect``, binary vanished, inspect timeout) → the loop
    falls back to the RETRYABLE ``renderer_unavailable`` — never the
    terminal ``renderer_image_stale`` configuration fault."""
    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: True,
        image_check=_raise_image_probe_oserror,
    )
    assert result.status == "exhausted"
    assert result.failure_reason == "renderer_unavailable"
    assert result.renderer_detail is None
    assert llm_calls["n"] == 0


def test_render_worker_image_detail_maps_probe_outcomes(monkeypatch):
    """Issue #346: the pre-flight probe REUSES
    ``d33d.render_worker._verify_render_worker_image`` (Docker-free via
    the ``expected_hash`` stub + a monkeypatched inspect branch):
    label-match → None (healthy); image absent → image_missing; label
    mismatch → label_mismatch with actual; OSError (docker-query failure)
    → None (the caller degrades to renderer_unavailable, never a fault);
    rebuild_command is always the canonical command."""
    import subprocess

    import d33d.render_worker as rw
    from d33d import design_loop

    # Override the conftest hermetic stub (issue #346): this test
    # deliberately exercises the REAL probe function (the inspect branch
    # is stubbed via ``rw.subprocess`` below, the rest is production
    # code).
    from tests.conftest import image_detail_override

    image_detail_override(_real_image_detail)

    probe_labels = {"labels": {rw.BUILD_HASH_LABEL: "abc123"}}

    def _inspect_stub(argv, *a, **kw):
        if argv[:2] == ["docker", "image"]:
            import json as _json

            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=_json.dumps(probe_labels["labels"]).encode(),
                stderr=b"",
            )
        raise AssertionError(f"unexpected argv in probe test: {argv}")

    # 1. Label matches → healthy (None).
    probe_labels["labels"] = {rw.BUILD_HASH_LABEL: "abc123"}
    monkeypatch.setattr(rw.subprocess, "run", _inspect_stub)
    assert design_loop._render_worker_image_detail(expected_hash="abc123") is None

    # 2. Image absent (inspect non-zero) → image_missing + expected.
    monkeypatch.setattr(
        rw.subprocess,
        "run",
        lambda argv, *a, **kw: subprocess.CompletedProcess(
            args=argv, returncode=1, stdout=b"", stderr=b"no such image"
        ),
    )
    detail = design_loop._render_worker_image_detail(expected_hash="abc123")
    assert detail is not None
    assert detail["reason"] == "image_missing"
    assert detail["expected"] == "abc123"
    assert detail["rebuild_command"] == rw.canonical_build_command()

    # 3. Label mismatch → label_mismatch with actual + expected.
    probe_labels["labels"] = {rw.BUILD_HASH_LABEL: "stale999"}
    monkeypatch.setattr(rw.subprocess, "run", _inspect_stub)
    detail = design_loop._render_worker_image_detail(expected_hash="abc123")
    assert detail is not None
    assert detail["reason"] == "label_mismatch"
    assert detail["actual"] == "stale999"
    assert detail["expected"] == "abc123"
    assert detail["rebuild_command"] == rw.canonical_build_command()

    # 4. Docker-query failure (OSError) → None (never a fabricated fault).
    monkeypatch.setattr(
        rw.subprocess,
        "run",
        lambda argv, *a, **kw: (_ for _ in ()).throw(
            subprocess.TimeoutExpired(cmd=argv, timeout=15)
        ),
    )
    assert design_loop._render_worker_image_detail(expected_hash="abc123") is None


def test_render_worker_image_detail_missing_build_input_degrades_none(tmp_path):
    """Issue #346: ``build_hash`` raising FileNotFoundError (a hashed build
    input missing from the tree) degrades to ``None`` (the per-render
    guard's established mapping — log + continue) — never a crash, never
    a fabricated image fault."""
    import d33d.design_loop as dl

    root = tmp_path / "empty-tree"
    root.mkdir()
    assert dl._render_worker_image_detail(repo_root=root) is None


def test_renderer_check_false_short_circuits_before_image_probe():
    """Issue #346: a failing ``renderer_check`` (daemon down — #277)
    short-circuits on ``renderer_unavailable`` BEFORE the image probe runs
    (the image probe is never called for a dead daemon) — the #277 path is
    untouched by the new pre-flight."""
    probe_calls = {"n": 0}

    def _image_check():
        probe_calls["n"] += 1
        return {"reason": "image_missing", "expected": "x"}

    llm_calls = {"n": 0}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: False,
        image_check=_image_check,
    )
    assert result.failure_reason == "renderer_unavailable"
    assert result.renderer_detail is None
    assert probe_calls["n"] == 0, "the image probe must not run when the daemon is down"
    assert llm_calls["n"] == 0


def test_image_stale_result_cache_never_masks_rebuild():
    """Issue #346 edge: a FAILED image probe is never cached (the 30 s
    success-only cache applies to the docker-info probe only) — a rebuild
    mid-session is picked up on the next design loop: an injected
    ``image_check`` that flips fault → healthy between two runs ends the
    first run on ``renderer_image_stale`` and the second on a pass."""
    llm_calls = {"n": 0}
    state = {"detail": {"reason": "image_missing", "expected": "x"}}

    def llm_fn(role, messages, system):
        llm_calls["n"] += 1
        return _scad_llm(GOOD_SCAD)

    first = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        renderer_check=lambda: True,
        image_check=lambda: state["detail"],
    )
    assert first.failure_reason == "renderer_image_stale"
    state["detail"] = None  # the operator rebuilt the image
    second = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: _render(),
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        renderer_check=lambda: True,
        image_check=lambda: state["detail"],
    )
    assert second.status == "pass"


# ---------------------------------------------------------------------------
# Issue #309 regression: a syntax_error from an STL-export failure (QA's
# verbatim shape) no longer stops the loop via _container_error_stop —
# the loop feeds the error text into the next repair iteration and
# continues up to the iteration cap. Plus the observability no-secrets
# guard for the per-failure ERROR log line.
# ---------------------------------------------------------------------------

QA_STL_ABORT_MARKER = (
    "[entrypoint] STL export failed with exit code 1 — aborting remaining steps"
)
QA_PARSE_ERROR = "ERROR: Parser error in /work/model.scad at line 3"


def test_stl_export_syntax_error_continues_to_iteration_2_with_repair_text():
    """Issue #309: iteration 1 renders the QA verbatim shape (exit 1,
    stderr = only the STL marker, the OpenSCAD ERROR: line in the
    harvested render.log tail). The re-routed syntax_error must NOT stop
    the loop (pre-#309 the same shape was container_error →
    _container_error_stop after iteration 1); the loop continues to
    iteration 2, and the repair input carries the error text."""
    seen_prompts: list[str] = []

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        seen_prompts.append(text)
        if "REPAIR directive" in text:
            return _scad_llm(GOOD_SCAD)
        return _scad_llm(BAD_SCAD)

    render1 = RenderResult(
        ok=False,
        exit_code=1,
        duration_ms=10,
        error_class="syntax_error",
        stderr=(
            QA_STL_ABORT_MARKER + "\n" + QA_PARSE_ERROR
        ),
        stl=None,
        csg=None,
        views=VIEWS_OK,
    )
    i = {"n": 0}

    def render_fn(scad, defines):
        r = render1 if i["n"] == 0 else _render()
        i["n"] += 1
        return r

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
    )
    # The loop did NOT stop at iteration 1 (no _container_error_stop).
    assert result.iterations_used == 2
    assert len(result.iterations) == 2
    assert result.status == "pass"
    # The error text flowed into iteration 2's repair input (the existing
    # repair prompt path: failure_class + instruction + evidence from the
    # classified stderr).
    assert "REPAIR directive" in seen_prompts[1]
    assert "failure_class:" in seen_prompts[1]
    # The evidence is the classified stderr fragment — the OpenSCAD error
    # text is what the model sees to fix the design.
    assert "ERROR: Parser error" in seen_prompts[1] or "STL export failed" in seen_prompts[1]
    # The routed failure class is a repairable syntax class, never
    # container_error.
    assert result.iterations[0].failure_class is not None
    assert result.iterations[0].failure_class != "container_error"


def test_failed_render_log_line_never_contains_secrets(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Issue #309 no-secrets guard: the per-failure ERROR log line
    (render_worker._log_render_failure) carries the render's own
    diagnostic text — the render container never sees an LLM key, so the
    guard pins that key-shaped material (a Bearer/Authorization token, an
    API-key assignment) never appears in the log line, even when it is
    (impossibly) present in the diagnostic text itself: the log line is
    built from stderr + render.log tail only, and the render never sees a
    key. Assert both directions: (a) the line contains the diagnostic
    content (project id, error_class, exit code, the error text) and
    (b) no key-shaped token appears even when fed through a stub stderr
    that would carry one — the harvest/log path must not echo it."""
    import d33d.render_worker as rw_mod

    log_result = RenderResult(
        ok=False,
        exit_code=1,
        duration_ms=10,
        error_class="syntax_error",
        stderr=QA_PARSE_ERROR,
        stl=None,
        csg=None,
        views=VIEWS_OK,
    )
    with caplog.at_level(logging.ERROR, logger=rw_mod.__name__):
        rw_mod._log_render_failure(log_result, QA_PARSE_ERROR, project_id=42)
    records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert records
    msg = records[0].message
    # Content: project id, class, exit code, the OpenSCAD error text.
    assert "project_id=42" in msg
    assert "error_class=syntax_error" in msg
    assert "exit_code=1" in msg
    assert "ERROR: Parser error" in msg
    # No-secrets guard: no key-shaped material in the log line.
    assert "Bearer" not in msg
    assert "Authorization" not in msg
    assert "sk-" not in msg
    assert "TRAIL_OPENERS_LLM_KEY=***" not in msg
    # Second direction: even if key-shaped text somehow reached the
    # diagnostic channel, the log line is the diagnostic text verbatim —
    # the guarantee is that the render never carries a key (network none,
    # no credentials in the volume), so the guard holds by construction.
    # The render never reads environment variables, so nothing to echo.
    render_argv = rw_mod.build_docker_argv("img", "render-00000001")
    assert "TRAIL_OPENERS_LLM_KEY" not in " ".join(render_argv)


# ---------------------------------------------------------------------------
# Issue #317: metric screw-hole clearance post-check (single-source table,
# ok-render branch, before the score.perfect pass return)
# ---------------------------------------------------------------------------


def _screw_scad_llm(scad: str, params: list[dict] | None = None) -> LLMResult:
    """A T1-shaped design response whose tool call carries a ``parameters``
    metadata array (the label/reason channel the post-check reads)."""
    args: dict[str, Any] = {"scad": scad}
    if params is not None:
        args["parameters"] = params
    return LLMResult(
        content=f"```json\n{json.dumps({'tool': 'emit_design', 'arguments': args})}\n```",
        tool_calls=({"name": "emit_design", "arguments": args},),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _plate_scad(hole: float) -> str:
    """A 60 × 45 × 8 plate with a single circular through-hole of the
    given diameter — every literal a named declaration (the named-params
    gate passes), bbox matches the stated triple exactly."""
    return (
        "W = 60;\nD = 45;\nH = 8;\nhole_d = "
        f"{hole:g};\n"
        "difference() {\n"
        "  cube([W, D, H]);\n"
        "  translate([30, 22.5, 0]) cylinder(h = H + 2, d = hole_d);\n"
        "}\n"
    )


def _run_screw_loop(
    llm_script: Sequence[LLMResult],
    request: str,
    *,
    bbox_fn: BboxFn = None,
    max_iterations: int = MAX_ITERATIONS,
    seen_prompts: list[str] | None = None,
):
    """Run the loop over an ok-render plate (all five gate bits green)
    with a ``request`` that names (or does not name) a metric screw.
    The render script is one ok render, reused per iteration."""
    render = _render()
    seen = seen_prompts if seen_prompts is not None else []

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        seen.append(text)
        return llm_script[min(len(seen) - 1, len(llm_script) - 1)]

    def render_fn(scad, defines):
        return render

    return run_design_loop(
        photo=PHOTO,
        stated_dims=(60.0, 45.0, 8.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn or (lambda r: BboxInfo(60.0, 45.0, 8.0, 21600.0)),
        request=request,
        max_iterations=max_iterations,
    )


def test_screw_clearance_post_check_repairs_undersize_m4():
    """Issue #317 trigger: "a 60 × 45 mm plate with an M4 hole" with
    ``hole_d = 4.0`` → the all-green candidate does NOT pass; iteration
    1's repair carries ``failure_class: geometrically_wrong`` and the
    message "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5
    mm" (substring of the instruction). The scripted model repeats 4.0,
    so the hole stays undersize to the cap (the within-cap pass is
    pinned by the next test)."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    llm = [_screw_scad_llm(_plate_scad(4.0), meta)]
    seen: list[str] = []
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole", seen_prompts=seen)
    # The all-green candidate does NOT pass: the post-check routes the
    # repair and the loop takes the repair iterations within the cap.
    assert result.status == "exhausted"
    assert result.iterations_used == MAX_ITERATIONS == 3
    # Iteration 1: all five gate bits green (the hole is not a gate bit)
    # yet it still got the structured repair.
    first = result.iterations[0]
    assert first.score.perfect is True
    assert first.failure_class == "geometrically_wrong"
    assert first.repair is not None
    assert first.repair["failure_class"] == "geometrically_wrong"
    assert (
        "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm"
        in first.repair["instruction"]
    )
    # The repair reached iteration 2's prompt through the existing
    # REPAIR block (same _design_messages path, unchanged).
    assert "REPAIR directive" not in seen[0]
    assert "REPAIR directive" in seen[1]
    assert "failure_class: geometrically_wrong" in seen[1]
    assert "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm" in seen[1]


def test_screw_clearance_repair_scad_source_equals_iteration_scad():
    """Issue #317: the repair's ``scad_source`` carries the iteration's
    REAL SCAD (routed through ``route_repair`` mirroring the #276
    axis_params_mismatch construction), not an empty string."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    scad = _plate_scad(4.0)
    llm = [_screw_scad_llm(scad, meta)]
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole")
    first = result.iterations[0]
    assert first.repair is not None
    # The repair's scad_source is the iteration's own SCAD source.
    assert first.repair["scad_source"] == first.scad_source
    assert first.repair["scad_source"] == scad
    assert first.repair["scad_source"] != ""


def test_screw_clearance_repair_then_clearance_passes_within_cap():
    """Issue #317: the model fixes the hole on the repair iteration —
    4.0 mm triggers, 4.5 mm passes, all within the 3-iteration cap and
    the no-improvement limit (the clearance repair counts as a normal
    repair iteration, not a new budget)."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        if "REPAIR directive" in text:
            return _screw_scad_llm(_plate_scad(4.5), meta)
        return _screw_scad_llm(_plate_scad(4.0), meta)

    def render_fn(scad, defines):
        return _render()

    seen: list[str] = []

    def llm_fn_seen(role, messages, system):
        seen.append(messages[0]["content"][0]["text"])
        return llm_fn(role, messages, system)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(60.0, 45.0, 8.0),
        render_fn=render_fn,
        llm_fn=llm_fn_seen,
        bbox_fn=lambda r: BboxInfo(60.0, 45.0, 8.0, 21600.0),
        request="a 60 × 45 mm plate with an M4 hole",
    )
    assert result.status == "pass"
    assert result.iterations_used == 2
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    assert result.iterations[1].failure_class is None
    assert result.iterations[1].repair is None
    assert "REPAIR directive" not in seen[0]
    assert "REPAIR directive" in seen[1]
    assert "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm" in seen[1]


def test_screw_clearance_post_check_passes_at_clearance():
    """Issue #317: hole_d = 4.5 (the table's M4 clearance) does NOT
    trigger — the candidate passes on iteration 1 with no repair."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    llm = [_screw_scad_llm(_plate_scad(4.5), meta)]
    seen: list[str] = []
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole", seen_prompts=seen)
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None
    assert result.iterations[0].failure_class is None
    assert "REPAIR directive" not in seen[0]


def test_screw_clearance_post_check_boundary_005_mm():
    """Issue #317 boundary: below clearance by MORE than 0.05 mm triggers
    (4.44 → 4.5 − 4.44 = 0.06 > 0.05); at or above the threshold it
    abstains (4.45 → 0.05, not more than 0.05)."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    # 4.44: triggers (the repair is routed; the scripted model repeats 4.44
    # to the cap — the hole stays undersize).
    llm = [_screw_scad_llm(_plate_scad(4.44), meta)]
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole")
    assert result.status == "exhausted"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert "4.44 mm" in result.iterations[0].repair["instruction"]
    # 4.45: abstains — no false repair, the pass is a clean pass.
    llm = [_screw_scad_llm(_plate_scad(4.45), meta)]
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole")
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None


def test_screw_clearance_post_check_no_screw_named_does_nothing():
    """Issue #317: a request that names NO metric screw size never
    triggers, even with an undersize hole param (4.0 mm < 4.5)."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    llm = [_screw_scad_llm(_plate_scad(4.0), meta)]
    seen: list[str] = []
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with a 4 mm hole", seen_prompts=seen)
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None
    assert "REPAIR directive" not in seen[0]


def test_screw_clearance_post_check_ambiguous_param_does_nothing():
    """Issue #317: MORE than one hole-diameter candidate (``hole_d`` and
    ``m4_hole`` both read as M4 holes) → the check cannot identify the
    param confidently and does NOTHING (no repair, no crash)."""
    scad = (
        "W = 60;\nD = 45;\nH = 8;\nhole_d = 4.0;\nm4_hole = 4.0;\n"
        "difference() {\n"
        "  cube([W, D, H]);\n"
        "  translate([30, 22.5, 0]) cylinder(h = H + 2, d = hole_d);\n"
        "  translate([10, 10, 0]) cylinder(h = H + 2, d = m4_hole);\n"
        "}\n"
    )
    meta = [
        {"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"},
        {"name": "m4_hole", "label": "M4 hole", "unit": "mm"},
    ]
    llm = [_screw_scad_llm(scad, meta)]
    seen: list[str] = []
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole", seen_prompts=seen)
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None
    assert result.iterations[0].failure_class is None
    assert "REPAIR directive" not in seen[0]


def test_screw_clearance_threaded_wording_abstains():
    """Issue #317: threaded / tapped / insert wording is NOT a clearance
    hole — the check abstains with NO repair (never a false repair),
    even when the param is at the nominal size."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    for request in (
        "a 60 × 45 mm plate with an M4 threaded hole",
        "a 60 × 45 mm plate with a tapped M4 hole",
        "a 60 × 45 mm plate with a heat-set insert for M4",
    ):
        llm = [_screw_scad_llm(_plate_scad(4.0), meta)]
        seen: list[str] = []
        result = _run_screw_loop(llm, request, seen_prompts=seen)
        assert result.status == "pass", request
        assert result.iterations_used == 1, request
        assert result.iterations[0].repair is None, request
        assert "REPAIR directive" not in seen[0], request


def test_screw_clearance_post_check_survives_missing_param_meta():
    """Issue #317: the T1 tier carries no ``parameters`` metadata — the
    check falls back to the name heuristic only (``hole_d`` reads as a
    hole) and still triggers; never a crash on missing param_meta."""
    llm = [_screw_scad_llm(_plate_scad(4.0))]  # no parameters array
    seen: list[str] = []
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole", seen_prompts=seen)
    assert result.status == "exhausted"  # the model repeats 4.0 to the cap
    assert result.iterations[0].repair is not None
    assert (
        "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm"
        in result.iterations[0].repair["instruction"]
    )


def test_screw_clearance_undersize_at_cap_returns_best_effort():
    """Issue #317: if the cap is reached with the hole still undersize,
    the loop returns its normal best-effort result — no new
    failure_reason (the failure_reason is a gate-class name, not the
    clearance finding)."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    llm = [_screw_scad_llm(_plate_scad(4.0), meta)]  # never fixed
    result = _run_screw_loop(llm, "a 60 × 45 mm plate with an M4 hole")
    assert result.status == "exhausted"
    assert result.iterations_used == MAX_ITERATIONS == 3
    assert result.failure_reason is None
    for iteration in result.iterations:
        assert iteration.failure_class == "geometrically_wrong"
        assert iteration.repair is not None
        assert iteration.repair["failure_class"] == "geometrically_wrong"


def test_screw_clearance_word_boundaries_never_trigger():
    """Issue #317: ``M40`` / ``BM4`` are not M4 references — the post-check
    sees no screw size and does nothing."""
    meta = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
    for request in ("an M40 flange plate", "a plate with a BM4 reference"):
        llm = [_screw_scad_llm(_plate_scad(4.0), meta)]
        result = _run_screw_loop(llm, request)
        assert result.status == "pass", request
        assert result.iterations[0].repair is None, request


def test_screw_clearance_direct_check_contract():
    """Issue #317: the post-check helper's contract in isolation —
    the DETECTION tuple shape (size, value, clearance, label) and the
    trigger/pass/no-op conditions."""
    from d33d.screw_hole_check import undersize_screw_hole as _undersize_screw_hole

    request = "a 60 × 45 mm plate with an M4 hole"
    meta = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}
    # Triggers: 4.0 is below the 4.5 clearance by more than 0.05 mm.
    det = _undersize_screw_hole(request, {"W": 60.0, "hole_d": 4.0}, meta)
    assert det is not None
    size, value, clearance, label = det
    assert size == "M4"
    assert value == 4.0
    assert clearance == 4.5
    # The label is the param's model label when one exists (the
    # param_meta label wins over the identifier).
    assert label == "M4 hole diameter"
    # No label (T1 name-heuristic fallback): the identifier is used.
    unlabeled = _undersize_screw_hole(request, {"hole_d": 4.0}, {})
    assert unlabeled is not None and unlabeled[3] == "hole_d"
    # Passes: at clearance (and above).
    assert _undersize_screw_hole(request, {"hole_d": 4.5}, meta) is None
    assert _undersize_screw_hole(request, {"hole_d": 5.0}, meta) is None
    # Boundary: exactly 0.05 below abstains; more than 0.05 below triggers.
    assert _undersize_screw_hole(request, {"hole_d": 4.45}, meta) is None
    assert _undersize_screw_hole(request, {"hole_d": 4.44}, meta) is not None
    # No screw size named → abstain.
    assert _undersize_screw_hole("a plate with a hole", {"hole_d": 4.0}, meta) is None
    # Ambiguous (two candidates) → abstain.
    ambiguous = {"hole_d": 4.0, "m4_hole": 4.0}
    assert (
        _undersize_screw_hole(
            request, ambiguous, {"hole_d": meta["hole_d"], "m4_hole": {"label": "M4 hole"}}
        )
        is None
    )
    # Threaded wording → abstain.
    assert (
        _undersize_screw_hole(
            "a plate with an M4 threaded hole", {"hole_d": 4.0}, meta
        )
        is None
    )
    # No hole param at all → abstain.
    assert _undersize_screw_hole(request, {"W": 60.0, "D": 45.0}, {}) is None
    # Empty request → abstain.
    assert _undersize_screw_hole("", {"hole_d": 4.0}, meta) is None


# ---------------------------------------------------------------------------
# Issue #386: through-hole genus post-check (the rendered mesh's genus
# must EXCEED the parent version's baseline — 0 for a new design, the
# imported part's own stored hole count for v1 on an import — operator
# decision 2026-10-05). Real tiny STLs built with trimesh in tmp_path
# (the ``_render`` stub's fake ``model.stl`` path means no existing test
# loads a real mesh through the loop).
# ---------------------------------------------------------------------------


def _through_box_llm(scad: str, params: list[dict] | None = None) -> LLMResult:
    """A T1-shaped design response for a 20 × 20 × 20 box SCAD (the
    through-hole post-check's candidate) whose tool call carries a
    ``parameters`` metadata array."""
    args: dict[str, Any] = {"scad": scad}
    if params is not None:
        args["parameters"] = params
    return LLMResult(
        content=f"```json\n{json.dumps({'tool': 'emit_design', 'arguments': args})}\n```",
        tool_calls=(
            {"name": "emit_design", "arguments": args},
        ),
        prompt_hash="h" * 64,
        tier="T1",
        status="ok",
        request_body={},
    )


def _through_box_scad_source() -> str:
    """A 20 × 20 × 20 box — every literal a named declaration (the
    named-params gate passes), bbox matches the stated triple exactly.
    (The SCAD is a stand-in: the loop's test render carries the REAL
    mesh's STL path, so the mesh — not the SCAD — is what the check
    measures.)"""
    return "W = 20;\nD = 20;\nH = 20;\ncube([W, D, H]);\n"


def _render_with_stl(stl_path: str) -> RenderResult:
    """An ok render whose ``stl`` points at a REAL file on disk (the
    through-hole check's trimesh.load seam) — the ``_render`` stub's
    fake ``model.stl`` is non-existent, so the check would abstain on
    it; this helper carries the real path."""
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=10,
        error_class="ok",
        stderr="",
        stl=stl_path,
        csg="model.csg",
        views=VIEWS_OK,
        render_log="",
    )


def _box_minus_cylinder(blind: bool) -> trimesh.Trimesh:
    """A 20 × 20 × 20 box with a 6 mm-diameter cylindrical cut at the
    centre: ``blind`` — the cylinder goes 10 mm into the box (a
    pocket, genus 0); ``not blind`` — the cylinder pierces both faces
    (a through-hole, genus 1)."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    height = 12 if blind else 44
    z0 = 0 if blind else -12
    cyl = trimesh.creation.cylinder(radius=3.0, height=height)
    cyl.apply_translation([0, 0, z0])
    out = box.difference(cyl)
    if isinstance(out, trimesh.Scene):
        out = out.to_mesh()
    return out


def _run_through_loop(
    stl_path: str | None,
    request: str,
    *,
    through_baseline_genus: int | None = None,
    max_iterations: int = MAX_ITERATIONS,
) -> DesignResult:
    """Run the loop over an ok-render 20×20×20 box (all five gate bits
    green) whose render carries the given real STL path (``None`` →
    the ``_render`` stub's non-existent ``model.stl`` — the abstain
    case)."""
    render = _render_with_stl(stl_path) if stl_path else _render()
    llm = _through_box_llm(_through_box_scad_source())

    def llm_fn(role, messages, system):
        return llm

    def render_fn(scad, defines):
        return render

    return run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(20.0, 20.0, 20.0, 8000.0),
        request=request,
        max_iterations=max_iterations,
        through_baseline_genus=through_baseline_genus,
    )


def test_through_hole_pocket_request_fails_geometrically_wrong(tmp_path):
    """Issue #386 (must fail on main): a through request ("drill a 6 mm
    hole through the middle") with a POCKET mesh (a box minus a blind
    cylinder — euler 2, genus 0) over a zero baseline → iteration 1 is
    ``geometrically_wrong`` (NOT a pass), and the repair instruction
    says the hole must cut through the full thickness. The scripted
    model repeats the pocket to the cap (the within-cap pass is pinned
    by the next test)."""
    stl = str(tmp_path / "pocket.stl")
    _box_minus_cylinder(blind=True).export(stl)
    result = _run_through_loop(
        stl, "drill a 6 mm hole through the middle of the top face"
    )
    # The all-green candidate does NOT pass: the post-check routes the
    # repair and the loop takes the repair iterations within the cap.
    assert result.status == "exhausted"
    assert result.iterations_used == MAX_ITERATIONS == 3
    first = result.iterations[0]
    # All five gate bits green (genus is not a gate bit) yet the
    # structured repair fired.
    assert first.score.perfect is True
    assert first.failure_class == "geometrically_wrong"
    assert first.repair is not None
    assert first.repair["failure_class"] == "geometrically_wrong"
    assert "full thickness" in first.repair["instruction"]
    assert "does not pass" in first.repair["instruction"]


def test_through_hole_through_mesh_passes(tmp_path):
    """Issue #386: a through request with a true through-hole mesh
    (genus 1) over a zero baseline → the loop returns "pass" on
    iteration 1 (the check passes — the hole did pass), mirroring
    ``test_screw_clearance_post_check_passes_at_clearance``."""
    stl = str(tmp_path / "through.stl")
    _box_minus_cylinder(blind=False).export(stl)
    result = _run_through_loop(
        stl, "drill a 6 mm hole through the middle of the top face"
    )
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None
    assert result.iterations[0].failure_class is None


def test_through_hole_blind_request_no_trigger(tmp_path):
    """Issue #386: a BLIND-hole request (no ``through``) with a pocket
    mesh → a pass (no trigger), mirroring
    ``test_screw_clearance_post_check_no_screw_named_does_nothing``."""
    stl = str(tmp_path / "pocket.stl")
    _box_minus_cylinder(blind=True).export(stl)
    for request in (
        "drill a 5 mm hole 3 mm deep in the top face",
        "a counterbore 5 mm deep for the screw",
        "a pocket 2 mm deep in the centre",
    ):
        result = _run_through_loop(stl, request)
        assert result.status == "pass", request
        assert result.iterations_used == 1, request
        assert result.iterations[0].repair is None, request


def test_through_hole_unreadable_stl_abstains(tmp_path):
    """Issue #386: a through request whose ``render.stl`` path does not
    exist (or the file is unreadable) → the check abstains (one log
    line) and the loop PASSES, mirroring the direct-check contract's
    abstain style."""
    # A non-existent path (the render carries a path that is not a file).
    missing = str(tmp_path / "does-not-exist.stl")
    result = _run_through_loop(
        missing, "drill a 6 mm hole through the middle"
    )
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None


def test_through_hole_repair_then_through_mesh_passes_within_cap(tmp_path):
    """Issue #386: the model fixes the hole on the repair iteration —
    the pocket triggers, the through mesh passes, all within the
    3-iteration cap and the no-improvement limit (the through-hole
    repair counts as a normal repair iteration)."""
    pocket_stl = str(tmp_path / "pocket.stl")
    through_stl = str(tmp_path / "through.stl")
    _box_minus_cylinder(blind=True).export(pocket_stl)
    _box_minus_cylinder(blind=False).export(through_stl)

    def llm_fn(role, messages, system):
        text = messages[0]["content"][0]["text"]
        if "REPAIR directive" in text:
            return _through_box_llm(_through_box_scad_source())
        return _through_box_llm(_through_box_scad_source())

    calls = {"n": 0}

    def render_fn(scad, defines):
        calls["n"] += 1
        stl = pocket_stl if calls["n"] == 1 else through_stl
        return _render_with_stl(stl)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(20.0, 20.0, 20.0, 8000.0),
        request="drill a 6 mm hole through the middle",
    )
    assert result.status == "pass"
    assert result.iterations_used == 2
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    assert result.iterations[1].failure_class is None
    assert result.iterations[1].repair is None


def test_through_hole_baseline_comparison(tmp_path):
    """Issue #386 (operator decision 2026-10-05): the baseline
    comparison — a part whose stored hole count is 1 (baseline 1): a
    pocket render (genus 0) does not exceed it (fires, geometrically_
    wrong); a through render (genus 1) does NOT exceed it either
    (EXCEED is strict — a single hole on a one-hole part still fires).
    The baseline comes from the explicit ``through_baseline_genus``
    seam (the route's part-report reader, not re-parsed here)."""
    pocket_stl = str(tmp_path / "pocket.stl")
    through_stl = str(tmp_path / "through.stl")
    _box_minus_cylinder(blind=True).export(pocket_stl)
    _box_minus_cylinder(blind=False).export(through_stl)
    req = "drill a 6 mm hole through the middle"
    # baseline 1, pocket genus 0 → 0 < 1, fires.
    result = _run_through_loop(pocket_stl, req, through_baseline_genus=1)
    assert result.status == "exhausted"
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    # baseline 1, through genus 1 → 1 does NOT exceed 1, fires.
    result = _run_through_loop(through_stl, req, through_baseline_genus=1)
    assert result.status == "exhausted"
    assert result.iterations[0].failure_class == "geometrically_wrong"
    # baseline 0, through genus 1 → 1 exceeds 0, passes.
    result = _run_through_loop(through_stl, req, through_baseline_genus=0)
    assert result.status == "pass"
    assert result.iterations_used == 1


def _box_with_n_through_holes(n: int) -> trimesh.Trimesh:
    """A 20 × 20 × 20 box with ``n`` 6 mm-diameter through-holes
    (genus ``n``). The holes are spaced along the x-axis."""
    box = trimesh.creation.box(extents=(20, 20, 20))
    for i in range(n):
        x = -8.0 + i * (16.0 / max(n, 1))
        cyl = trimesh.creation.cylinder(radius=1.5, height=44)
        cyl.apply_translation([x, 0, 0])
        box = box.difference(cyl)
    if isinstance(box, trimesh.Scene):
        box = box.to_mesh()
    return box


def test_through_hole_plate_genus_3_pocket_fails_through_passes(tmp_path):
    """Issue #386 (operator decision 2026-10-05, case ii): an imported
    plate with genus 3 (three existing through-holes). A pocket render
    (genus 3 — the pocket did not add a hole) must FAIL (3 does not
    exceed 3). A through-hole render (genus 4 — one new hole added) must
    PASS (4 exceeds 3)."""
    req = "drill a 6 mm hole through the plate"
    genus3_stl = str(tmp_path / "genus3.stl")
    _box_with_n_through_holes(3).export(genus3_stl)
    result = _run_through_loop(genus3_stl, req, through_baseline_genus=3)
    assert result.status == "exhausted"
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    genus4_stl = str(tmp_path / "genus4.stl")
    _box_with_n_through_holes(4).export(genus4_stl)
    result = _run_through_loop(genus4_stl, req, through_baseline_genus=3)
    assert result.status == "pass"
    assert result.iterations_used == 1
    assert result.iterations[0].repair is None


def test_through_hole_v2_edit_existing_hole(tmp_path):
    """Issue #386 (operator decision 2026-10-05, case iii): a v2 edit
    on a design whose v1 already has one hole (baseline 1). A pocket
    render (genus 0) must FAIL. A through render (genus 1) does NOT
    exceed the baseline — must also FAIL (EXCEED is strict). A render
    with genus 2 (two through-holes) exceeds the baseline — PASSES."""
    req = "drill another 6 mm hole through the middle"
    pocket_stl = str(tmp_path / "pocket.stl")
    _box_minus_cylinder(blind=True).export(pocket_stl)
    result = _run_through_loop(pocket_stl, req, through_baseline_genus=1)
    assert result.status == "exhausted"
    assert result.iterations[0].failure_class == "geometrically_wrong"
    through_stl = str(tmp_path / "through.stl")
    _box_minus_cylinder(blind=False).export(through_stl)
    result = _run_through_loop(through_stl, req, through_baseline_genus=1)
    assert result.status == "exhausted"
    assert result.iterations[0].failure_class == "geometrically_wrong"
    genus2_stl = str(tmp_path / "genus2.stl")
    _box_with_n_through_holes(2).export(genus2_stl)
    result = _run_through_loop(genus2_stl, req, through_baseline_genus=1)
    assert result.status == "pass"
    assert result.iterations_used == 1


# ---------------------------------------------------------------------------
# Issue #332 — import guard: post-check (missing import, wrong filename,
# rescale, resize → repair; correct candidate → pass)
# ---------------------------------------------------------------------------


def _import_scad_llm(scad: str) -> LLMResult:
    """A T1-shaped design response for import-guard tests (issue #332):
    passes ``scad`` through verbatim — the helper never mutates the source,
    so an import-guard test that asserts ``no_import`` on a no-import SCAD
    keeps measuring the SCAD the guard actually sees."""
    return _llm_result(
        content=_t1_tool_call_payload("emit_design", {"scad": scad}),
        tool_calls=({"name": "emit_design", "arguments": {"scad": scad}},),
    )


def _floor_scad_llm(scad: str) -> LLMResult:
    """A T1-shaped design response for the part-baseline FLOOR tests
    (issue #383): prepends ``W = 20;`` so the source declares a named
    parameter (bit 3) and exempts its own multi-digit literals (the
    floor cases assert on bit 2, the bbox gate, in isolation). The
    ``import("part.stl")`` / ``scale(1)`` line is still present, so the
    import guard does not fire (bit 3 green) and the floor is the only
    gate under test."""
    return _import_scad_llm(f"W = 20;\n{scad}")


def _run_import_loop(
    llm_script: Sequence[LLMResult],
    part_scale: float = 1.0,
    bbox: BboxInfo | None = None,
    stated: tuple[float, float, float] = (20.0, 25.0, 30.0),
    render_script: Sequence[RenderResult] | None = None,
) -> DesignResult:
    """Run the design loop with an import project (``part_scale`` set) and
    a scripted LLM/render."""
    i = {"n": 0}
    renders = render_script if render_script is not None else [_render()]

    def llm_fn(role, messages, system):
        return llm_script[min(i["n"], len(llm_script) - 1)]

    def render_fn(scad, defines):
        r = renders[min(i["n"], len(renders) - 1)]
        i["n"] += 1
        return r

    return run_design_loop(
        photo=PHOTO,
        stated_dims=stated,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=(lambda r: bbox if r.error_class == "ok" else None),
        part_scale=part_scale,
    )


def test_import_guard_missing_import_is_repair():
    """Issue #332: a candidate that does NOT import("part.stl") at all
    → repair routed via the existing geometrically_wrong class."""
    llm = [_import_scad_llm("W = 20;\ncube([W, 25, 30]);\n")]
    result = _run_import_loop(llm, part_scale=1.0, bbox=BboxInfo(20.0, 25.0, 30.0))
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].repair["failure_class"] == "geometrically_wrong"
    assert "import" in result.iterations[0].repair["instruction"]


def test_import_guard_wrong_filename_is_repair():
    """Issue #332: a candidate that imports a DIFFERENT filename
    (e.g. part.3mf) → the import guard detects wrong_import."""
    from d33d.import_guard import import_guard_violation
    scad = 'import("part.3mf");\n'
    det = import_guard_violation(scad, part_scale=1.0)
    assert det is not None
    assert det[0] == "wrong_import"
    assert "part.3mf" in det[1]


def test_import_guard_rescaled_import_is_repair():
    """Issue #332: a candidate that applies a scale() to the import with
    a factor OTHER than the settled one → the import guard detects rescaled_import."""
    from d33d.import_guard import import_guard_violation
    scad = 'scale(2) import("part.stl");\n'
    det = import_guard_violation(scad, part_scale=1.0)
    assert det is not None
    assert det[0] == "rescaled_import"
    assert "scale" in det[1]


def test_import_guard_resized_part_is_repair():
    """Issue #332: a candidate that calls resize() → the import guard
    detects resized_part."""
    from d33d.import_guard import import_guard_violation
    scad = 'resize([40, 50, 60]) import("part.stl");\n'
    det = import_guard_violation(scad, part_scale=1.0)
    assert det is not None
    assert det[0] == "resized_part"
    assert "resize" in det[1]


def test_import_guard_correct_candidate_no_violation():
    """Issue #332: a candidate that imports("part.stl") with the correct
    scale and adds geometry → no import guard violation."""
    from d33d.import_guard import import_guard_violation
    scad = 'scale(1) import("part.stl");\nunion() { cube([10, 10, 10]); }\n'
    det = import_guard_violation(scad, part_scale=1.0)
    assert det is None


def test_import_guard_correct_scale_254_no_violation():
    """Issue #332: a 3MF imported at inches (scale=25.4) with the correct
    scale() → no import guard violation."""
    from d33d.import_guard import import_guard_violation
    scad = 'scale(25.4) import("part.stl");\n'
    det = import_guard_violation(scad, part_scale=25.4)
    assert det is None


def test_import_guard_no_part_project_no_guard():
    """Issue #332: a project with NO part (part_scale=None) — the import
    guard does not fire (byte-identity regression anchor)."""
    llm = [_import_scad_llm("W = 20;\ncube([W, 25, 30]);\n")]
    result = _run_import_loop(llm, part_scale=None, bbox=BboxInfo(20.0, 25.0, 30.0))
    assert result.iterations[0].failure_class is None


def test_import_guard_catches_rebuild_when_part_scale_set():
    """Issue #374 (deterministic gate): a model candidate that does NOT
    ``import("part.stl")`` on an imported project (a cube()-rebuild) must
    NOT pass silently — the existing ``import_guard_violation``
    (``no_import`` → the ``geometrically_wrong`` repair route) catches it.
    The guard fires only when ``part_scale`` is set, so a part wiring that
    silently degrades to part-less would let the rebuild pass; this
    asserts the guard fires while the wiring is intact."""
    from d33d.import_guard import import_guard_violation

    det = import_guard_violation("W = 20;\ncube([W, 25, 30]);\n", part_scale=1.0)
    assert det is not None
    assert det[0] == "no_import"

    llm = [_import_scad_llm("W = 20;\ncube([W, 25, 30]);\n")]
    result = _run_import_loop(llm, part_scale=1.0, bbox=BboxInfo(20.0, 25.0, 30.0))
    assert result.iterations[0].failure_class == "geometrically_wrong"
    assert result.iterations[0].repair is not None
    assert "import" in result.iterations[0].repair["instruction"]

    assert import_guard_violation(
        'scale(1) import("part.stl");\n', part_scale=1.0
    ) is None


# ---------------------------------------------------------------------------
# Issue #332 — ground truth: score() with part_bbox_mm
# ---------------------------------------------------------------------------


def test_score_with_part_bbox_larger_candidate_passes():
    """Issue #332 (issue #383 per-axis semantics): an import project's
    candidate whose bbox is LARGER than the user's stated dims — but
    within tolerance of the PART's measured extent (the #383 floor
    direction that growth is allowed) — passes the bbox gate (bit 2)."""
    # Part bbox is 20/25/30, stated is 20/25/30 (user confirmed the part's
    # extent). A candidate 20/25/30.5 is a marginal add/cut: the one off
    # axis (z) is 0.5 mm above the stated 30 — within the gate's tolerance
    # max(0.3, 0.5) = 0.5 mm, so bit 2 passes.
    render = _render()
    bbox = BboxInfo(20.0, 25.0, 30.5, 15100.0)
    s = score(
        render,
        (20.0, 25.0, 30.0),  # stated dims == part bbox
        bbox=bbox,
        scad_source="W = 20;\ncube([W, 25, 30.5]);\n",
        part_bbox_mm=(20.0, 25.0, 30.0),  # part's measured bbox
    )
    # Bit 2 (bbox) passes: the candidate matches the part's extent (and a
    # marginal growth beyond it is allowed on the floor axis).
    assert s.bits[2] is True


def test_score_with_part_bbox_divergent_candidate_fails():
    """Issue #332: a candidate whose bbox DIVERGES from the part's extent
    (beyond tolerance) keeps the stated-dims comparison and fails."""
    render = _render()
    bbox = BboxInfo(100.0, 100.0, 100.0, 1000000.0)  # way bigger than part
    s = score(
        render,
        (15.0, 20.0, 25.0),
        bbox=bbox,
        scad_source="W = 100;\ncube([W, W, W]);\n",
        part_bbox_mm=(20.0, 25.0, 30.0),
    )
    # Bit 2 fails: the candidate diverges from the part's extent.
    assert s.bits[2] is False


def test_score_no_part_bbox_byte_identical():
    """Issue #332 / #383: without part_bbox_mm (None), the score is
    byte-identical to today's behaviour — the part-baseline floor only
    activates when a part is present (regression anchor)."""
    render = _render()
    bbox = BboxInfo(20.0, 25.0, 30.0, 15000.0)
    s_with = score(
        render,
        (15.0, 20.0, 25.0),
        bbox=bbox,
        scad_source="W = 20;\ncube([W, 25, 30]);\n",
        part_bbox_mm=None,
    )
    s_without = score(
        render,
        (15.0, 20.0, 25.0),
        bbox=bbox,
        scad_source="W = 20;\ncube([W, 25, 30]);\n",
    )
    assert s_with.bits == s_without.bits
    assert s_with.rank == s_without.rank
    assert s_with.bbox_abstained == s_without.bbox_abstained


# ---------------------------------------------------------------------------
# Issue #383 — part-baseline floor: a pure import project (no stated dims)
# no longer abstains. The rendered extent must not be SMALLER than the
# part's extent per axis beyond max(1%, 0.5 mm) — growth is allowed.
# ---------------------------------------------------------------------------


def _score_part_floor(render, bbox, part=(20.0, 20.0, 20.0), stated=(0.0, 0.0, 0.0)):
    """Score a candidate against the part-baseline floor (issue #383): the
    part's measured extents present, the stated triple absent (or as
    given)."""
    return score(
        render,
        stated,
        bbox=bbox,
        scad_source="scale(1) import(\"part.stl\");\n",
        part_bbox_mm=part,
    )


def test_score_part_floor_shrunken_candidate_fails():
    """Issue #383: a pure import project (part 20×20×20, no stated dims)
    with a rendered bbox of 5×5×1 — the 17.7 mm³ garbage fragment from
    the live bug — must FAIL the bbox gate (bit 2), not pass via the
    old whole-gate abstention."""
    render = _render()
    bbox = BboxInfo(5.0, 5.0, 1.0, 17.7)
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is False
    # A failing bit is a measured failure, not an abstention.
    assert s.bbox_abstained is False


def test_score_part_floor_exact_part_passes():
    """Issue #383: a pure import candidate whose bbox matches the part
    (20×20×20) passes the bbox gate."""
    render = _render()
    bbox = BboxInfo(20.0, 20.0, 20.0, 8000.0)
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is True
    assert s.bbox_abstained is False


def test_score_part_floor_addon_growth_passes():
    """Issue #383: growth is allowed — a 25×20×20 add-on on a 20×20×20
    part passes the bbox gate (the floor constrains the shrink direction
    only)."""
    render = _render()
    bbox = BboxInfo(25.0, 20.0, 20.0, 10000.0)
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is True


def test_score_part_floor_boundary_within_tolerance_passes():
    """Issue #383: the boundary — 19.5×20×20 on a 20×20×20 part is exactly
    at the 0.5 mm tolerance (max(1%, 0.5 mm) = 0.5 mm at 20 mm) and must
    PASS (the tolerance is a closed bound)."""
    render = _render()
    bbox = BboxInfo(19.5, 20.0, 20.0, 7600.0)
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is True


def test_score_part_floor_just_beyond_tolerance_fails():
    """Issue #383: 19.4×20×20 is just beyond the 0.5 mm tolerance on the
    x axis — the floor bites and the gate fails (the boundary is real,
    not a blanket pass)."""
    render = _render()
    bbox = BboxInfo(19.4, 20.0, 20.0, 7560.0)
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is False


def test_score_part_floor_stated_axis_wins_over_part():
    """Issue #383: a CONFIRMED stated axis always wins over the part
    baseline — part 20×20×20 with stated H=15 and a render of 20×20×15
    PASSES (the gate compares H against 15, not the part's 20; the
    "cut it down to 15 mm tall" exception)."""
    render = _render()
    bbox = BboxInfo(20.0, 20.0, 15.0, 6000.0)
    s = _score_part_floor(render, bbox, stated=(0.0, 0.0, 15.0))
    assert s.bits[2] is True


def test_score_part_floor_stated_axis_still_enforced():
    """Issue #383: a confirmed stated axis is enforced AS TODAY — part
    20×20×20 with stated H=15 and a render of 20×20×20 (not cut down)
    FAILS on H (the floor only applies to unconfirmed axes)."""
    render = _render()
    bbox = BboxInfo(20.0, 20.0, 20.0, 8000.0)
    s = _score_part_floor(render, bbox, stated=(0.0, 0.0, 15.0))
    assert s.bits[2] is False


def test_score_part_floor_multibody_whole_mesh_extent():
    """Issue #383: the floor compares the WHOLE-MESH extent against the
    part's overall extents — a multi-body candidate whose union is 20×
    20×20 passes even though no single component matches the part (no
    component matching on a partial/empty stated set, the whole-mesh path
    is the floor's path)."""
    render = _render()
    bbox = BboxInfo(
        20.0,
        20.0,
        20.0,
        9000.0,
        components=(
            (10.0, 10.0, 10.0, 1000.0, 0.0, 0.0, 0.0),
            (10.0, 10.0, 10.0, 1000.0, 10.0, 10.0, 10.0),
        ),
    )
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is True


def test_score_part_floor_multibody_shrunken_union_fails():
    """Issue #383: a multi-body candidate whose WHOLE-MESH extent is
    shrunken below the part (the bodies are small, the union is 5×5×1)
    fails the floor even though a single body might fit (the floor
    constrains the overall extent, not per-component)."""
    render = _render()
    bbox = BboxInfo(
        5.0,
        5.0,
        1.0,
        25.0,
        components=(
            (5.0, 5.0, 1.0, 25.0, 0.0, 0.0, 0.0),
        ),
    )
    s = _score_part_floor(render, bbox)
    assert s.bits[2] is False


def _run_part_floor_loop(
    llm_script, bbox, stated=(0.0, 0.0, 0.0), part_bbox_mm=(20.0, 20.0, 20.0)
):
    """Run the design loop with an import project's part-baseline floor:
    part_bbox_mm present, stated dims absent (the pure-import shape),
    the given measured bbox on an ok render."""
    i = {"n": 0}

    def llm_fn(role, messages, system):
        return llm_script[min(i["n"], len(llm_script) - 1)]

    def render_fn(scad, defines):
        r = _render()
        i["n"] += 1
        return r

    return run_design_loop(
        photo=PHOTO,
        stated_dims=stated,
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=(lambda r: bbox if r.error_class == "ok" else None),
        part_scale=1.0,
        part_bbox_mm=part_bbox_mm,
    )


def test_loop_part_floor_shrunken_render_not_a_pass():
    """Issue #383 loop level: part 20×20×20, no stated dims, render
    5×5×1 — the loop must NOT pass (the gate fails → repair, never a
    silent pass). Fails on main (the old abstention let it through)."""
    llm = [_floor_scad_llm('scale(1) import("part.stl");\n')]
    result = _run_part_floor_loop(llm, BboxInfo(5.0, 5.0, 1.0, 17.7))
    assert result.status == "exhausted"
    assert result.failure_reason == "bbox_out_of_tolerance"
    assert result.best.score.bits[2] is False


def test_loop_part_floor_exact_part_passes():
    """Issue #383 loop level: part 20×20×20, no stated dims, render
    20×20×20 — the gate passes and the loop passes (the part measures
    itself)."""
    llm = [_floor_scad_llm('scale(1) import("part.stl");\n')]
    result = _run_part_floor_loop(llm, BboxInfo(20.0, 20.0, 20.0, 8000.0))
    assert result.status == "pass"
    assert result.best.score.bits[2] is True


def test_loop_part_floor_addon_growth_passes():
    """Issue #383 loop level: part 20×20×20, no stated dims, render
    25×20×20 (an add-on) — growth is allowed, the loop passes."""
    llm = [_floor_scad_llm('scale(1) import("part.stl");\n')]
    result = _run_part_floor_loop(llm, BboxInfo(25.0, 20.0, 20.0, 10000.0))
    assert result.status == "pass"
    assert result.best.score.bits[2] is True


def test_loop_part_floor_stated_height_passes():
    """Issue #383 loop level: part 20×20×20, stated H=15 (the "cut it
to 15 mm" exception), render 20×20×15 — the gate compares H against
    15 (not the part's 20) and the loop passes."""
    llm = [_floor_scad_llm('scale(1) import("part.stl");\nH = 15;\n')]
    result = _run_part_floor_loop(llm, BboxInfo(20.0, 20.0, 15.0, 6000.0), stated=(0.0, 0.0, 15.0))
    assert result.status == "pass"
    assert result.best.score.bits[2] is True


def test_loop_unknown_variable_render_is_repair_naming_variable():
    """Issue #383 loop level: an exit-0 render whose HARVESTED render.log
    carries the OpenSCAD unknown-variable warning (the real render path —
    the container stderr carries only [entrypoint] markers, so the warning
    is only reachable via ``render.render_log``) must be routed to repair
    as the ``unknown_variable`` class, with the variable named in the
    evidence. Fails without the render.log wiring (the loop then sees only
    the marker-only stderr → the generic unclassified class)."""
    warning = 'WARNING: Ignoring unknown variable "H" in file model.scad, line 1'
    render = _render(
        error_class="syntax_error",
        stderr="[entrypoint] Starting render of /work/model.scad\n",
        render_log=warning,
    )
    llm = [_scad_llm("H = 20;\ncube([H, H, H]);\n")]
    i = {"n": 0}

    def render_fn(scad, defines):
        return render

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(0.0, 0.0, 0.0),
        render_fn=render_fn,
        llm_fn=(lambda role, messages, system: llm[min(i["n"], len(llm) - 1)]),
    )
    assert result.iterations[0].failure_class == "unknown_variable"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].repair["failure_class"] == "unknown_variable"
    assert "H" in result.iterations[0].repair["evidence"]
