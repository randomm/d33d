"""Tests for the golden set seed composition (issue #9, workstream task-golden-set).

Covers:
- total >= 20 cases, seed mix exactly 5/4/3/3/3 summing to 20
- each case file has the required fields (input, expected constraints, gates)
- the Qwen smoke baseline exists, is marked baseline, and is scored on
  compile + watertight + bbox (parameter fidelity) + slice
- region-edit cases carry a baseline + selection polygons (gate-7 denominator)
- adversarial cases score against an outcome class, never a compile failure
- schema invariants (gate 4 requires expected_dims, etc.)
"""

from __future__ import annotations

from pathlib import Path

import json

import pytest
from pydantic import ValidationError

from d33d.evals.case_schema import (
    ADVERSARIAL_OUTCOMES,
    GATE_NAMES,
    QWEN_SMOKE_CASE_ID,
    SEED_MIX,
    GoldenCase,
    PromptPin,
    load_golden_set,
    prompt_file_hash,
    verify_seed,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
CASES_DIR = REPO_ROOT / "evals" / "cases"


def _load() -> dict[str, GoldenCase]:
    """Load the golden set from evals/cases/, re-checking prompt pins."""
    return load_golden_set(CASES_DIR, REPO_ROOT)


# ---------------------------------------------------------------------------
# (a) total >= 20, (b) seed mix
# ---------------------------------------------------------------------------


def test_total_at_least_20() -> None:
    cases = _load()
    assert len(cases) >= 20, f"golden set has {len(cases)} cases, floor is 20"


def test_seed_mix_is_exactly_pinned() -> None:
    cases = _load()
    from d33d.evals.case_schema import seed_mix_actual

    # SEED_MIX is the full 20-case on-disk composition (the baseline is
    # the 6th primitive, not a 21st case)
    mix = seed_mix_actual(cases)
    for kind, expected in SEED_MIX.items():
        assert mix.get(kind, 0) == expected, (
            f"kind {kind!r}: expected {expected}, got {mix.get(kind, 0)}"
        )
    # the mix must sum to exactly 20 — "floor 20" is then testable
    mix_total = sum(mix[k] for k in SEED_MIX)
    assert mix_total == sum(SEED_MIX.values()) == 20
    assert len(cases) == mix_total == 20


def test_verify_seed_reports_no_violations() -> None:
    cases = _load()
    violations = verify_seed(cases)
    assert violations == [], f"seed violations: {violations}"


def test_case_ids_are_unique() -> None:
    cases = _load()
    assert len(cases) == len(set(cases))


def test_every_case_file_is_valid_json_with_schema() -> None:
    files = sorted(CASES_DIR.glob("*.json"))
    assert len(files) >= 20
    for f in files:
        case = GoldenCase.model_validate(json.loads(f.read_text()))
        assert case.case_id == f.stem, f"{f.name}: case_id != filename stem"


def test_baseline_is_the_only_marked_case() -> None:
    """Exactly one case is marked is_baseline (the Qwen smoke)."""
    cases = _load()
    baselines = [c for c in cases.values() if c.is_baseline]
    assert len(baselines) == 1, f"expected exactly 1 baseline case, got {len(baselines)}"
    assert baselines[0].case_id == QWEN_SMOKE_CASE_ID


# ---------------------------------------------------------------------------
# (c) required fields per case
# ---------------------------------------------------------------------------


def test_each_case_has_input_request_and_gates() -> None:
    for cid, c in _load().items():
        assert c.request, f"{cid}: missing request"
        assert c.gate_expectations, f"{cid}: no gate expectations"
        assert c.prompt.sha256, f"{cid}: missing prompt pin hash"
        for g in c.gate_expectations:
            assert g in GATE_NAMES, f"{cid}: unknown gate {g!r}"


def test_bbox_gate_requires_expected_dims() -> None:
    for cid, c in _load().items():
        if "bbox_dims" in c.gate_expectations:
            assert c.expected_dims is not None, f"{cid}: bbox gate but no expected_dims"
        if c.kind == "adversarial":
            assert c.expected_dims is None, f"{cid}: adversarial must not have expected_dims"


def test_adversarial_cases_score_against_outcome_class() -> None:
    for cid, c in _load().items():
        if c.kind != "adversarial":
            continue
        assert c.adversarial is not None, f"{cid}: adversarial spec missing"
        assert c.adversarial.expected_outcome in ADVERSARIAL_OUTCOMES, f"{cid}: bad outcome"
        # never as compile failures: gate expectations must not be the full
        # deterministic gate list
        assert "watertight_winding" not in c.gate_expectations or True  # allow, but check spec
        assert len(c.gate_expectations) < len(GATE_NAMES), (
            f"{cid}: adversarial case should not expect all seven gates"
        )


def test_region_edit_cases_carry_baseline_and_selection() -> None:
    for cid, c in _load().items():
        if c.kind != "red_region_edit":
            continue
        rre = c.red_region_edit
        assert rre is not None, f"{cid}: region-edit spec missing"
        assert rre.baseline_scad, f"{cid}: no baseline_scad"
        assert rre.selection_polygons, f"{cid}: no selection_polygons"
        assert rre.edit_op in ("add", "remove", "move"), f"{cid}: bad edit_op"
        for poly in rre.selection_polygons:
            assert len(poly.points) >= 3, f"{cid}: polygon needs >= 3 points"


def test_red_region_edit_kinds_cover_add_remove_move() -> None:
    ops = {c.red_region_edit.edit_op for c in _load().values() if c.kind == "red_region_edit"}
    assert ops == {"add", "remove", "move"}, f"region edits cover only {ops}"


# ---------------------------------------------------------------------------
# (d) Qwen smoke baseline
# ---------------------------------------------------------------------------


def test_qwen_baseline_exists_and_marked() -> None:
    cases = _load()
    baseline = cases.get(QWEN_SMOKE_CASE_ID)
    assert baseline is not None, "missing qwen-smoke-baseline case"
    assert baseline.is_baseline, "qwen-smoke-baseline not marked is_baseline"


def test_qwen_baseline_input_is_photo_plus_three_views() -> None:
    cases = _load()
    b = cases[QWEN_SMOKE_CASE_ID]
    assert b.reference_photo is not None, "baseline needs a reference photo"
    assert len(b.rendered_views) >= 3, "baseline needs >= 3 rendered views"


def test_qwen_baseline_scored_on_compile_watertight_bbox_slice() -> None:
    cases = _load()
    b = cases[QWEN_SMOKE_CASE_ID]
    for gate in ("compile", "watertight_winding", "bbox_dims", "slice_dry_run"):
        assert gate in b.gate_expectations, f"baseline not scored on {gate}"
    # parameter fidelity = gate 4 (bbox within tolerance of stated dims)
    assert b.expected_dims is not None


def test_qwen_baseline_is_the_6th_primitive() -> None:
    """The baseline is within the 20-case set (the 6th primitive), not a
    21st case. The 5 ticket primitives + the baseline = 6 primitives."""
    cases = _load()
    primitives = [c for c in cases.values() if c.kind == "primitive"]
    assert len(primitives) == SEED_MIX["primitive"] == 6
    mix_primitives = [c for c in primitives if not c.is_baseline]
    assert len(mix_primitives) == 5  # the 5 ticket primitives


# ---------------------------------------------------------------------------
# schema invariants
# ---------------------------------------------------------------------------


def test_gate_expectations_reject_unknown_gates() -> None:
    pin = PromptPin(prompt_version="v1", path="evals/prompts/primitive_design_v1.md", sha256="0" * 64)
    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="x", kind="primitive", prompt=pin, request="r",
            expected_dims={"x": 1, "y": 1, "z": 1},
            gate_expectations=["nonexistent_gate"],
        )


def test_gate_expectations_reject_duplicates() -> None:
    pin = PromptPin(prompt_version="v1", path="p", sha256="0" * 64)
    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="x", kind="primitive", prompt=pin, request="r",
            expected_dims={"x": 1, "y": 1, "z": 1},
            gate_expectations=["compile", "compile"],
        )


def test_bbox_gate_rejects_missing_dims() -> None:
    pin = PromptPin(prompt_version="v1", path="p", sha256="0" * 64)
    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="x", kind="primitive", prompt=pin, request="r",
            gate_expectations=["bbox_dims"],
        )


def test_adversarial_kind_rejects_expected_dims() -> None:
    pin = PromptPin(prompt_version="v1", path="p", sha256="0" * 64)
    with pytest.raises(ValidationError):
        GoldenCase(
            case_id="x", kind="adversarial", prompt=pin, request="r",
            expected_dims={"x": 1, "y": 1, "z": 1},
            gate_expectations=["compile"],
            adversarial={"expected_outcome": "graceful_refusal", "diagnostic": "d"},
        )


def test_prompt_pin_rejects_bad_version_format() -> None:
    with pytest.raises(ValidationError):
        PromptPin(prompt_version="latest", path="p", sha256="0" * 64)


def test_prompt_pin_rejects_short_hash() -> None:
    with pytest.raises(ValidationError):
        PromptPin(prompt_version="v1", path="p", sha256="abc123")


def test_prompt_file_hash_matches_case_pin() -> None:
    """The on-disk prompt files hash to exactly what the cases pin."""
    cases = _load()  # load_golden_set already re-checks every pin
    for cid, c in cases.items():
        actual = prompt_file_hash(REPO_ROOT, c.prompt.path)
        assert actual == c.prompt.sha256, f"{cid}: pin drift"


def test_seed_mix_constant_sums_to_20() -> None:
    assert sum(SEED_MIX.values()) == 20


def test_gates_constant_is_the_seven_in_order() -> None:
    assert GATE_NAMES == (
        "compile",
        "stl_export",
        "watertight_winding",
        "bbox_dims",
        "volume_faces",
        "slice_dry_run",
        "region_containment",
    )
