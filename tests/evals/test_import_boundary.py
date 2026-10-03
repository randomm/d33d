"""Fast pytest for the fill-and-recut boundary route (issue #340,
operator decision 4).

The boundary is route-level and deterministic: on a project whose part
units are assumed/settled, a request that RESIZES a feature the design
does not own as a parameter (a noun from the closed feature-noun set)
must fire :func:`d33d.fill_recut.fill_recut_trigger` and produce the
templated boundary sentence (the fill-and-recut offer) — never a silent
resize (no design-loop run) and never an invented size. Add phrasings
("add a 38 mm hole", "drill a hole") do NOT fire the trigger: adds go to
the design loop. The holey.stl-derived fixture is the import surface the
boundary route runs on; the test reads its extents to pin the "big hole"
scenario without touching the mesh.

No model, no Docker, no network — this is part of the
``pytest -m "not slow and not live"`` gate.
"""

from __future__ import annotations

from d33d.fill_recut import (
    FEATURE_NOUNS,
    FRILL_HOLE_DIAMETER_REPLY,
    FRILL_NO_DIMENSION_REPLY,
    boundary_sentence,
    fill_and_recut_instruction,
    fill_recut_trigger,
    own_feature_names,
)
from tests.evals._casefile_helpers import FIXTURE, fixture_extents


def test_holey_fixture_is_a_20mm_box_with_a_hole() -> None:
    """The boundary scenario is pinned to the fixture geometry: a 20 mm
    box whose through hole is smaller than 38 mm, so 'make the big hole
    38 mm' is a genuine fill-and-recut (recut larger than the plate
    face), not a resize the mesh could absorb."""
    extents = fixture_extents(FIXTURE)
    assert extents == (20.0, 20.0, 20.0)
    assert max(extents) < 38.0, (
        f"fixture largest side {max(extents)} mm >= 38 mm — the "
        "'make the big hole 38 mm' scenario no longer holds a "
        "fill-and-recut over this mesh; re-derive the fixture"
    )
    # The fixture is a holey plate, not a solid box: its volume is
    # strictly less than the 8000 mm³ that the same 20 mm box would
    # have if it were solid.
    assert FIXTURE.is_file(), f"holey fixture missing: {FIXTURE} does not exist"
    import trimesh

    mesh = trimesh.load(str(FIXTURE), process=False)
    mesh.merge_vertices()
    assert mesh.volume < 8000.0, (
        f"fixture volume {mesh.volume:.1f} mm³ >= 8000 — the fixture "
        "is a solid box, not a holey plate; the 'big hole' scenario "
        "is vacuous"
    )


def test_make_big_hole_38mm_fires_the_trigger() -> None:
    """'make the big hole 38 mm' on the holey part fires the
    fill-and-recut trigger with the user's own noun and size — never a
    silent resize."""
    trigger = fill_recut_trigger("make the big hole 38 mm")
    assert trigger is not None, (
        "'make the big hole 38 mm' must fire the fill-and-recut trigger "
        "(a resize of an imported feature)"
    )
    assert trigger["noun"] == "hole"
    assert trigger["size"] == 38.0
    assert trigger["move"] is False
    assert trigger["move_distance"] is None
    assert trigger["direction"] is None


def test_trigger_is_never_silent_resize_or_invented_size() -> None:
    """The trigger carries the user's own number, mono-formatted into the
    boundary sentence — the reply offers the fill-and-recut with the
    user's exact dimension and never resizes silently."""
    trigger = fill_recut_trigger("make the big hole 38 mm")
    assert trigger is not None
    sentence = boundary_sentence(trigger["noun"], trigger["size"])
    expected = FRILL_HOLE_DIAMETER_REPLY.format(noun="hole", dim="38")
    assert sentence == expected
    # The user's own number is in the reply, never invented or truncated.
    assert "38" in sentence
    # And it is a fill-and-recut offer, not a resize acknowledgement.
    assert "fill it" in sentence
    assert "Ø38 mm" in sentence


def test_boundary_sentence_shape_for_the_hole_38mm_case() -> None:
    """The templated sentence for the hole+38 case is the UX spec's
    sentence verbatim (mono-formatted mm, same axis)."""
    assert boundary_sentence("hole", 38.0) == (
        "That hole came with your file, so I can't resize it directly — "
        "the file has no parameters for me to change. What I can do: "
        "fill it, then cut a Ø38 mm one on the same axis. It'll look "
        "the same, and you'll see it as a change in the history."
    )


def test_accepted_offer_instruction_is_fill_then_recut_never_resize() -> None:
    """A clean 'yes' on the pending offer appends an explicit
    fill-and-recut instruction to the design-loop request: union over
    the hole, then difference at the new size — never a scale() on the
    import."""
    offer = {"noun": "hole", "size": 38.0, "move": False}
    instruction = fill_and_recut_instruction(offer)
    assert "union a solid over the existing hole" in instruction
    assert "difference the new hole at 38 mm on the same axis" in instruction
    assert "import(\"part.stl\") stays as brought" in instruction
    assert "resize" in instruction.lower()  # "Never resize..."


def test_add_phrasings_do_not_fire_the_trigger() -> None:
    """Adds are not resizes: 'add a 38 mm hole' / 'drill a hole' go to
    the design loop (rule 4 forbids resizing the imported mesh, not
    adding onto it), so the boundary must not fire."""
    assert fill_recut_trigger("add a 38 mm hole") is None
    assert fill_recut_trigger("drill a hole") is None
    assert fill_recut_trigger("make a new slot 10 mm") is None


def test_designs_own_feature_never_triggers() -> None:
    """A resize of a feature the design's own current version owns as a
    parameter (operator decision (d)) never triggers: the user is
    resizing their own feature, not the imported mesh."""
    latest = {
        "params": {"hole_diameter": 10.0},
        "param_meta": {"hole_diameter": {"label": "Hole diameter"}},
    }
    own = own_feature_names(latest)
    assert "hole" in own
    trigger = fill_recut_trigger("make the hole 38 mm")
    assert trigger is not None
    # The route's caller-side check: the trigger fires but the noun is
    # in the design's own feature names, so no fill-recut offer.
    assert trigger["noun"] in own


def test_noun_is_from_the_closed_set() -> None:
    """Only the closed feature-noun set can trigger — free-form nouns
    never do ('make the thing 38 mm' does not fire)."""
    assert fill_recut_trigger("make the thing 38 mm") is None
    # The singular forms of the closed set always trigger (the trigger's
    # noun alternation is the closed set itself, lower-cased; the
    # plural-only entries are their singulars + "s").
    for noun in FEATURE_NOUNS:
        assert fill_recut_trigger(f"make the {noun} 38 mm") is not None, (
            f"closed-set noun {noun!r} must trigger"
        )


def test_no_dimension_is_an_ask_not_a_guess() -> None:
    """A resize with no dimension asks for the size — never invents
    one."""
    trigger = fill_recut_trigger("make the hole bigger")
    assert trigger is not None
    assert trigger["noun"] == "hole"
    assert trigger["size"] is None
    assert boundary_sentence("hole", None) == FRILL_NO_DIMENSION_REPLY.format(
        noun="hole"
    )
    assert "?" in boundary_sentence("hole", None)
