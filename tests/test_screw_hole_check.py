"""tests/test_screw_hole_check.py — deterministic screw-hole clearance
post-check (issue #317).

Covers the ``d33d.screw_hole_check`` module's public API:

* ``mm`` — the millimetre value renderer (natural precision, ≥1 decimal).
* ``is_screw_hole_candidate`` — the param-identification predicate.
* ``undersize_screw_hole`` — the post-check itself: trigger, pass,
  boundary, no-screw-named, ambiguous-param, threaded-wording,
  missing-param-meta, word-boundary behaviour, and the repair dict's
  shape (``failure_class``, ``instruction`` substring, ``evidence``).
"""

from __future__ import annotations

from d33d.screw_hole_check import (
    is_screw_hole_candidate,
    mm,
    undersize_screw_hole,
)

# ---------------------------------------------------------------------------
# mm: the millimetre value renderer
# ---------------------------------------------------------------------------


def test_mm_natural_precision():
    assert mm(4.0) == "4.0"
    assert mm(4.5) == "4.5"
    assert mm(4.44) == "4.44"
    assert mm(9.0) == "9.0"
    assert mm(2.4) == "2.4"
    assert mm(4.10) == "4.1"


# ---------------------------------------------------------------------------
# is_screw_hole_candidate: the param-identification predicate
# ---------------------------------------------------------------------------


def test_candidate_hole_in_name():
    assert is_screw_hole_candidate("hole_d", 4.0, None)
    assert is_screw_hole_candidate("m4_hole", 4.0, None)
    assert is_screw_hole_candidate("screw_hole_diameter", 4.0, None)


def test_candidate_no_hole_in_name_rejected():
    assert not is_screw_hole_candidate("width", 4.0, None)
    assert not is_screw_hole_candidate("m4_diameter", 4.0, None)
    assert not is_screw_hole_candidate("bore", 4.0, None)


def test_candidate_threaded_in_name_rejected():
    assert not is_screw_hole_candidate("m4_threaded_hole", 4.0, None)
    assert not is_screw_hole_candidate("tapped_hole", 4.0, None)
    assert not is_screw_hole_candidate("insert_hole", 4.0, None)


def test_candidate_hole_in_meta_label():
    meta = {"label": "M4 hole diameter", "unit": "mm"}
    assert is_screw_hole_candidate("hole_d", 4.0, meta)
    # Meta without "hole" in label/reason disqualifies even if name has it
    # (the check requires hole wording in BOTH when meta is present).
    meta_no_hole = {"label": "Diameter", "unit": "mm"}
    assert not is_screw_hole_candidate("hole_d", 4.0, meta_no_hole)


def test_candidate_threaded_in_meta_rejected():
    meta = {"label": "M4 threaded hole", "unit": "mm"}
    assert not is_screw_hole_candidate("hole_d", 4.0, meta)
    meta_tapped = {"label": "Hole", "reason": "tapped for M4"}
    assert not is_screw_hole_candidate("hole_d", 4.0, meta_tapped)
    meta_insert = {"label": "Hole", "reason": "heat-set insert"}
    assert not is_screw_hole_candidate("hole_d", 4.0, meta_insert)


def test_candidate_zero_or_negative_value_ignored():
    # The predicate itself doesn't check the value (the caller does);
    # it only checks the name/meta wording.  A zero value is still a
    # candidate by name — the caller filters on value > 0.
    assert is_screw_hole_candidate("hole_d", 0.0, None)


# ---------------------------------------------------------------------------
# undersize_screw_hole: the post-check
# ---------------------------------------------------------------------------

REQUEST = "a 60 × 45 mm plate with an M4 hole"
META = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}


def test_triggers_on_undersize_m4():
    repair = undersize_screw_hole(REQUEST, {"W": 60.0, "hole_d": 4.0}, META)
    assert repair is not None
    assert repair["failure_class"] == "geometrically_wrong"
    assert "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm" in (
        repair["instruction"]
    )
    assert "scad_source" in repair
    assert "M4 hole diameter" in repair["evidence"]
    assert "4.0 mm" in repair["evidence"]
    assert "4.5 mm" in repair["evidence"]


def test_passes_at_clearance():
    assert undersize_screw_hole(REQUEST, {"hole_d": 4.5}, META) is None


def test_passes_above_clearance():
    assert undersize_screw_hole(REQUEST, {"hole_d": 5.0}, META) is None


def test_boundary_005_mm_abstains():
    # 4.5 - 4.45 = 0.05, NOT more than 0.05 → abstain.
    assert undersize_screw_hole(REQUEST, {"hole_d": 4.45}, META) is None


def test_boundary_005_plus_epsilon_triggers():
    # 4.5 - 4.44 = 0.06 > 0.05 → trigger.
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.44}, META)
    assert repair is not None
    assert "4.44 mm" in repair["instruction"]


def test_no_screw_size_named_abstains():
    assert undersize_screw_hole(
        "a 60 × 45 mm plate with a 4 mm hole", {"hole_d": 4.0}, META
    ) is None
    assert undersize_screw_hole("a plate with a hole", {"hole_d": 4.0}, META) is None


def test_ambiguous_two_candidates_abstains():
    params = {"hole_d": 4.0, "m4_hole": 4.0}
    meta = {
        "hole_d": {"label": "M4 hole diameter", "unit": "mm"},
        "m4_hole": {"label": "M4 hole", "unit": "mm"},
    }
    assert undersize_screw_hole(REQUEST, params, meta) is None


def test_no_hole_param_abstains():
    assert undersize_screw_hole(REQUEST, {"W": 60.0, "D": 45.0}, {}) is None


def test_empty_request_abstains():
    assert undersize_screw_hole("", {"hole_d": 4.0}, META) is None


def test_threaded_wording_abstains():
    meta = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}
    for request in (
        "a 60 × 45 mm plate with an M4 threaded hole",
        "a 60 × 45 mm plate with a tapped M4 hole",
        "a 60 × 45 mm plate with a heat-set insert for M4",
    ):
        assert undersize_screw_hole(request, {"hole_d": 4.0}, meta) is None, request


def test_word_boundary_m40_not_m4():
    meta = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}
    assert undersize_screw_hole(
        "an M40 flange plate", {"hole_d": 4.0}, meta
    ) is None


def test_word_boundary_bm4_not_m4():
    meta = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}
    assert undersize_screw_hole(
        "a plate with a BM4 reference", {"hole_d": 4.0}, meta
    ) is None


def test_m4x20_is_m4():
    # "M4x20" is a thread-length spec that still names M4.
    meta = {"hole_d": {"label": "M4 hole diameter", "unit": "mm"}}
    repair = undersize_screw_hole(
        "a plate with an M4x20 clearance hole", {"hole_d": 4.0}, meta
    )
    assert repair is not None
    assert "M4" in repair["instruction"]


def test_missing_param_meta_name_heuristic_fallback():
    # No metadata at all — the name "hole_d" reads as a hole.
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, {})
    assert repair is not None
    assert "hole_d" in repair["evidence"]


def test_non_numeric_param_value_ignored():
    # A non-numeric value in params is skipped by the check.
    params: dict[str, float] = {"hole_d": 4.0}  # type: ignore[assignment]
    params["bad"] = "not a number"  # type: ignore[dict-item]
    repair = undersize_screw_hole(REQUEST, params, META)
    assert repair is not None  # hole_d still triggers
    # The evidence uses the label from META (not the raw name).
    assert "M4 hole diameter" in repair["evidence"]


def test_evidence_uses_label_when_present():
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, META)
    assert repair is not None
    assert "M4 hole diameter" in repair["evidence"]
    assert "hole_d" not in repair["evidence"]


def test_evidence_uses_name_when_no_label():
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, {})
    assert repair is not None
    assert "hole_d" in repair["evidence"]


def test_repair_dict_has_all_required_keys():
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, META)
    assert repair is not None
    for key in ("failure_class", "instruction", "scad_source", "evidence"):
        assert key in repair, f"missing key: {key}"


def test_repair_failure_class_is_geometrically_wrong():
    """The post-check uses the EXISTING geometrically_wrong class —
    no new error_class is introduced (issue #317 operator decision)."""
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, META)
    assert repair is not None
    assert repair["failure_class"] == "geometrically_wrong"
    # The class is in the closed enum (verified in test_failure_classes.py,
    # but pinned here for the post-check's specific contract).
    from d33d.failure_classes import REPAIRABLE_CLASSES, FAILURE_CLASSES

    assert "geometrically_wrong" in FAILURE_CLASSES
    assert "geometrically_wrong" in REPAIRABLE_CLASSES


def test_instruction_carries_exact_message_substring():
    """The repair instruction carries the exact-form sentence as a
    substring (operator decision: the sentence appears INSIDE
    ``instruction``, not as the whole instruction)."""
    repair = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, META)
    assert repair is not None
    exact = "M4 clearance hole is 4.0 mm; printed M4 clearance is 4.5 mm"
    assert exact in repair["instruction"]
    # The instruction is longer than the exact sentence (it also carries
    # the remediation instruction).
    assert len(repair["instruction"]) > len(exact)


def test_m3_hole_at_3_0_triggers():
    """M3 clearance is 3.4 mm; a 3.0 mm hole is below by 0.4 mm → trigger."""
    meta = {"m3_hole": {"label": "M3 hole diameter", "unit": "mm"}}
    repair = undersize_screw_hole(
        "a plate with an M3 hole", {"m3_hole": 3.0}, meta
    )
    assert repair is not None
    assert "M3 clearance hole is 3.0 mm; printed M3 clearance is 3.4 mm" in (
        repair["instruction"]
    )


def test_m8_hole_at_8_5_triggers():
    """M8 clearance is 9.0 mm; an 8.5 mm hole is below by 0.5 mm → trigger."""
    meta = {"m8_hole": {"label": "M8 hole diameter", "unit": "mm"}}
    repair = undersize_screw_hole(
        "a plate with an M8 hole", {"m8_hole": 8.5}, meta
    )
    assert repair is not None
    assert "M8 clearance hole is 8.5 mm; printed M8 clearance is 9.0 mm" in (
        repair["instruction"]
    )


def test_m2_5_hole_at_2_5_triggers():
    """M2.5 clearance is 2.9 mm; a 2.5 mm hole is below by 0.4 mm →
    trigger. The regex must match M2.5 (not M2)."""
    meta = {"m25_hole": {"label": "M2.5 hole diameter", "unit": "mm"}}
    repair = undersize_screw_hole(
        "a plate with an M2.5 hole", {"m25_hole": 2.5}, meta
    )
    assert repair is not None
    assert "M2.5 clearance hole is 2.5 mm; printed M2.5 clearance is 2.9 mm" in (
        repair["instruction"]
    )


def test_two_screw_sizes_named_single_undersize_triggers():
    """If the request names two screw sizes and one param is undersize
    for BOTH sizes, the check sees two undersize entries (same param,
    different sizes) → abstain (ambiguous).  When only ONE (param, size)
    pair is undersize, the check fires."""
    meta = {
        "m4_hole": {"label": "M4 hole diameter", "unit": "mm"},
        "m3_hole": {"label": "M3 hole diameter", "unit": "mm"},
    }
    # m3_hole at 3.0: undersize for M3 (3.4-3.0=0.4>0.05) AND for M4
    # (4.5-3.0=1.5>0.05) — two entries → abstain (ambiguous).
    repair = undersize_screw_hole(
        "a plate with an M4 hole and an M3 hole",
        {"m3_hole": 3.0},
        meta,
    )
    assert repair is None


def test_two_undersize_sizes_abstains():
    """If two different screw sizes each have an undersize hole, the check
    cannot identify which one to repair → abstain."""
    meta = {
        "m4_hole": {"label": "M4 hole diameter", "unit": "mm"},
        "m3_hole": {"label": "M3 hole diameter", "unit": "mm"},
    }
    repair = undersize_screw_hole(
        "a plate with an M4 hole and an M3 hole",
        {"m4_hole": 4.0, "m3_hole": 3.0},
        meta,
    )
    assert repair is None
