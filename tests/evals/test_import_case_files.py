"""Fast, non-live checks for the imported-part eval cases (issue #340).

Verifies — without calling a model, Docker, or the network:
- the three ``imported_part`` case files parse via ``load_golden_set``
  (prompt pins re-checked) and each pins the ``part.stl`` fixture;
- the fixture exists, is under 1 MB, and parses with trimesh as a
  watertight, winding-consistent mesh;
- the run.py mesh-staging path hands the fixture to the render seam as
  ``part_path``/``repo_dir`` for a ``part``-carrying case, and passes no
  part for a part-less case (the no-part render stays byte-identical);
- the import guard runs inside the gate phase: a candidate that does not
  ``import("part.stl")`` at the settled scale fails the gate with the
  guard's detail, and a compliant candidate passes it.
"""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path

import pytest

from d33d.design_prompts import import_part_instruction
from d33d.evals.case_schema import (
    GATE_NAMES,
    KIND_PRECHECK_GATES,
    load_golden_set,
    verify_seed,
)
from d33d.evals.harness import run_case_gates
from tests.evals._casefile_helpers import write_cases as _write_cases

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"
FIXTURE = CASES_DIR / "fixtures" / "part.stl"
IMPORT_CASE_IDS = (
    "import-drill-hole",
    "import-cut-slot",
    "import-split-for-bed",
)


def _load() -> dict:
    return load_golden_set(CASES_DIR, REPO_ROOT)


def _load_run_module():
    spec = importlib.util.spec_from_file_location(
        "evals_run", REPO_ROOT / "evals" / "run.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# (a) the three case files parse and pin the fixture
# ---------------------------------------------------------------------------


def test_imported_part_cases_parse_and_pin_fixture() -> None:
    cases = _load()
    for cid in IMPORT_CASE_IDS:
        case = cases.get(cid)
        assert case is not None, f"missing case {cid}"
        assert case.kind == "imported_part"
        assert case.case_id == cid
        assert case.part is not None, f"{cid}: no part ref"
        assert case.part.fixture == "evals/cases/fixtures/part.stl"
        assert case.part.scale == 1.0
        # gate_expectations name only the base seven gates — the
        # import_guard pre-check is implied by the kind (single source:
        # KIND_PRECHECK_GATES), never declared in the case file.
        precheck = KIND_PRECHECK_GATES.get(case.kind)
        for gate in case.gate_expectations:
            assert gate in GATE_NAMES
            assert gate != precheck, f"{cid}: {gate!r} is a kind pre-check gate"


def test_import_cases_pin_prompt_with_the_settled_scale() -> None:
    """For EVERY imported_part case, the pinned prompt file must contain
    ``import_part_instruction(case.part.scale)`` verbatim — the prompt
    file's import paragraph IS that output, and the import guard accepts
    exactly that settled scale. A future case with a non-1.0 scale whose
    prompt was not re-rendered fails fast at CI time instead of silently
    fighting the import guard."""
    cases = _load()
    for cid, case in cases.items():
        if case.kind != "imported_part":
            continue
        assert case.part is not None
        prompt_text = (REPO_ROOT / case.prompt.path).read_text(encoding="utf-8")
        expected = import_part_instruction(case.part.scale)
        assert expected in prompt_text, (
            f"{cid}: prompt {case.prompt.path!r} does not carry "
            f"import_part_instruction({case.part.scale}) verbatim — re-render "
            "the prompt for the case's settled scale (and re-pin)"
        )


def test_import_guard_is_in_the_gate_registry() -> None:
    """import_guard is the imported_part kind's pre-check gate and the
    report's gate order carries it, ordered first (issue #340)."""
    from d33d.evals.report import BASE_GATE_ORDER, case_gate_order

    assert KIND_PRECHECK_GATES == {"imported_part": "import_guard"}
    assert "import_guard" not in GATE_NAMES  # implied, not declared
    assert "import_guard" not in BASE_GATE_ORDER
    order = case_gate_order("imported_part")
    assert order[0] == "import_guard"  # ordered first
    assert order[1:] == BASE_GATE_ORDER
    # a kind without a pre-check keeps the base order unchanged
    assert case_gate_order("primitive") == BASE_GATE_ORDER
    # and every declared gate is one the registry knows (base + pre-checks)
    known = set(GATE_NAMES) | set(KIND_PRECHECK_GATES.values())
    cases = _load()
    for cid, case in cases.items():
        for gate in case.gate_expectations:
            assert gate in known, f"{cid}: unknown gate {gate!r}"


def test_fixture_exists_under_1mb_and_parses_watertight() -> None:
    assert FIXTURE.is_file(), "part.stl fixture missing"
    assert FIXTURE.stat().st_size < 1_000_000, "part.stl exceeds 1 MB"
    import trimesh

    mesh = trimesh.load(str(FIXTURE), process=False)
    mesh.merge_vertices()
    assert mesh.is_watertight, "part.stl is not watertight"
    assert mesh.is_winding_consistent, "part.stl is not winding-consistent"
    assert len(mesh.faces) > 0


def test_verify_seed_is_empty_with_fixture_check() -> None:
    cases = _load()
    violations = verify_seed(cases, REPO_ROOT)
    assert violations == [], f"seed violations: {violations}"


def test_verify_seed_flags_missing_fixture() -> None:
    """A part fixture that does not exist on disk is a seed violation."""
    cases = _load()
    case = cases[IMPORT_CASE_IDS[0]]
    case.part.fixture = "evals/cases/fixtures/missing.stl"
    violations = verify_seed(cases, REPO_ROOT)
    assert any("missing.stl" in v for v in violations)


# ---------------------------------------------------------------------------
# (b) run.py mesh-staging: the fixture goes to the render seam as part_path
# ---------------------------------------------------------------------------


class _RenderResult:
    def __init__(self) -> None:
        self.error_class = "ok"
        self.stderr = ""
        self.scad_source = ""
        self.stl = None
        self.csg = None
        self.views = ()


def test_run_all_stages_fixture_as_part_path(tmp_path: Path) -> None:
    """run.py hands the case's fixture to the render seam as part_path +
    repo_dir for a part-carrying case, and renders without a part for a
    part-less case."""
    import asyncio

    run = _load_run_module()

    cases_dir = _write_cases(
        tmp_path,
        [
            {
                "case_id": "import-drill-hole",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {"fixture": "evals/cases/fixtures/part.stl", "scale": 1.0},
            },
            {
                "case_id": "primitive-box-20mm",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )

    calls: list[dict] = []

    def render_fn(scad_source, **kwargs):
        calls.append({"scad": scad_source, **kwargs})
        return _RenderResult()

    async def factory(request_body):
        class R:
            def json(self):
                return {
                    "choices": [{"message": {"content": "scale(1) import(\"part.stl\")"}}]
                }

        return R()

    report = asyncio.run(
        run._run_all(
            repo_root=REPO_ROOT,
            config={},
            request_factory=factory,
            model_id="m",
            cases_dir=cases_dir,
            render_fn=render_fn,
        )
    )
    assert report  # the report JSON is non-empty

    staged = [c for c in calls if "part_path" in c]
    unstaged = [c for c in calls if "part_path" not in c]
    assert len(staged) == 1 and len(unstaged) == 1
    assert staged[0]["part_path"] == REPO_ROOT / "evals" / "cases" / "fixtures" / "part.stl"
    assert staged[0]["repo_dir"] == REPO_ROOT


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


# ---------------------------------------------------------------------------
# (b2) per-case containment: staging problems fail the case, never the run
# ---------------------------------------------------------------------------


def test_check_fixture_containment_rejects_empty_path(tmp_path: Path) -> None:
    from d33d.evals.case_schema import check_fixture_containment

    assert check_fixture_containment(tmp_path, "") == "part fixture path is empty"
    assert check_fixture_containment(tmp_path, "   ") == "part fixture path is empty"


# ---------------------------------------------------------------------------
# (b2) per-case containment: staging problems fail the case, never the run
# ---------------------------------------------------------------------------


def _llm_factory():
    """The stub LLM call. First call per run is the design call — return
    OpenSCAD. Subsequent calls are the judge — return a passing verdict
    (the stub render fn has no STL/views, so the judge's content must be
    JSON it can parse). The bad cases never reach the judge (they fail
    at staging), so the normal case's judge call is the 2nd call."""
    state = {"calls": 0}

    async def factory(request_body):
        state["calls"] += 1
        content = (
            'scale(1) import("part.stl");'
            if state["calls"] == 1
            else '{"pass": true, "reason": "stub verdict"}'
        )

        class R:
            def json(self):
                return {"choices": [{"message": {"content": content}}]}

        return R()

    return factory


def _run(tmp_path: Path, cases: list[dict], render_fn=None):
    import asyncio

    run = _load_run_module()
    if render_fn is None:

        def render_fn(scad_source, **kwargs):
            return _RenderResult()

    return asyncio.run(
        run._run_all(
            repo_root=REPO_ROOT,
            config={},
            request_factory=_llm_factory(),
            model_id="m",
            cases_dir=_write_cases(tmp_path, cases),
            render_fn=render_fn,
        )
    )


def test_escaping_fixture_yields_failure_outcome_other_cases_run(tmp_path: Path):
    """A ``../`` fixture escape is that case's failure — the run, the other
    case, and the report are all unaffected (issue #340 per-case
    containment)."""
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "escape-bad",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/../part.stl",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["escape-bad"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "escapes" in bad["detail"]
    # the other case still ran and still passes
    good = cases["normal-box"]
    assert good["ok"] is True
    assert good["gates"]["compile"]["status"] == "pass"
    assert report_doc["summary"]["total"] == 2
    assert report_doc["summary"]["passed"] == 1


def test_non_stl_fixture_yields_failure_outcome_other_cases_run(tmp_path: Path):
    """A ``.3mf`` fixture (3MF staging is out of scope) is that case's
    failure — the run, the other case, and the report are unaffected."""
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "bad-ext",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/part.3mf",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    report_doc = _json.loads(report)
    cases = report_doc["cases"]
    bad = cases["bad-ext"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "must be an STL file" in bad["detail"]
    good = cases["normal-box"]
    assert good["ok"] is True
    assert report_doc["summary"]["passed"] == 1


def test_missing_fixture_yields_failure_outcome(tmp_path: Path):
    import json as _json

    report = _run(
        tmp_path,
        [
            {
                "case_id": "missing-fix",
                "kind": "imported_part",
                "request": "drill a hole",
                "part": {
                    "fixture": "evals/cases/fixtures/no-such.stl",
                    "scale": 1.0,
                },
            },
            {
                "case_id": "normal-box",
                "kind": "primitive",
                "request": "a box",
            },
        ],
    )
    cases = _json.loads(report)["cases"]
    bad = cases["missing-fix"]
    assert bad["ok"] is False
    assert bad["failure_class"] == "artifact_error"
    assert "missing on disk" in bad["detail"]
    assert cases["normal-box"]["ok"] is True


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


# ---------------------------------------------------------------------------
# (b3) verify_seed containment via the shared helper (issue #340 fix round)
# ---------------------------------------------------------------------------


def test_verify_seed_flags_escaping_fixture() -> None:
    """A fixture that escapes ``evals/cases/fixtures/`` (``../``
    traversal) is a seed violation, reported via the shared containment
    helper — not just a missing file."""
    cases = _load()
    cases[IMPORT_CASE_IDS[0]].part.fixture = "evals/cases/fixtures/../part.stl"
    violations = verify_seed(cases, REPO_ROOT)
    assert any("escapes" in v for v in violations)


def test_verify_seed_flags_absolute_fixture(tmp_path: Path) -> None:
    """An absolute fixture path is a seed violation (the fixture must be a
    relative path under the repo root)."""
    cases = _load()
    cases[IMPORT_CASE_IDS[0]].part.fixture = str(FIXTURE)
    violations = verify_seed(cases, REPO_ROOT)
    assert any("not a relative path" in v for v in violations)


def test_verify_seed_without_repo_root_reports_cannot_verify() -> None:
    """With ``repo_root=None`` the containment check cannot resolve the
    fixture path: the violation is "cannot verify fixture without
    repo_root" (a None repo_root is not dereferenced) and the run of
    the other checks still completes without crashing."""
    cases = _load()
    violations = verify_seed(cases)
    for cid in IMPORT_CASE_IDS:
        assert any(
            v.startswith(f"{cid}: cannot verify fixture without repo_root")
            for v in violations
        ), f"{cid}: missing the no-repo_root violation in {violations}"
