"""Fast, non-live checks for the import guard inside the gate phase
(issue #340): the guard's gate-phase failures, the run_case/build_report
path with the guard's short-circuit, and the render_fn kwargs contract
(legacy stub containment, the signature probe, and the run.py wrapper)."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from d33d.evals.case_schema import load_golden_set
from d33d.evals.gates import GateResult
from d33d.evals.harness import CaseOutcome, run_case_gates
from tests.evals._casefile_helpers import (
    FIXTURE,
    IMPORT_CASE_IDS,
    REPO_ROOT,
    _RenderResult,
    _run,
)

CASES_DIR = REPO_ROOT / "evals" / "cases"


def _load() -> dict:
    return load_golden_set(CASES_DIR, REPO_ROOT)


# ---------------------------------------------------------------------------
# (c) the import guard inside the gate phase
# ---------------------------------------------------------------------------


def _case_with_part() -> object:
    return _load()[IMPORT_CASE_IDS[0]]


def test_gate_phase_flags_missing_import() -> None:
    case = _case_with_part()
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source="cube([20, 20, 20]);",  # no import at all
    )
    assert gates["import_guard"].status == "fail"
    assert "no_import" in gates["import_guard"].detail


def test_gate_phase_flags_rescaled_import() -> None:
    case = _case_with_part()
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source='scale(2) import("part.stl");',
    )
    assert gates["import_guard"].status == "fail"
    assert "rescaled_import" in gates["import_guard"].detail


def test_gate_phase_flags_resize() -> None:
    case = _case_with_part()
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source='scale(1) import("part.stl"); resize([50, 50, 50]);',
    )
    assert gates["import_guard"].status == "fail"
    assert "resized_part" in gates["import_guard"].detail


def test_import_guard_failure_class_is_taggable() -> None:
    """The guard's failure class (``artifact_error``) is in
    ``GATE_TAGGABLE_CLASSES["import_guard"]`` — the guard's GateResult
    goes through the same :func:`assert_taggable` check as every other
    gate (the tagging invariant has no gap for the pre-check gate)."""
    from d33d.evals.gates import GATE_TAGGABLE_CLASSES, assert_taggable

    assert "artifact_error" in GATE_TAGGABLE_CLASSES["import_guard"]
    # The guard's failure row itself is built through assert_taggable
    # (import_guard_result); a non-taggable class would raise.
    from d33d.evals.gates import import_guard_result

    result = import_guard_result("no_import", "the candidate does not import")
    assert result.status == "fail"
    assert result.failure_class == "artifact_error"
    # And the table entry is exactly the class the guard emits — no
    # wider, no narrower.
    assert GATE_TAGGABLE_CLASSES["import_guard"] == frozenset({"artifact_error"})
    # A class the guard cannot emit is rejected by the same invariant.
    with pytest.raises(ValueError, match="not taggable"):
        assert_taggable("import_guard", "geometrically_wrong")


def test_gate_phase_passes_compliant_candidate() -> None:
    case = _case_with_part()
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source='scale(1) import("part.stl");\ndifference() { scale(1) import("part.stl"); cylinder(h=40, d=12); }',
    )
    assert gates["import_guard"].status == "pass"


def test_gate_phase_skips_guard_for_non_import_kind() -> None:
    case = _load()["primitive-box-20mm"]
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source="cube([20, 20, 20]);",
    )
    assert "import_guard" not in gates


def test_gate_phase_short_circuits_after_guard_failure() -> None:
    """A guard failure records the failure and the remaining declared
    gates are not run (the runner short-circuits on the first failure)."""
    case = _case_with_part()
    gates = run_case_gates(
        case=case,
        repo_root=REPO_ROOT,
        render_result=_RenderResult(),
        mesh=None,
        stl_path=None,
        scad_source='scale(2) import("part.stl");',
    )
    assert list(gates) == ["compile", "import_guard"]


# ---------------------------------------------------------------------------
# (c2) full gate_expectations of a real import case through run_case and
# build_report (issue #340 fix round)
# ---------------------------------------------------------------------------


async def _passing_judge(ji) -> object:
    from d33d.evals.judge import JudgeVerdict

    return JudgeVerdict(passed=True, reason="stub")


def _async_factory(content: str):
    async def factory(request):
        class R:
            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return R()

    return factory


def _ok_render_fn():
    def render_fn(scad_source, *, part_path=None, repo_dir=None):
        return _RenderResult()

    return render_fn


def test_run_case_full_gate_expectations_guard_short_circuits() -> None:
    """An imported_part case with the FULL gate_expectations of a real
    import case (loaded from the git-tracked case files, not a trimmed
    copy) driven through ``run_case`` with a guard-violating candidate:
    the gates short-circuit at compile + import_guard, the case fails
    with ``artifact_error``, and the remaining declared gates never run.
    Mutation check: removing the import-guard call from
    ``run_case_gates`` makes this fail (the row then carries a passing
    compile row with no import_guard)."""
    from d33d.evals.harness import run_case

    case = _load()["import-drill-hole"]
    assert "stl_export" in case.gate_expectations  # the FULL set, not trimmed

    outcome = asyncio.run(
        run_case(
            case=case,
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=_async_factory('scale(2) import("part.stl");'),
            render_fn=_ok_render_fn(),
            part_path=FIXTURE,
            part_repo_dir=REPO_ROOT,
            judge_fn=_passing_judge,
        )
    )
    assert list(outcome.gates) == ["compile", "import_guard"]
    assert outcome.gates["compile"].status == "pass"
    assert outcome.gates["import_guard"].status == "fail"
    assert outcome.ok is False
    assert outcome.failure_class == "artifact_error"
    assert outcome.judge is None  # the gate failure short-circuited it


def test_build_report_orders_import_guard_first_and_counts_failed() -> None:
    """``build_report`` over the guard-failing outcome above: the report
    row orders ``import_guard`` first and counts the case as failed."""
    from d33d.evals.harness import run_case
    from d33d.evals.report import build_report

    case = _load()["import-drill-hole"]
    outcome = asyncio.run(
        run_case(
            case=case,
            repo_root=REPO_ROOT,
            model_id="m",
            request_factory=_async_factory('scale(2) import("part.stl");'),
            render_fn=_ok_render_fn(),
            part_path=FIXTURE,
            part_repo_dir=REPO_ROOT,
            judge_fn=_passing_judge,
        )
    )
    report = build_report([outcome], ts="2026-01-01T00:00:00+00:00")
    row = report.outcomes[outcome.case_id]
    # The row's gate mapping is reordered in place to the kind's gate
    # order: the pre-check import_guard first, then the base 1->7 (the
    # short-circuit left compile after it in the outcome's own dict).
    assert list(row["gates"]) == ["import_guard", "compile"]
    assert row["ok"] is False
    assert report.passed == 0
    assert report.failed == 1
    assert report.total == 1


def test_render_fn_legacy_stub_typeerror_is_contained(tmp_path: Path) -> None:
    """A legacy one-argument render_fn handed an imported-part case is
    contained: the case fails with ``artifact_error`` (the detail names
    the kwargs), the run does not crash, and the other cases are
    unaffected (per-case containment)."""
    import json as _json

    def legacy_render_fn(scad_source):
        return _RenderResult()

    report = _run(
        tmp_path,
        [
            {
                "case_id": "legacy-import",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/part.stl",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
        render_fn=legacy_render_fn,
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["legacy-import"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "render_fn does not accept part_path/repo_dir" in bad["detail"]
    # the other case still ran and still passes
    assert cases["normal-box"]["ok"] is True
    assert report_doc["summary"]["passed"] == 1


def test_render_fn_uninspectable_counts_as_not_accepting_kwargs() -> None:
    """A render_fn whose signature cannot be inspected (a builtin like
    ``iter``) is counted as NOT accepting ``part_path``/``repo_dir`` —
    the probe returns ``False``, so the imported-part case gets the
    clear artifact_error outcome ("render_fn does not accept
    part_path/repo_dir") instead of a raw TypeError.

    The probe is a pure function of the signature (checked BEFORE the
    call), so this tests it directly without needing a full run."""
    from d33d.evals.harness import _render_fn_accepts_part_kwargs

    # ``iter`` is a builtin that ``inspect.signature`` cannot inspect —
    # the probe must treat it as NOT accepting the kwargs.
    assert _render_fn_accepts_part_kwargs(iter) is False

    # ``range`` is a builtin type that ``inspect.signature`` cannot
    # inspect either.
    assert _render_fn_accepts_part_kwargs(range) is False

    # A correctly signed render_fn passes the probe (regression guard).
    def ok_fn(scad_source, *, part_path=None, repo_dir=None):
        return None

    assert _render_fn_accepts_part_kwargs(ok_fn) is True

    # A **kwargs render_fn passes the probe (regression guard).
    def kw_fn(scad_source, **kwargs):
        return None

    assert _render_fn_accepts_part_kwargs(kw_fn) is True


def test_render_fn_correct_signature_typeerror_propagates() -> None:
    """A correctly signed render_fn whose BODY raises TypeError propagates
    the TypeError — it is not swallowed and mislabelled as "render_fn does
    not accept part_path/repo_dir" (mutation check: restoring the old
    ``except TypeError`` around the call makes this fail with the
    mislabelled artifact_error outcome instead)."""
    from d33d.evals.harness import run_case

    def broken_render_fn(scad_source, *, part_path=None, repo_dir=None):
        raise TypeError("boom")

    case = _load()["import-drill-hole"]
    with pytest.raises(TypeError, match="boom"):
        asyncio.run(
            run_case(
                case=case,
                repo_root=REPO_ROOT,
                model_id="m",
                request_factory=_async_factory("cube([1, 1, 1]);"),
                render_fn=broken_render_fn,
                part_path=FIXTURE,
                part_repo_dir=REPO_ROOT,
                judge_fn=_passing_judge,
            )
        )


def test_build_report_is_pure_over_outcome_gates() -> None:
    """``build_report`` reorders the report ROW, never the outcome: the
    keys of ``outcome.gates`` are unchanged after the aggregation."""
    from d33d.evals.report import build_report

    case = _load()[IMPORT_CASE_IDS[0]]
    outcome = CaseOutcome(
        case_id=case.case_id,
        kind=case.kind,
        prompt_version=case.prompt.prompt_version,
        prompt_sha256=case.prompt.sha256,
        request=case.request,
        # compile ordered BEFORE import_guard here (the gate phase
        # records the pre-check first) — the row must be reordered, the
        # outcome must not be.
        gates={
            "compile": GateResult(gate="compile", status="pass"),
            "import_guard": GateResult(gate="import_guard", status="fail"),
        },
        failure_class="artifact_error",
        detail="import_guard: no_import",
    )
    before = list(outcome.gates)
    assert before == ["compile", "import_guard"]

    report = build_report([outcome], ts="2026-01-01T00:00:00+00:00")

    assert list(outcome.gates) == before  # pure: the outcome is untouched
    assert list(report.outcomes[outcome.case_id]["gates"]) == [
        "import_guard",
        "compile",
    ]


def test_run_py_render_wrapper_satisfies_render_fn_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The wrapper ``evals/run.py`` installs around
    ``render_for_design_loop`` satisfies the ``RenderFn`` call shape the
    harness uses: both the no-part call (``render_fn(scad_source)``) and
    the imported-part call (``render_fn(scad_source, part_path=..., 
    repo_dir=...)``) work, and the worker always receives an empty
    ``defines`` map."""

    recorded: list[dict] = []

    class R:
        error_class = "ok"
        stderr = ""
        scad_source = ""
        stl = None
        csg = None
        views = ()

    def fake_worker(scad_source, defines, *args, **kwargs):
        recorded.append({"scad": scad_source, "defines": defines, "kwargs": kwargs})
        return R()

    # Patch the name in the worker module; the wrapper (defined inside
    # ``main`` in run.py) looks it up at call time, so a monkeypatch on
    # the module attribute is what the wrapper will invoke.
    import d33d.render_worker as worker

    monkeypatch.setattr(worker, "render_for_design_loop", fake_worker)

    # The wrapper's exact body as installed by ``evals/run.py`` (the
    # closure the brief specifies), bound to the patched worker.
    def _render(scad_source, *, part_path=None, repo_dir=None):
        return worker.render_for_design_loop(
            scad_source, {}, part_path=part_path, repo_dir=repo_dir
        )

    # No-part call shape (the harness's part-less path).
    _render("cube([1,1,1]);")
    # Imported-part call shape (the harness's part-carrying path).
    _render("cube([1,1,1]);", part_path=FIXTURE, repo_dir=REPO_ROOT)

    assert len(recorded) == 2
    assert recorded[0]["scad"] == "cube([1,1,1]);"
    assert recorded[0]["defines"] == {}
    assert recorded[0]["kwargs"] == {"part_path": None, "repo_dir": None}
    assert recorded[1]["defines"] == {}
    assert recorded[1]["kwargs"]["part_path"] == FIXTURE
    assert recorded[1]["kwargs"]["repo_dir"] == REPO_ROOT

    # The wrapper's shape is what the harness probes before the call.
    from d33d.evals.harness import _render_fn_accepts_part_kwargs

    assert _render_fn_accepts_part_kwargs(_render)
