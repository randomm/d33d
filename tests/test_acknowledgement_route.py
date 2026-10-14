"""Issue #449: the one acknowledgement decision, pinned as a table.

``acknowledgement_route`` is the single home of the #449 precedence. This
module pins its outcome over phrase x measured x live x trigger, so a change
to the precedence fails here first, in one place.
"""

from __future__ import annotations

import pytest

from d33d.acknowledgement import acknowledgement_route
from d33d.fill_recut import fill_recut_trigger, is_bare_affirmation, is_literal_yes

MEASURED = {"report": {"holes": [{"diameter": 5.0}]}}
UNMEASURED = {"report": {"hole_count": 2}}
TRIGGER = {"noun": "hole", "size": 5.0, "move": False, "move_distance": None, "direction": None}

# phrase -> (literal yes?, bare affirmation?)
# Expected outcome per (measured, live): the columns of the table below.
#   (not live, measured) (not live, unmeasured) (live, measured) (live, unmeasured)
TABLE = {
    "yes": ("point_first", "no_offer", "point_first", None),
    "yes please": ("point_first", "no_offer", "point_first", None),
    "ok thanks": ("no_offer", "no_offer", "point_first", None),
    "yes, add a chamfer here": (None, None, None, None),
    "what is this?": (None, None, None, None),
}


def test_premises_hold() -> None:
    """The phrase classes are what the table assumes."""
    assert is_literal_yes("yes") and is_bare_affirmation("yes")
    assert is_literal_yes("yes please") and is_bare_affirmation("yes please")
    assert not is_literal_yes("ok thanks") and is_bare_affirmation("ok thanks")
    assert not is_bare_affirmation("yes, add a chamfer here")
    assert not is_bare_affirmation("what is this?")
    assert fill_recut_trigger("make the hole 6 mm") is not None


@pytest.mark.parametrize("text", list(TABLE))
@pytest.mark.parametrize(
    ("part", "live", "column"),
    [
        (MEASURED, False, 0),
        (UNMEASURED, False, 1),
        (MEASURED, True, 2),
        (UNMEASURED, True, 3),
        (None, False, 1),
        (None, True, 3),
    ],
    ids=[
        "not_live_measured",
        "not_live_unmeasured",
        "live_measured",
        "live_unmeasured",
        "not_live_no_part",
        "live_no_part",
    ],
)
def test_route_matrix_without_trigger(text, part, live, column) -> None:
    expected = TABLE[text][column]
    assert acknowledgement_route(text, part, live=live, trigger=None) == expected


@pytest.mark.parametrize("text", list(TABLE))
@pytest.mark.parametrize("part", [MEASURED, UNMEASURED, None], ids=["measured", "unmeasured", "no_part"])
@pytest.mark.parametrize("live", [False, True], ids=["not_live", "live"])
def test_a_trigger_is_never_an_acknowledgement(text, part, live) -> None:
    """A resize/move trigger wins over every acknowledgement state."""
    assert acknowledgement_route(text, part, live=live, trigger=TRIGGER) is None


def test_live_suppresses_the_no_offer_notice() -> None:
    """With a live offer a bare affirmation is never 'no_offer'."""
    for text in TABLE:
        for part in (MEASURED, UNMEASURED, None):
            assert acknowledgement_route(text, part, live=True, trigger=None) != "no_offer"


def test_literal_yes_is_point_first_on_measured_only_when_not_live() -> None:
    """Not live: literal yes is point-first on a measured part only; on an
    unmeasured part it gets the notice (the #349 reply)."""
    assert acknowledgement_route("yes", MEASURED, live=False, trigger=None) == "point_first"
    assert acknowledgement_route("yes", UNMEASURED, live=False, trigger=None) == "no_offer"
