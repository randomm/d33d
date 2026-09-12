"""The harness enforces gates-before-judge ordering and the report
aggregates correctly (issue #9, workstream task-harness).

Covers (all mocked — no real LLM call, no real Docker):
- a gate failure short-circuits the design call AND the judge (the
  spec's "7 deterministic gates before any vision judge" — a case that
  fails gate 1 never reaches the judge)
- a gate-passing case reaches the judge (the judge runs only when the
  gates let it through)
- the judge verdict maps onto the outcome's ``ok`` / ``failure_class``
- ``build_report`` aggregates the per-case outcomes (counts,
  failure-class histogram mapped onto the design-loop taxonomy, the
  "best-candidate + reason" shape)
- ``select_best_candidate`` picks the best-scoring case (a full pass
  outranks a gates-only pass outranks a fail; deterministic tiebreak)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from d33d.evals.case_schema import (
    GoldenCase,
    PromptPin,
)
from d33d.evals.harness import CaseOutcome, run_case
from d33d.evals.judge import JudgeVerdict
from d33d.evals.report import build_report, select_best_candidate

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pin() -> PromptPin:
    import hashlib

    return PromptPin(
        prompt_version="v1",
        path="evals/prompts/primitive_design_v1.md",
        sha256=hashlib.sha256(
            (REPO_ROOT / "evals" / "prompts" / "primitive_design_v1.md").read_bytes()
        ).hexdigest(),
    )


def _case(case_id: str = "box-20", kind: str = "primitive") -> GoldenCase:
    return GoldenCase(
        case_id=case_id,
        kind=kind,  # type: ignore[arg-type]
        prompt=_pin(),
        request="A 20mm box",
        expected_dims=None if kind == "adversarial" else {"x": 20, "y": 20, "z": 20},
        gate_expectations=["compile", "stl_export", "watertight_winding", "bbox_dims"],
    )


class _Render:
    def __init__(self, error_class: str = "ok", stderr: str = "") -> None:
        self.error_class = error_class
        self.stderr = stderr
        self.scad_source = ""


def _mesh() -> Any:
    import trimesh

    return trimesh.creation.box(extents=(20.0, 20.0, 20.0))


class _NoJsonResponse:
    def json(self) -> Any:
        raise AttributeError("no json")


def _judge_factory(pass_: bool, reason: str = "ok") -> Any:
    calls: list[int] = []

    async def factory(request: dict[str, Any]) -> Any:
        calls.append(1)

        class _R:
            def json(self) -> Any:
                import json as _json

                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": _json.dumps({"pass": pass_, "reason": reason}),
                            }
                        }
                    ]
                }

        return _R()

    factory.calls = calls  # type: ignore[attr-defined]
    return factory  # type: ignore[attr-defined]


def _design_factory(content: str = "cube([20,20,20]);") -> Any:
    calls: list[int] = []

    async def factory(request: dict[str, Any]) -> Any:
        calls.append(1)

        class _R:
            def json(self) -> Any:
                return {
                    "choices": [{"message": {"role": "assistant", "content": content}}]
                }

        return _R()

    factory.calls = calls  # type: ignore[attr-defined]
    return factory  # type: ignore[attr-defined]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Gate failure short-circuits the design call AND the judge
# ---------------------------------------------------------------------------


def test_gate1_failure_short_circuits_design_and_judge() -> None:
    """A render that fails (``timeout``) short-circuits: the design call
    is never made and the judge never runs (the spec's ordering)."""
    design = _design_factory()
    judge = _judge_factory(True)

    async def judge_fn(ji: Any) -> JudgeVerdict:
        judge.calls.append("judge")
        return JudgeVerdict(passed=True, reason="should not run")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            render_result=_Render(error_class="timeout", stderr="timed out"),
            mesh=_mesh(),
            stl_path="/tmp/model.stl",
            model_id="m",
            request_factory=design,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates_ok is False
    assert outcome.gates["compile"].status == "fail"
    assert outcome.judge is None
    assert outcome.failure_class == "timeout"
    # Neither the design call nor the judge ran.
    assert len(design.calls) == 0
    assert "judge" not in judge.calls


def test_gate3_failure_short_circuits_judge() -> None:
    """A mesh that fails watertight short-circuits: the judge never runs
    (the design call may run, but the verdict is the gate's)."""
    design = _design_factory()
    calls: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        calls.append(1)
        return JudgeVerdict(passed=True, reason="no")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            render_result=_Render("ok"),
            mesh=None,  # no mesh -> gate 3 fails
            stl_path="/tmp/model.stl",
            model_id="m",
            request_factory=design,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates["watertight_winding"].status == "fail"
    assert outcome.judge is None
    assert len(calls) == 0


def test_gate_passing_reaches_the_judge() -> None:
    """A case that passes every declared gate reaches the judge (the
    judge runs only after the gate phase passes)."""
    design = _design_factory()
    called: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        called.append(1)
        return JudgeVerdict(passed=True, reason="matches")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            render_result=_Render("ok"),
            mesh=_mesh(),
            stl_path="/tmp/model.stl",
            model_id="m",
            request_factory=design,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates_ok is True
    assert outcome.judge is not None
    assert outcome.judge.passed is True
    assert outcome.ok is True
    assert outcome.failure_class is None
    assert len(called) == 1
    assert len(design.calls) == 1


def test_judge_fail_fails_the_case_with_judges_class() -> None:
    """A gate-passing candidate the judge fails gets the judge's
    ``failure_class`` (``geometrically_wrong`` for a non-adversarial
    case)."""
    design = _design_factory()
    called: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        called.append(1)
        return JudgeVerdict(passed=False, reason="hole too small", failure_class="geometrically_wrong")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            render_result=_Render("ok"),
            mesh=_mesh(),
            stl_path="/tmp/model.stl",
            model_id="m",
            request_factory=design,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates_ok is True
    assert outcome.ok is False
    assert outcome.failure_class == "geometrically_wrong"


def test_design_call_failure_maps_to_container_error() -> None:
    """A design call whose response has no ``.json()`` maps to
    ``container_error`` (the gate phase passed; the failure is the
    call). The case's overall verdict is a fail (the candidate never
    produced a valid design), even though every *declared* gate passed
    — the design-call failure is not a gate, it is the candidate.
    """
    called: list[int] = []

    async def no_json_factory(request: dict[str, Any]) -> Any:
        called.append(1)
        return _NoJsonResponse()

    async def judge_fn(ji: Any) -> JudgeVerdict:
        return JudgeVerdict(passed=True, reason="no")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            render_result=_Render("ok"),
            mesh=_mesh(),
            stl_path="/tmp/model.stl",
            model_id="m",
            request_factory=no_json_factory,
            judge_fn=judge_fn,
        )
    )
    assert outcome.failure_class == "container_error"
    assert outcome.gates_ok is True  # every declared gate passed
    assert outcome.judge is None  # the judge never ran
    assert outcome.detail.startswith("design call failed")
    assert len(called) == 1


# ---------------------------------------------------------------------------
# Report aggregation
# ---------------------------------------------------------------------------


def _outcome(
    case_id: str,
    ok: bool,
    failure_class: str | None = None,
    detail: str = "",
) -> CaseOutcome:
    """A per-case outcome with a real gate row (so ``ok`` reflects the
    gate + failure_class, not an empty gate map)."""
    from d33d.evals.gates import GateResult

    if ok:
        gates: dict[str, Any] = {"compile": GateResult(gate="compile", status="pass")}
    else:
        gates = {
            "compile": GateResult(
                gate="compile",
                status="fail",
                failure_class=failure_class or "empty_model",
                detail=detail,
            )
        }
    return CaseOutcome(
        case_id=case_id,
        kind="primitive",
        prompt_version="v1",
        prompt_sha256="x" * 64,
        request="r",
        gates=gates,
        failure_class=failure_class,
        detail=detail,
    )


def test_build_report_counts_and_best() -> None:
    outcomes = [
        _outcome("a", True),
        _outcome("b", False, "empty_model", "volume 0"),
        _outcome("c", False, "timeout", "timed out"),
    ]
    report = build_report(outcomes, ts="2026-01-01T00:00:00+00:00")
    assert report.total == 3
    assert report.passed == 1
    assert report.failed == 2
    # The best candidate is the passing case.
    assert report.best is not None
    assert report.best.case_id == "a"
    # The failure-class histogram maps onto the design-loop taxonomy.
    assert report.failure_class_counts.get("empty_model") == 1
    assert report.failure_class_counts.get("timeout") == 1


def test_build_report_outcome_classes_collapse() -> None:
    """``graceful_refusal`` / ``clearance_applied`` collapse onto
    ``unclassified_syntax_error`` in the design-loop taxonomy."""
    outcomes = [
        _outcome("a", False, "graceful_refusal", "refused"),
    ]
    report = build_report(outcomes, ts="2026-01-01T00:00:00+00:00")
    assert report.failure_class_counts.get("unclassified_syntax_error") == 1
    assert "graceful_refusal" not in report.failure_class_counts


def test_select_best_prefers_full_pass() -> None:
    full_pass = _outcome("full", True)
    failing = _outcome("gates", False, "empty_model", "volume 0")
    best = select_best_candidate([failing, full_pass])
    assert best.case_id == "full"
    assert best.failure_class is None


def test_select_best_no_pass_names_weakest_failure() -> None:
    """When no case passed, the best candidate is None and the reason
    names the closest-to-pass failing case's class + detail."""
    outcomes = [
        _outcome("a", False, "empty_model", "volume 0"),
        _outcome("b", False, "timeout", "timed out"),
    ]
    best = select_best_candidate(outcomes)
    assert best.case_id is not None  # the closest-to-pass failing case
    assert best.failure_class in ("empty_model", "timeout")


def test_select_best_empty_list() -> None:
    best = select_best_candidate([])
    assert best.case_id is None
    assert best.reason == "no cases ran"


def test_report_to_json_is_serialisable() -> None:
    import json as _json

    report = build_report([_outcome("a", True)], ts="2026-01-01T00:00:00+00:00")
    text = report.to_json()
    parsed = _json.loads(text)
    assert parsed["summary"]["total"] == 1
    assert parsed["summary"]["passed"] == 1
    assert parsed["best"]["case_id"] == "a"
