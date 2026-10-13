"""Issue #437: positional hole resolution, viewer-pick nearest hole, and
one-decimal mm formatting (the Python side of the parity pin).

The fixture holes are the QA v36 plate: bounds 0..120 x 0..80 (mm), holes
at (8, 72), (112, 8) and (112, 72), all Ø4.
"""

from __future__ import annotations

import pytest

from d33d.hole_position import (
    nearest_hole_to_point,
    positional_words,
    select_positional_hole,
)
from d33d.hole_select import select_measured_hole
from d33d.mm_format import fmt_mm1

BOUNDS = [[0.0, 0.0], [120.0, 80.0]]


def _hole(x: float, y: float, d: float = 4.0) -> dict:
    return {"center": [x, y, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": d}


PLATE = [_hole(8, 72), _hole(112, 8), _hole(112, 72)]


@pytest.mark.parametrize(
    ("message", "centre"),
    [
        ("make the top right hole 6 mm", [112.0, 72.0]),
        ("make the top-right hole 6 mm", [112.0, 72.0]),
        ("make the upper right hole 6 mm", [112.0, 72.0]),
        ("make the bottom right hole 6 mm", [112.0, 8.0]),
        ("make the lower right hole 6 mm", [112.0, 8.0]),
        ("make the top left hole 6 mm", [8.0, 72.0]),
        ("make the left hole 6 mm", [8.0, 72.0]),
        # Bottom-left corner (0, 0): (8, 72) is ~72 mm away, (112, 8) ~112 mm
        # — a clear nearest, not a tie.
        ("make the bottom left hole 6 mm", [8.0, 72.0]),
    ],
)
def test_positional_phrase_selects_expected_fixture_hole(message, centre) -> None:
    hole = select_positional_hole(PLATE, message, BOUNDS)
    assert hole is not None, message
    assert hole["center"][:2] == centre, message


def test_top_right_is_112_72_and_bottom_right_is_112_8() -> None:
    """The acceptance pins, stated plainly."""
    assert select_positional_hole(PLATE, "top right", BOUNDS)["center"][:2] == [112.0, 72.0]
    assert select_positional_hole(PLATE, "bottom right", BOUNDS)["center"][:2] == [112.0, 8.0]
    assert select_positional_hole(PLATE, "top left", BOUNDS)["center"][:2] == [8.0, 72.0]


def test_corner_one_is_ambiguous_on_the_symmetric_plate() -> None:
    """"the corner one": every fixture hole is 11.3 mm from its nearest
    bounds corner — a three-way tie → point-at (None), never a guess."""
    assert select_positional_hole(PLATE, "the corner one", BOUNDS) is None
    assert select_positional_hole(PLATE, "make the corner hole 6 mm", BOUNDS) is None


def test_edge_phrase_tie_is_point_at() -> None:
    """"top": (8, 72) and (112, 72) are both 8 mm from the top edge →
    a within-1-mm tie → None."""
    assert select_positional_hole(PLATE, "the top one", BOUNDS) is None


def test_near_tie_within_one_mm_is_point_at() -> None:
    """Two holes whose distances to the reference corner differ by less
    than 1 mm are a tie, not a pick."""
    holes = [_hole(10.0, 70.0), _hole(10.5, 70.0)]
    assert select_positional_hole(holes, "top left", BOUNDS) is None


def test_no_position_word_is_not_positional() -> None:
    assert positional_words("make the hole 6 mm") is None
    assert select_positional_hole(PLATE, "make the hole 6 mm", BOUNDS) is None


def test_missing_or_malformed_bounds_is_point_at() -> None:
    assert select_positional_hole(PLATE, "top right", None) is None
    assert select_positional_hole(PLATE, "top right", [[0.0], [1.0]]) is None
    assert select_positional_hole(PLATE, "top right", [[0.0, 0.0], [float("nan"), 1.0]]) is None


def test_positional_ignores_the_centre_threshold() -> None:
    """A single far-away hole is still "the top right" one — the #414
    30 mm centre threshold does not apply to positional phrases."""
    holes = [_hole(112.0, 72.0), _hole(60.0, 40.0)]
    chosen = select_positional_hole(holes, "top right", BOUNDS)
    assert chosen is not None and chosen["center"][:2] == [112.0, 72.0]


def test_select_measured_hole_positional_precedes_diameter() -> None:
    """"the top right hole 6 mm" resolves by position, not by the closest
    diameter (every fixture hole is Ø4, so the diameter rule would tie)."""
    chosen = select_measured_hole(PLATE, "make the top right hole 6 mm", None,
                                  trigger_size=6.0, bounds_mm=BOUNDS)
    assert chosen is not None and chosen["center"][:2] == [112.0, 72.0]


def test_select_measured_hole_positional_without_bounds_is_point_at() -> None:
    assert select_measured_hole(PLATE, "make the top right hole 6 mm", None,
                                trigger_size=6.0, bounds_mm=None) is None


def test_nearest_hole_to_viewer_pick() -> None:
    assert nearest_hole_to_point(PLATE, [110.0, 70.0, 3.0])["center"][:2] == [112.0, 72.0]
    assert nearest_hole_to_point(PLATE, [9.0, 71.0, 0.0])["center"][:2] == [8.0, 72.0]


def test_nearest_hole_to_viewer_pick_between_holes_is_none() -> None:
    holes = [_hole(0.0, 0.0), _hole(2.0, 0.0)]
    assert nearest_hole_to_point(holes, [1.0, 0.0, 0.0]) is None


def test_nearest_hole_to_far_viewer_pick_is_none() -> None:
    """A pick clearly nearer one hole but far from it (~52 mm) names no
    hole \u2014 no proximity-free offer for a distant hole."""
    assert nearest_hole_to_point(PLATE, [60.0, 10.0, 0.0]) is None


def test_nearest_hole_to_viewer_pick_without_point_is_none() -> None:
    assert nearest_hole_to_point(PLATE, None) is None
    assert nearest_hole_to_point(PLATE, [1.0]) is None
    assert nearest_hole_to_point(PLATE, [float("inf"), 0.0, 0.0]) is None


# Issue #437 — one-decimal half-up formatting (the Python side of the
# parity pin; the TS twin is ``fmtMm1`` / ``dia`` in web/src/copy.ts).
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (3.99429, "4.0"),
        (4, "4.0"),
        (3.25, "3.3"),
        (3.35, "3.4"),
        (38, "38.0"),
    ],
)
def test_fmt_mm1_shared_sample_values(value: float, expected: str) -> None:
    assert fmt_mm1(value) == expected


def test_one_decimal_copy_is_only_for_a_resolved_measured_hole() -> None:
    """Operator decision (2026-10-12): the #351 unknown-count offer keeps its
    copy ("\u00d838 mm"); the one-decimal diameter is for a resolved measured
    hole only."""
    from d33d.fill_recut import boundary_sentence

    assert "\u00d838 mm" in boundary_sentence("hole", 38.0)
    assert "\u00d838.0 mm" in boundary_sentence("hole", 38.0, measured=True)


def test_fmt_mm1_is_half_up_on_the_typed_decimal() -> None:
    """Half-up on the decimal the user typed — never binary rounding
    (Python's own ``round(3.25, 1)`` gives 3.2)."""
    assert fmt_mm1(2.45) == "2.5"
    assert fmt_mm1(3.25) == "3.3"
    assert round(3.25, 1) == 3.2  # Python's round() is banker's: 3.2
