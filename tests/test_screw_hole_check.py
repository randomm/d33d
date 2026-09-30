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
    # Returns the DETECTION tuple (size, value, clearance, label) — the
    # caller (design_loop) folds it into the repair dict.
    det = undersize_screw_hole(REQUEST, {"W": 60.0, "hole_d": 4.0}, META)
    assert det is not None
    size, value, clearance, label = det
    assert size == "M4"
    assert value == 4.0
    assert clearance == 4.5
    assert label == "M4 hole diameter"


def test_passes_at_clearance():
    assert undersize_screw_hole(REQUEST, {"hole_d": 4.5}, META) is None


def test_passes_above_clearance():
    assert undersize_screw_hole(REQUEST, {"hole_d": 5.0}, META) is None


def test_boundary_005_mm_abstains():
    # 4.5 - 4.45 = 0.05, NOT more than 0.05 → abstain.
    assert undersize_screw_hole(REQUEST, {"hole_d": 4.45}, META) is None


def test_boundary_005_plus_epsilon_triggers():
    # 4.5 - 4.44 = 0.06 > 0.05 → trigger.
    det = undersize_screw_hole(REQUEST, {"hole_d": 4.44}, META)
    assert det is not None
    assert det[1] == 4.44


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


def test_no_param_meta_and_no_screw_framing_abstains():
    """Adversarial finding: when a param has NO ``param_meta``, the
    check may fire only if the REQUEST also frames the screw size as a
    hole ("hole", "holes", "through", "clearance", "screw hole",
    "mounting hole", or "for M4 screws" / "for an M4 screw").  A request
    that does NOT frame a hole (e.g. "an M4 nut trap") must NOT fire —
    the check must never guess which param a named screw sizes."""
    # No meta + no hole-framing → abstain.
    assert undersize_screw_hole("an M4 nut trap", {"m4_hole": 4.0}, {}) is None
    assert undersize_screw_hole(
        "a plate with an M4 bolt head recess", {"m4_hole": 4.0}, {}
    ) is None
    assert undersize_screw_hole(
        "a bracket with an M4 fastener", {"m4_hole": 4.0}, {}
    ) is None
    # No meta + hole-framing present → fires (the request frames a hole).
    assert undersize_screw_hole(REQUEST, {"hole_d": 4.0}, {}) is not None
    assert undersize_screw_hole(
        "a plate with holes for M4 screws", {"hole_d": 4.0}, {}
    ) is not None
    assert undersize_screw_hole(
        "a plate with a through hole for an M4 screw", {"hole_d": 4.0}, {}
    ) is not None
    assert undersize_screw_hole(
        "a plate with a mounting hole for M4", {"hole_d": 4.0}, {}
    ) is not None
    # With meta (meta says hole), the hole-framing is not required.
    meta_m4 = {"m4_hole": {"label": "M4 hole diameter", "unit": "mm"}}
    assert (
        undersize_screw_hole("an M4 nut trap", {"m4_hole": 4.0}, meta_m4) is not None
    )


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
    det = undersize_screw_hole(
        "a plate with an M4x20 clearance hole", {"hole_d": 4.0}, meta
    )
    assert det is not None
    assert det[0] == "M4"


def test_missing_param_meta_name_heuristic_fallback():
    # No metadata — the name "hole_d" reads as a hole AND the request
    # frames a screw ("M4 hole"), so the check fires and the label is
    # the raw name.
    det = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, {})
    assert det is not None
    assert det[3] == "hole_d"


def test_non_numeric_param_value_ignored():
    # A non-numeric value in params is skipped by the check.
    params: dict[str, object] = {"hole_d": 4.0, "bad": "not a number"}
    det = undersize_screw_hole(REQUEST, params, META)
    assert det is not None  # hole_d still triggers
    # The label uses the META label (not the raw name).
    assert det[3] == "M4 hole diameter"


def test_label_uses_label_when_present():
    det = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, META)
    assert det is not None
    assert det[3] == "M4 hole diameter"


def test_label_uses_name_when_no_label():
    det = undersize_screw_hole(REQUEST, {"hole_d": 4.0}, {})
    assert det is not None
    assert det[3] == "hole_d"


def test_m3_hole_at_3_0_triggers():
    """M3 clearance is 3.4 mm; a 3.0 mm hole is below by 0.4 mm → trigger."""
    meta = {"m3_hole": {"label": "M3 hole diameter", "unit": "mm"}}
    det = undersize_screw_hole(
        "a plate with an M3 hole", {"m3_hole": 3.0}, meta
    )
    assert det is not None
    assert det[0] == "M3"
    assert det[1] == 3.0
    assert det[2] == 3.4


def test_m8_hole_at_8_5_triggers():
    """M8 clearance is 9.0 mm; an 8.5 mm hole is below by 0.5 mm → trigger."""
    meta = {"m8_hole": {"label": "M8 hole diameter", "unit": "mm"}}
    det = undersize_screw_hole(
        "a plate with an M8 hole", {"m8_hole": 8.5}, meta
    )
    assert det is not None
    assert det[0] == "M8"
    assert det[1] == 8.5
    assert det[2] == 9.0


def test_m2_5_hole_at_2_5_triggers():
    """M2.5 clearance is 2.9 mm; a 2.5 mm hole is below by 0.4 mm →
    trigger. The regex must match M2.5 (not M2)."""
    meta = {"m25_hole": {"label": "M2.5 hole diameter", "unit": "mm"}}
    det = undersize_screw_hole(
        "a plate with an M2.5 hole", {"m25_hole": 2.5}, meta
    )
    assert det is not None
    assert det[0] == "M2.5"


def test_two_screw_sizes_named_single_undersize_triggers():
    """If the request names two screw sizes and one param is undersize
    for ONE of them, the check fires (one (param, size) pair).  If a
    param is undersize for BOTH sizes, the check sees two entries
    (same param, different sizes) → abstain (ambiguous)."""
    meta = {
        "m4_hole": {"label": "M4 hole diameter", "unit": "mm"},
        "m3_hole": {"label": "M3 hole diameter", "unit": "mm"},
    }
    # m4_hole at 4.0: undersize for M4 (4.5-4.0=0.5>0.05) but NOT for M3
    # (3.4-4.0 is negative).  One entry → fires.
    det = undersize_screw_hole(
        "a plate with an M4 hole and an M3 hole",
        {"m4_hole": 4.0},
        meta,
    )
    assert det is not None
    assert det[0] == "M4"
    # m3_hole at 3.0: undersize for M3 (3.4-3.0=0.4>0.05) AND for M4
    # (4.5-3.0=1.5>0.05) — two entries → abstain (ambiguous).
    assert undersize_screw_hole(
        "a plate with an M4 hole and an M3 hole",
        {"m3_hole": 3.0},
        meta,
    ) is None


def test_two_undersize_params_abstains():
    """If two different params each have an undersize hole, the check
    cannot identify which one to repair → abstain."""
    meta = {
        "m4_hole": {"label": "M4 hole diameter", "unit": "mm"},
        "m3_hole": {"label": "M3 hole diameter", "unit": "mm"},
    }
    assert undersize_screw_hole(
        "a plate with an M4 hole and an M3 hole",
        {"m4_hole": 4.0, "m3_hole": 3.0},
        meta,
    ) is None
