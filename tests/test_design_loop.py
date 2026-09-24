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
import os
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from d33d.config.catalogue import load_catalogue
from d33d.config.probes import CapabilityResult
from d33d.design_llm import LLMResult, send
from d33d.design_loop import (
    GATE_REASON_BITS,
    MAX_ITERATIONS,
    MAX_SCAD_SOURCE_BYTES,
    MAX_SCAD_VALIDATION_CHARS,
    NO_IMPROVEMENT_LIMIT,
    BboxInfo,
    DesignResult,
    Score,
    _scad_from_result,
    extract_named_params,
    is_best,
    make_llm_fn,
    no_improvement,
    run_design_loop,
    run_design_loop_async,
    scad_looks_valid,
    score,
)
from d33d.failure_classes import (
    FAILURE_CLASSES,
    classify_failure,
    route_repair,
)
from d33d.prompt_hash import canonical_hash
from d33d.render_worker import RenderResult

PHOTO = "data:image/png;base64,REF"
STATED = (20.0, 25.0, 30.0)

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
    assert good.bits == (True, True, True, True)
    assert good.rank == 4
    assert good.perfect

    bad = score(
        _render(error_class="syntax_error", stderr="ERROR: syntax"),
        STATED,
        scad_source=GOOD_SCAD,
    )
    # The failed render still reports the six view filenames and the scad
    # still carries the named-parameter block — only the ok/ok-bbox gates
    # fail on a syntax error (the metric is a pure function of its inputs).
    assert bad.rank == 2
    assert bad.bits == (False, True, False, True)


def test_no_improvement_is_named_predicate_on_rank():
    low = Score(
        bits=(False, False, False, False), rank=0, tiebreak=(False, False, False, False)
    )
    high = Score(
        bits=(True, False, False, False), rank=1, tiebreak=(True, False, False, False)
    )
    assert no_improvement(high, low) is True  # non-increase
    assert no_improvement(low, high) is False  # increase
    assert no_improvement(low, low) is True  # equal rank = no improvement


def test_is_best_rank_then_deterministic_tiebreak():
    a = Score(
        bits=(True, True, False, False), rank=2, tiebreak=(True, True, False, False)
    )
    b = Score(
        bits=(True, False, True, False), rank=2, tiebreak=(True, False, True, False)
    )
    c = Score(
        bits=(False, False, False, False), rank=0, tiebreak=(False, False, False, False)
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
    assert result.failure_reason == GATE_REASON_BITS[0]  # error_class_not_ok


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
    # Declining rank trajectory: 3 (ok, views, params) → 1 (params only) → 1.
    assert [r.score.rank for r in result.iterations] == [3, 1, 1]
    # Two consecutive non-improvements fired → early stop at iteration 3.
    assert result.iterations_used == NO_IMPROVEMENT_LIMIT + 1 == 3
    # The best is iteration 1 (rank 3), NOT the last attempt (rank 1).
    assert result.best.iteration == 1
    assert result.best.score.rank == 3


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
            return _FakeResponse({"choices": [{"message": {"tool_calls": [tool_call]}}]})

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
    # (structural equality with the DESIGN_TOOLS test constant).
    assert sent_design[0]["tools"] == [
        {
            "type": "function",
            "function": {
                "name": "emit_design",
                "description": "Emit the parametric OpenSCAD",
                "parameters": {
                    "type": "object",
                    "properties": {"scad": {"type": "string"}},
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
        catalogue, {"design": factory, "critique": factory}, capabilities={"design": t0, "critique": t0}
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
        "properties": {"scad": {"type": "string"}},
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

    llm = make_llm_fn(catalogue, {"critique": crit_factory}, capabilities={"critique": t0})
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
                            "content": _t1_tool_call_payload("emit_design", {"scad": GOOD_SCAD})
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
    total = MAX_SCAD_SOURCE_BYTES - len(header.encode("utf-8")) - len(body.encode("utf-8"))
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
        tool_calls=(
            {"name": "emit_design", "arguments": {"scad": prose.rstrip()}},
        ),
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
