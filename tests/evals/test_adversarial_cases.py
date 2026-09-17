"""The harness enforces the design-call -> real render -> gates -> judge
ordering and the report aggregates correctly (issue #9, workstream
task-harness; re-ordered for issue #108).

Covers (all mocked — no real LLM call, no real Docker):
- the design call runs FIRST (the model's output is what is measured)
- a render failure (gate 1) short-circuits the gates and the judge
  (the design call HAS already run — it produced the output the render
  rejected)
- a gate failure (e.g. watertight) short-circuits the judge
- a gate-passing case reaches the judge (the judge runs only when the
  gates let the model's rendered output through)
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
    """RenderResult-shaped stand-in (``render_fn``'s return)."""

    def __init__(
        self, error_class: str = "ok", stderr: str = "", stl: str | None = None
    ) -> None:
        self.error_class = error_class
        self.stderr = stderr
        self.scad_source = ""
        self.stl = stl


def _mesh_stl(tmp: Path) -> str:
    """A real 20mm-box STL on disk (the injected render_fn points its
    ``stl`` here so the harness loads a real mesh)."""
    import trimesh

    path = tmp / "box20.stl"
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(str(path))
    return str(path)


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
                                "content": _json.dumps(
                                    {"pass": pass_, "reason": reason}
                                ),
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


def _render_fn(tmp: Path, error_class: str = "ok", stl: str | None = None) -> Any:
    """A render_fn that records the scad source it was handed and
    returns a fixed RenderResult-shaped object (the seam stand-in for
    the Docker worker)."""
    handed: list[str] = []

    def fn(scad_source: str) -> _Render:
        handed.append(scad_source)
        return _Render(error_class=error_class, stl=stl)

    fn.handed = handed  # type: ignore[attr-defined]
    return fn  # type: ignore[attr-defined]


def _run(coro: Any) -> Any:
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# The re-ordered pipeline (issue #108): design call first, then the
# render, then the gates on the model's output, then the judge
# ---------------------------------------------------------------------------


def test_design_call_runs_before_render_and_gates(tmp_path: Path) -> None:
    """The design call's OUTPUT is what the render_fn receives — the
    gates measure the model's output, never a reference fixture."""
    design = _design_factory(content="cube([20,20,20]);")
    render = _render_fn(tmp_path)

    _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=design,
            render_fn=render,
            judge_fn=_passing_judge,
        )
    )
    # The design call happened exactly once...
    assert len(design.calls) == 1
    # ...and the render received the design call's output.
    assert render.handed == ["cube([20,20,20]);"]


def test_render_failure_short_circuits_gates_and_judge(tmp_path: Path) -> None:
    """A render that fails (``timeout``) short-circuits: the gates report
    the failure, the judge never runs. The design call HAS already run —
    it produced the output the render rejected (issue #108 ordering)."""
    design = _design_factory()
    render = _render_fn(tmp_path, error_class="timeout")
    judge_calls: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        judge_calls.append(1)
        return JudgeVerdict(passed=True, reason="should not run")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=design,
            render_fn=render,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates_ok is False
    assert outcome.gates["compile"].status == "fail"
    assert outcome.judge is None
    assert outcome.failure_class == "timeout"
    # The design call ran (it produced the rejected output); the judge did not.
    assert len(design.calls) == 1
    assert len(judge_calls) == 0


def test_gate2_failure_short_circuits_later_gates_and_judge(tmp_path: Path) -> None:
    """A render that yields no STL fails gate 2 (``stl_export``):
    the watertight gate and the judge never run (the design call and the
    render already ran — the ordering is design -> render -> gates)."""
    design = _design_factory()
    render = _render_fn(tmp_path, stl=None)  # no STL -> no mesh -> gate 3 fails
    calls: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        calls.append(1)
        return JudgeVerdict(passed=True, reason="no")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=design,
            render_fn=render,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates["stl_export"].status == "fail"
    assert "watertight_winding" not in outcome.gates  # short-circuited
    assert outcome.judge is None
    assert len(calls) == 0


def test_gate_passing_reaches_the_judge(tmp_path: Path) -> None:
    """A case whose rendered output passes every declared gate reaches
    the judge (the judge runs only after the gate phase passes)."""
    design = _design_factory()
    render = _render_fn(tmp_path, stl=_mesh_stl(tmp_path))
    called: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        called.append(1)
        return JudgeVerdict(passed=True, reason="matches")

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=design,
            render_fn=render,
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


async def _passing_judge(ji: Any) -> JudgeVerdict:
    return JudgeVerdict(passed=True, reason="matches")


def test_judge_fail_fails_the_case_with_judges_class(tmp_path: Path) -> None:
    """A gate-passing candidate the judge fails gets the judge's
    ``failure_class`` (``geometrically_wrong`` for a non-adversarial
    case)."""
    design = _design_factory()
    render = _render_fn(tmp_path, stl=_mesh_stl(tmp_path))
    called: list[int] = []

    async def judge_fn(ji: Any) -> JudgeVerdict:
        called.append(1)
        return JudgeVerdict(
            passed=False, reason="hole too small", failure_class="geometrically_wrong"
        )

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=design,
            render_fn=render,
            judge_fn=judge_fn,
        )
    )
    assert outcome.gates_ok is True
    assert outcome.ok is False
    assert outcome.failure_class == "geometrically_wrong"


def test_design_call_failure_maps_to_container_error(tmp_path: Path) -> None:
    """A design call whose response has no ``.json()`` maps to
    ``container_error`` (the failure is the call itself). The render and
    the gates never run — there is no model output to measure."""
    called: list[int] = []

    async def no_json_factory(request: dict[str, Any]) -> Any:
        called.append(1)
        return _NoJsonResponse()

    render = _render_fn(tmp_path)

    outcome = _run(
        run_case(
            case=_case(),
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=no_json_factory,
            render_fn=render,
            judge_fn=_passing_judge,
        )
    )
    assert outcome.failure_class == "container_error"
    assert outcome.judge is None  # the judge never ran
    assert outcome.detail.startswith("design call failed")
    assert len(called) == 1
    # The render never ran — the design call failed first.
    assert render.handed == []


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
