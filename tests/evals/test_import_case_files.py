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

import importlib.util
import json
from pathlib import Path

from d33d.evals.case_schema import GATE_NAMES, load_golden_set, verify_seed
from d33d.evals.harness import run_case_gates

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
        for gate in case.gate_expectations:
            assert gate in GATE_NAMES


def test_fixture_exists_under_1mb_and_parses_watertight() -> None:
    assert FIXTURE.is_file(), "part.stl fixture missing"
    assert FIXTURE.stat().st_size < 1_000_000, "part.stl exceeds 1 MB"
    import trimesh

    mesh = trimesh.load(str(FIXTURE), process=False)
    mesh.merge_vertices()
    assert mesh.is_watertight, "part.stl is not watertight"
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

    cases_dir = tmp_path / "cases"
    cases_dir.mkdir()
    # one imported_part case + one prompt pin it can carry
    prompts_dir = tmp_path / "prompts"
    prompts_dir.mkdir()
    prompt = prompts_dir / "p.md"
    prompt.write_text("# prompt v1", encoding="utf-8")
    import hashlib

    pin = hashlib.sha256(prompt.read_bytes()).hexdigest()
    (cases_dir / "import-drill-hole.json").write_text(
        json.dumps(
            {
                "case_id": "import-drill-hole",
                "kind": "imported_part",
                "prompt": {
                    "prompt_version": "v1",
                    "path": str(prompt),
                    "sha256": pin,
                },
                "request": "drill a hole",
                "gate_expectations": ["compile"],
                "part": {"fixture": "evals/cases/fixtures/part.stl", "scale": 1.0},
            }
        ),
        encoding="utf-8",
    )
    (cases_dir / "primitive-box-20mm.json").write_text(
        json.dumps(
            {
                "case_id": "primitive-box-20mm",
                "kind": "primitive",
                "prompt": {
                    "prompt_version": "v1",
                    "path": str(prompt),
                    "sha256": pin,
                },
                "request": "a box",
                "gate_expectations": ["compile"],
            }
        ),
        encoding="utf-8",
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
