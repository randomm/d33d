"""Unit tests for d33d.hole_select (issue #396 lens fixes)."""

from __future__ import annotations

from d33d.hole_select import NoMatchHole, holes_in_mm, select_measured_hole

# ---------------------------------------------------------------------------
# holes_in_mm — scale guards (issue #396 lens item 2)
# ---------------------------------------------------------------------------

def test_holes_in_mm_missing_scale_returns_empty():
    """A missing scale (``None``) → empty list, never file units."""
    report = {
        "holes": [
            {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 5.0},
        ]
    }
    assert holes_in_mm(report, None) == []


def test_holes_in_mm_non_numeric_scale_returns_empty():
    """A non-numeric scale → empty list."""
    report = {
        "holes": [
            {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 5.0},
        ]
    }
    assert holes_in_mm(report, "abc") == []
    assert holes_in_mm(report, [1.0, 2.0]) == []


def test_holes_in_mm_zero_or_negative_scale_returns_empty():
    """A scale of 0 or negative → empty list."""
    report = {
        "holes": [
            {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 5.0},
        ]
    }
    assert holes_in_mm(report, 0.0) == []
    assert holes_in_mm(report, -1.0) == []


def test_holes_in_mm_positive_scale_converts_to_mm():
    """A positive scale → holes in mm."""
    report = {
        "holes": [
            {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 5.0},
        ]
    }
    result = holes_in_mm(report, 10.0)
    assert len(result) == 1
    assert abs(result[0]["center"][0] - 10.0) < 1e-6
    assert abs(result[0]["center"][1] - 20.0) < 1e-6
    assert abs(result[0]["diameter_mm"] - 50.0) < 1e-6


def test_holes_in_mm_non_numeric_center_component_omitted_not_raised():
    """A hole entry whose ``center`` has a non-numeric component is
    OMITTED (the contract: malformed entries are omitted), never a
    ValueError that would 500 the fill-recut call path."""
    report = {
        "holes": [
            {
                "center": ["a", 1.0, 2.0],
                "axis": [0.0, 0.0, 1.0],
                "diameter_mm": 5.0,
            },
            {
                "center": [1.0, 2.0, 0.5],
                "axis": [0.0, 0.0, 1.0],
                "diameter_mm": 5.0,
            },
        ]
    }
    result = holes_in_mm(report, 10.0)
    # The corrupt entry is omitted; the good entry is converted.
    assert len(result) == 1
    assert abs(result[0]["center"][0] - 10.0) < 1e-6


def test_holes_in_mm_only_corrupt_center_returns_empty():
    """A report whose only entry has a corrupt center → ``[]`` (never
    a raise, never a partial value)."""
    report = {
        "holes": [
            {"center": ["a", 1, 2], "axis": [0.0, 0.0, 1.0], "diameter_mm": 5.0},
        ]
    }
    assert holes_in_mm(report, 1.0) == []


def test_holes_in_mm_non_numeric_axis_degrades_to_default():
    """A hole entry whose ``axis`` has a non-numeric component degrades
    to the default ``[0, 0, 1]`` axis (the axis is unit — a corrupt
    axis is no evidence, not a malformed entry). The entry is NOT
    omitted — the center and diameter are still valid."""
    report = {
        "holes": [
            {
                "center": [1.0, 2.0, 0.5],
                "axis": ["x", "y", "z"],
                "diameter_mm": 5.0,
            },
        ]
    }
    result = holes_in_mm(report, 10.0)
    assert len(result) == 1
    assert result[0]["axis"] == [0.0, 0.0, 1.0]


# ---------------------------------------------------------------------------
# select_measured_hole — center qualifier with no usable bbox (item 3)
# ---------------------------------------------------------------------------

def test_select_measured_hole_center_qualifier_no_bbox_returns_none():
    """A 'center' qualifier with ``bbox_mm=None`` must return ``None``
    (the point-at fallback), NOT silently switch to diameter matching.
    The old code would fall through to the diameter branch when ``bbox_mm``
    was ``None`` — this pins that it does NOT."""
    holes = [
        {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        {"center": [5.0, 6.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 30.0},
    ]
    # "center hole 30 mm" — the center qualifier + a stated size.
    # With no bbox, the center rule cannot run. The old code would
    # fall through to rule 3 (diameter) and pick the 30 mm hole.
    # The correct behaviour: return None (point-at fallback).
    result = select_measured_hole(
        holes, "make the center hole 30 mm", bbox_mm=None, trigger_size=30.0
    )
    assert result is None, (
        f"center qualifier with no usable bbox must return None "
        f"(point-at fallback), not silently switch to diameter matching; "
        f"got {result}"
    )


def test_select_measured_hole_center_qualifier_no_hole_with_center_returns_none():
    """A 'center' qualifier where NO hole has a usable centre also
    returns ``None`` (point-at fallback), not diameter matching."""
    holes = [
        {"center": None, "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        {"center": None, "axis": [0.0, 0.0, 1.0], "diameter_mm": 30.0},
    ]
    result = select_measured_hole(
        holes, "make the center hole 30 mm", bbox_mm=[20.0, 10.0], trigger_size=30.0
    )
    assert result is None


def test_select_measured_hole_no_qualifier_diameter_match_still_works():
    """Without a center qualifier, the diameter match still works
    (the point-at fallback is only for the center-qualifier-with-no-bbox
    case, not for all cases)."""
    holes = [
        {"center": [1.0, 2.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        {"center": [5.0, 6.0, 0.5], "axis": [0.0, 0.0, 1.0], "diameter_mm": 30.0},
    ]
    # "make the hole 30 mm" — no center qualifier, stated size 30.
    result = select_measured_hole(
        holes, "make the hole 30 mm", bbox_mm=None, trigger_size=30.0
    )
    assert result is not None
    assert abs(result["diameter_mm"] - 30.0) < 1e-6


# ---------------------------------------------------------------------------
# select_measured_hole — center qualifier, no measured hole matches
# (issue #414)
# ---------------------------------------------------------------------------

def test_select_measured_hole_center_qualifier_no_match_returns_no_match():
    """Issue #414: a 'center' qualifier where NO measured hole is anywhere
    near the bbox centre must return the :class:`NoMatchHole` sentinel —
    NOT the nearest hole (the silent wrong pick QA flagged) and NOT
    ``None`` (which is the point-at fallback for a genuinely ambiguous
    pick). The caller uses the sentinel to say "I don't see a center
    hole" and list the measured holes, instead of making an offer for a
    hole that does not exist."""
    # The QA v36 shape: three holes at the corners, none anywhere near
    # the centre. bbox [120, 80] → centre (60, 40); the nearest hole is
    # (8, 72) at 61 mm — far beyond the 30 mm match threshold.
    holes = [
        {"center": [8.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [112.0, 8.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [112.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
    ]
    result = select_measured_hole(
        holes, "make the center hole 38 mm", bbox_mm=[120.0, 80.0], trigger_size=38.0
    )
    # Distances to the centre (60, 40): (8,72)→61.1, (112,8)→61.1, (112,72)
    # →61.1 mm — all far beyond the 30 mm match threshold.
    assert isinstance(result, NoMatchHole), (
        f"a 'center hole' no measured hole sits at must be NoMatchHole, "
        f"got {result!r}"
    )
    # It must be the SENTINEL (so the no-match copy fires), not None
    # (the point-at fallback) and not a hole dict (the silent pick).
    assert result is not None
    assert not isinstance(result, dict)


def test_select_measured_hole_center_qualifier_match_within_threshold_picks_hole():
    """Issue #414: the no-match signal must NOT fire when a measured hole
    IS near the centre — a 'center hole' with a hole actually at the
    centre picks it (the #396 behaviour, unchanged by #414)."""
    # A hole at (60, 40) = the bbox centre (dist 0), two corner holes.
    holes = [
        {"center": [8.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [60.0, 40.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 10.0},
        {"center": [112.0, 8.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
    ]
    result = select_measured_hole(
        holes, "make the center hole 38 mm", bbox_mm=[120.0, 80.0], trigger_size=38.0
    )
    assert isinstance(result, dict), (
        f"a measured hole at the centre must be picked, got {result!r}"
    )
    assert abs(result["diameter_mm"] - 10.0) < 1e-6, result


def test_select_measured_hole_center_qualifier_ambiguous_still_none_not_no_match():
    """Issue #414: two equally-near candidates within the ambiguity band
    stay ``None`` (the point-at fallback) — the no-match sentinel is for
    "nothing is near the centre", not for an ambiguous pick."""
    # Two holes equidistant from the centre (both within the 30 mm
    # threshold): (50, 40) and (70, 40) — 10 mm each from (60, 40), 0
    # apart in rank.
    holes = [
        {"center": [50.0, 40.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 8.0},
        {"center": [70.0, 40.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 8.0},
    ]
    result = select_measured_hole(
        holes, "make the center hole 38 mm", bbox_mm=[120.0, 80.0], trigger_size=38.0
    )
    assert result is None, (
        f"two equally-near candidates must stay None (point-at), got {result!r}"
    )


# ---------------------------------------------------------------------------
# DRY: _snap_axis single definition (item 7)
# ---------------------------------------------------------------------------

def test_snap_axis_single_definition_no_duplicate():
    """The ``_snap_axis`` function must exist in exactly ONE module as a
    top-level definition (``d33d.part_holes``), with ``d33d.hole_measure``
    importing it locally (the DRY fix, issue #396). A second top-level
    definition in ``hole_measure`` would be the duplication the fix
    addresses."""
    import d33d.hole_measure as hm
    import d33d.part_holes as ph

    # ``d33d.part_holes`` has the canonical top-level definition.
    ph_has_own = "_snap_axis" in ph.__dict__ and callable(ph.__dict__["_snap_axis"])
    assert ph_has_own, (
        "d33d.part_holes must have a top-level _snap_axis definition"
    )

    # ``d33d.hole_measure`` must NOT have its own top-level definition
    # (it imports it locally inside ``measure_genus_holes`` to avoid a
    # circular import).
    hm_has_own = "_snap_axis" in hm.__dict__ and callable(hm.__dict__["_snap_axis"])
    assert not hm_has_own, (
        "d33d.hole_measure must not have its own top-level _snap_axis "
        "definition (DRY: it imports from d33d.part_holes)"
    )
