"""Failure-class tagging tests for the eval harness (issue #9,
workstream ``task-gates``).

The harness uses a SUPERSET of #5's taxonomy — the 11 named LLM
failure classes (reused, not forked) + the 5 non-repairable render-
worker classes + the unrecognised-syntax fallback + two eval outcome
classes (``graceful_refusal`` / ``clearance_applied``). Adversarial
cases are scored against those two outcome classes, never as compile
failures.

This test file pins:

* the superset is a proper superset of #5's ``FAILURE_CLASSES`` (the
  17 entries), plus exactly the two outcome classes
* ``map_to_design_classes`` projects the superset back to #5's
  vocabulary (the two outcome classes collapse to
  ``unclassified_syntax_error``; everything else maps to itself)
* the per-gate taggability table: ``geometrically_wrong`` is taggable
  only at gate 4 (the bbox deterministic proxy) and the judge stage —
  never at gates 1, 2, 3, or 5; the two outcome classes are not
  taggable at any of gates 1–5
* gate 1's tagging reuses :func:`d33d.failure_classes.classify_
  failure` (no forked classifier), one fixture stderr per named LLM
  class
"""

from __future__ import annotations

import pytest

from d33d import failure_classes as fc
from d33d.evals import gates as eg

#: The 11 named LLM classes of #5 (reused from ``failure_classes``).
E11 = {
    "trailing_semicolon",
    "transform_order",
    "wrong_axis_rotation",
    "difference_inversion",
    "hull_miskowski_misuse",
    "zup_yup_confusion",
    "magic_numbers",
    "projection_offset_fragile",
    "text_missing_font",
    "hallucinated_bosl2",
    "geometrically_wrong",
}

#: The 5 non-repairable render-worker classes.
E5 = {
    "timeout",
    "oom",
    "container_error",
    "artifact_error",
    "empty_model",
}

#: The 17 entries of ``failure_classes.FAILURE_CLASSES`` (11 + 5 + 1
#: fallback).
E17 = E11 | E5 | {"unclassified_syntax_error"}  # 11 + 5 + 1 = 17

#: The two eval outcome classes.
OUTCOMES = {"graceful_refusal", "clearance_applied"}


# ---------------------------------------------------------------------------
# Superset structure
# ---------------------------------------------------------------------------


def test_superset_contains_all_17_failure_classes():
    assert E17.issubset(eg.EVAL_FAILURE_CLASSES)


def test_superset_contains_both_outcome_classes():
    assert OUTCOMES.issubset(eg.EVAL_FAILURE_CLASSES)


def test_superset_is_exactly_19_classes():
    """17 (failure_classes) + 2 (outcomes) = 19. No extras, no forks."""
    assert len(eg.EVAL_FAILURE_CLASSES) == 19
    assert eg.EVAL_FAILURE_CLASSES == E17 | OUTCOMES


def test_design_loop_classes_are_the_17():
    """``DESIGN_LOOP_CLASSES`` is the #5 vocabulary — the 17 entries of
    ``failure_classes.FAILURE_CLASSES`` (the 11 named LLM classes +
    5 non-repairable + 1 fallback)."""
    assert eg.DESIGN_LOOP_CLASSES == fc.FAILURE_CLASSES
    assert len(eg.DESIGN_LOOP_CLASSES) == 17


def test_outcome_classes_are_exactly_two():
    assert eg.OUTCOME_CLASSES == OUTCOMES
    assert len(eg.OUTCOME_CLASSES) == 2


def test_outcome_classes_disjoint_from_design_loop():
    """The two outcome classes are not part of #5's taxonomy — they
    exist only in the eval superset."""
    assert eg.OUTCOME_CLASSES.isdisjoint(eg.DESIGN_LOOP_CLASSES)


# ---------------------------------------------------------------------------
# Superset → #5 mapping
# ---------------------------------------------------------------------------


def test_map_is_identity_for_all_17():
    mapping = eg.map_to_design_classes(set(E17))
    for c in E17:
        assert mapping[c] == c


def test_map_collapses_outcome_classes_to_unclassified():
    mapping = eg.map_to_design_classes(set(OUTCOMES))
    assert mapping["graceful_refusal"] == "unclassified_syntax_error"
    assert mapping["clearance_applied"] == "unclassified_syntax_error"


def test_map_full_superset_is_total():
    """Every superset class has a mapping — the projection is total,
    so the report can always be expressed in #5's vocabulary."""
    mapping = eg.map_to_design_classes(set(eg.EVAL_FAILURE_CLASSES))
    assert set(mapping) == eg.EVAL_FAILURE_CLASSES
    # All mapped values are in #5's vocabulary.
    assert set(mapping.values()).issubset(fc.FAILURE_CLASSES)


def test_map_adversarial_outcomes_never_compile_failures():
    """The two outcome classes map to ``unclassified_syntax_error``,
    not to any specific compile-failure class — adversarial cases are
    scored as outcomes, never as compile failures."""
    mapping = eg.map_to_design_classes(set(OUTCOMES))
    for c in OUTCOMES:
        assert mapping[c] == "unclassified_syntax_error"
        assert mapping[c] not in E11 - {"unclassified_syntax_error"}


# ---------------------------------------------------------------------------
# Per-gate taggability
# ---------------------------------------------------------------------------


def test_gate1_tags_all_16_except_geometrically_wrong():
    """Gate 1 tags the 16 classes of ``FAILURE_CLASSES`` minus
    ``geometrically_wrong`` (an output that fails to compile is not a
    "compiles cleanly but geometrically wrong" outcome)."""
    taggable = eg.GATE_TAGGABLE_CLASSES["compile"]
    assert "geometrically_wrong" not in taggable
    assert (E17 - {"geometrically_wrong"}).issubset(taggable)
    assert len(taggable) == 16


def test_gates_2_3_5_tag_all_16_except_geometrically_wrong():
    for gate in ("stl_export", "watertight", "volume"):
        taggable = eg.GATE_TAGGABLE_CLASSES[gate]
        assert "geometrically_wrong" not in taggable
        assert (E17 - {"geometrically_wrong"}).issubset(taggable)
        assert len(taggable) == 16


def test_gate4_tags_geometrically_wrong():
    """Gate 4 is the only one of gates 1–5 that can tag
    ``geometrically_wrong`` — the bbox deviation is the deterministic
    proxy for the vision-only class."""
    taggable = eg.GATE_TAGGABLE_CLASSES["bbox"]
    assert "geometrically_wrong" in taggable
    assert (E17 - {"geometrically_wrong"}).issubset(taggable)
    assert len(taggable) == 17


def test_no_gate_tags_outcome_classes():
    """``graceful_refusal`` / ``clearance_applied`` are adversarial
    verdicts, not gate failures — no gate of 1–5 can tag them."""
    for gate in eg.GATE_NAMES:
        taggable = eg.GATE_TAGGABLE_CLASSES[gate]
        assert OUTCOMES.isdisjoint(taggable)


def test_geometrically_wrong_not_taggable_at_gates_1_2_3_5():
    for gate in ("compile", "stl_export", "watertight", "volume"):
        assert "geometrically_wrong" not in eg.GATE_TAGGABLE_CLASSES[gate]


def test_assert_taggable_passes_for_valid_pair():
    # No exception for a taggable pair.
    eg.assert_taggable("bbox", "geometrically_wrong")
    eg.assert_taggable("compile", "timeout")
    eg.assert_taggable("stl_export", "artifact_error")


def test_assert_taggable_rejects_geometrically_wrong_at_gate1():
    with pytest.raises(ValueError, match="not taggable at gate"):
        eg.assert_taggable("compile", "geometrically_wrong")


def test_assert_taggable_rejects_outcome_classes_at_any_gate():
    for gate in eg.GATE_NAMES:
        for oc in OUTCOMES:
            with pytest.raises(ValueError, match="not taggable at gate"):
                eg.assert_taggable(gate, oc)


def test_assert_taggable_rejects_unknown_gate():
    with pytest.raises(ValueError, match="unknown gate"):
        eg.assert_taggable("gate_8", "timeout")


def test_gate_names_are_the_five_in_order():
    assert eg.GATE_NAMES == ("compile", "stl_export", "watertight", "bbox", "volume")


def test_every_gate_name_has_a_taggable_entry():
    for gate in eg.GATE_NAMES:
        assert gate in eg.GATE_TAGGABLE_CLASSES
        assert len(eg.GATE_TAGGABLE_CLASSES[gate]) > 0


# ---------------------------------------------------------------------------
# Gate 1 tagging reuses failure_classes.classify_failure
# ---------------------------------------------------------------------------


def _classify_via_gate1(error_class: str, stderr: str = "") -> str:
    """Run gate 1 and return the tagged failure class."""
    r = eg.gate1_compiles(error_class, stderr=stderr)
    assert r.status == "fail"
    assert r.failure_class is not None
    return r.failure_class


@pytest.mark.parametrize(
    ("stderr", "expected_class"),
    [
        ("ERROR: Unknown module foo", "hallucinated_bosl2"),
        ("ERROR: font= missing", "text_missing_font"),
        ("ERROR: hull() child", "hull_miskowski_misuse"),
        ("ERROR: difference( inverted", "difference_inversion"),
        ("ERROR: offset( projection", "projection_offset_fragile"),
        ("ERROR: z-up y-up blender", "zup_yup_confusion"),
        ("ERROR: translate([1,2,3]) translate([4,5,6])", "transform_order"),
        ("ERROR: rotate(90, [0,0,1])", "wrong_axis_rotation"),
        ("ERROR: translate([1,2,3]); cube", "trailing_semicolon"),
        ("ERROR: something unrecognised", "unclassified_syntax_error"),
    ],
)
def test_gate1_tags_each_named_llm_class(stderr, expected_class):
    """Gate 1's tagging is the same first-match-wins classifier as
    ``failure_classes.classify_failure`` — no forked table."""
    assert _classify_via_gate1("syntax_error", stderr) == expected_class


def test_gate1_non_repairable_classes_pass_through():
    for ec in E5:
        assert _classify_via_gate1(ec) == ec


def test_gate1_tags_are_always_taggable():
    """Every class gate 1 can produce is in its taggable set — the
    assert_taggable invariant holds by construction."""
    # syntax_error with each stderr pattern
    for stderr in (
        "ERROR: Unknown module",
        "ERROR: font=",
        "ERROR: hull()",
        "ERROR: difference(",
        "ERROR: offset(",
        "ERROR: z-up",
        "ERROR: translate() translate()",
        "ERROR: rotate(1, [",
        "ERROR: translate();x",
        "ERROR: unrecognised",
    ):
        r = eg.gate1_compiles("syntax_error", stderr=stderr)
        assert r.failure_class in eg.GATE_TAGGABLE_CLASSES["compile"]
    for ec in E5:
        r = eg.gate1_compiles(ec)
        assert r.failure_class in eg.GATE_TAGGABLE_CLASSES["compile"]
