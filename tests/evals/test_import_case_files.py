"""Fast, non-live checks for the imported-part eval cases (issue #340).

Verifies — without calling a model, Docker, or the network:
- the three ``imported_part`` case files parse via ``load_golden_set``
  (prompt pins re-checked) and each pins the ``part.stl`` fixture;
- the fixture exists, is under 1 MB, and parses with trimesh as a
  watertight, winding-consistent mesh.
"""

from __future__ import annotations

import pytest

from d33d.design_prompts import import_part_instruction
from d33d.evals.case_schema import (
    GATE_NAMES,
    KIND_PRECHECK_GATES,
    load_golden_set,
    verify_seed,
)
from tests.evals._casefile_helpers import (
    CASES_DIR,
    FIXTURE,
    IMPORT_CASE_IDS,
    REPO_ROOT,
)


def _load() -> dict:
    return load_golden_set(CASES_DIR, REPO_ROOT)


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
    from d33d.evals.report import case_gate_order

    assert KIND_PRECHECK_GATES == {"imported_part": "import_guard"}
    assert "import_guard" not in GATE_NAMES  # implied, not declared
    order = case_gate_order("imported_part")
    assert order[0] == "import_guard"  # ordered first
    assert order[1:] == GATE_NAMES
    # a kind without a pre-check keeps the base order unchanged
    assert case_gate_order("primitive") == GATE_NAMES
    # and every declared gate is one the registry knows (base + pre-checks)
    known = set(GATE_NAMES) | set(KIND_PRECHECK_GATES.values())
    cases = _load()
    for cid, case in cases.items():
        for gate in case.gate_expectations:
            assert gate in known, f"{cid}: unknown gate {gate!r}"


def test_case_gate_order_uses_the_schema_gate_names() -> None:
    """The report's base gate order is the schema's ``GATE_NAMES``
    itself — one source of the declare-able taxonomy (the report no
    longer keeps its own ``BASE_GATE_ORDER`` alias)."""
    from d33d.evals.report import case_gate_order

    for kind in (
        "primitive",
        "red_region_edit",
        "boolean_topology",
        "photo_recreation",
        "adversarial",
    ):
        assert case_gate_order(kind) == GATE_NAMES
    assert case_gate_order("imported_part") == ("import_guard", *GATE_NAMES)


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


def test_imported_part_case_requires_part() -> None:
    """An ``imported_part`` case without a part ref is a schema error —
    the import guard must never silently skip because of a missing ref."""
    from pydantic import ValidationError

    from d33d.evals.case_schema import GoldenCase, PromptPin

    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="import-no-part",
            kind="imported_part",
            prompt=PromptPin(
                prompt_version="v1", path="p.md", sha256="f" * 64
            ),
            request="drill a hole",
            gate_expectations=["compile"],
        )


def test_primitive_case_must_not_carry_part() -> None:
    """A non-``imported_part`` case carrying a part ref is a schema error."""
    from pydantic import ValidationError

    from d33d.evals.case_schema import GoldenCase, PromptPin
    from d33d.evals.part_ref import PartRef

    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="primitive-with-part",
            kind="primitive",
            prompt=PromptPin(
                prompt_version="v1", path="p.md", sha256="f" * 64
            ),
            request="a box",
            gate_expectations=["compile"],
            part=PartRef(fixture="evals/cases/fixtures/part.stl", scale=1.0),
        )
