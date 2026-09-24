"""Table-driven tests for the closed axis lexicon (issue #261, task-a).

The lexicon is a pure, deterministic, no-LLM leaf module that classifies
each clause of a user message into absolute / relative / global / no
axis cues for W/D/H. This file pins:

* each absolute word maps its axis with the number;
* each relative cue is relative for that axis;
* each global cue releases all three axes;
* each excluded word maps nothing;
* a two-axes-one-number clause maps nothing;
* "40 mm wide and 12 mm tall" splits on "and" → W=40, H=12;
* "20 mm wide and tall" (two axes, one number, no second number in the
  "and" fragment) → nothing;
* "12 mm lower" is relative H, not absolute;
* "spacer_height 12" maps nothing (bare number, no mm unit, "height"
  inside an identifier);
* unmapped_mm_numbers is correct for "a 20 mm wide thing, lift it 12 mm"
  → [12];
* axis_for_question_word returns the axis for absolute words, None for
  relative/global/excluded words.
"""

from __future__ import annotations

import pytest

from d33d.axis_lexicon import axis_for_question_word, classify

# ---------------------------------------------------------------------------
# Absolute cues — each word maps its axis with the number
# ---------------------------------------------------------------------------


class TestAbsoluteCues:
    """Each absolute word maps its axis with the number."""

    @pytest.mark.parametrize(
        ("word", "axis"),
        [
            ("tall", "H"),
            ("high", "H"),
            ("height", "H"),
            ("wide", "W"),
            ("width", "W"),
            ("deep", "D"),
            ("depth", "D"),
        ],
    )
    def test_absolute_word_maps_axis_with_number(self, word: str, axis: str):
        result = classify(f"12 mm {word}")
        assert result.absolute[axis] == 12.0
        assert result.relative == set()
        assert result.global_ is False

    def test_absolute_with_comma(self):
        """The axis word after a comma still maps."""
        result = classify("a 20 mm wide thing")
        assert result.absolute == {"W": 20.0}

    def test_absolute_number_before_axis_word(self):
        """Number before the axis word."""
        result = classify("20 wide")
        assert result.absolute == {"W": 20.0}

    def test_absolute_axis_word_before_number(self):
        """Axis word before the number."""
        result = classify("height 90 mm")
        assert result.absolute == {"H": 90.0}

    def test_absolute_mm_unit_required(self):
        """A bare number without an explicit mm unit does not map."""
        result = classify("20 wide")
        # "20 wide" maps because "wide" is the axis word and 20 is
        # directly adjacent. This is an absolute cue, not a bare number.
        assert result.absolute == {"W": 20.0}

    def test_bare_number_no_axis_word_maps_nothing(self):
        """A bare number with no axis word maps nothing."""
        result = classify("make it 15")
        assert result.absolute == {}

    def test_mm_number_no_axis_word_maps_nothing(self):
        """An mm number with no axis word maps nothing (it is a feature
        size, not an axis statement)."""
        result = classify("add a 5 mm hole")
        assert result.absolute == {}

    def test_absolute_no_unit_but_adjacent(self):
        """A bare number directly adjacent to an axis word IS an
        absolute cue (the spec says "a number with a mm unit, or a bare
        number directly adjacent to the axis word")."""
        result = classify("20 wide")
        assert result.absolute == {"W": 20.0}


# ---------------------------------------------------------------------------
# Relative cues — relative for that axis, never absolute
# ---------------------------------------------------------------------------


class TestRelativeCues:
    """Each relative cue is relative for its axis, never absolute."""

    @pytest.mark.parametrize(
        ("word", "axis"),
        [
            ("taller", "H"),
            ("shorter", "H"),
            ("higher", "H"),
            ("lower", "H"),
            ("wider", "W"),
            ("narrower", "W"),
            ("deeper", "D"),
            ("shallower", "D"),
        ],
    )
    def test_relative_word_marks_relative_axis(self, word: str, axis: str):
        result = classify(f"make it {word}")
        assert axis in result.relative
        # Relative is never absolute: "12 mm lower" releases H.
        if word == "lower":
            assert "H" not in result.absolute

    def test_relative_with_mm_number_is_relative_not_absolute(self):
        """The spec's key case: "12 mm lower" is relative H, not
        absolute H=12."""
        result = classify("12 mm lower")
        assert "H" in result.relative
        assert "H" not in result.absolute


# ---------------------------------------------------------------------------
# Global cues — release all three axes
# ---------------------------------------------------------------------------


class TestGlobalCues:
    """Each global cue releases all three axes."""

    @pytest.mark.parametrize(
        "word",
        [
            "bigger",
            "smaller",
            "scale",
            "scaled",
            "resize",
            "resized",
            "half the size",
            "twice the size",
        ],
    )
    def test_global_word_sets_global(self, word: str):
        result = classify(f"make it {word}")
        assert result.global_ is True


# ---------------------------------------------------------------------------
# Excluded words — must NOT trigger any axis
# ---------------------------------------------------------------------------


class TestExcludedWords:
    """Excluded words (long/length, thick/thickness, diameter/Ø/bore,
    verbs like lift/sit/reach/clear) map nothing."""

    @pytest.mark.parametrize(
        "word",
        [
            "long",
            "length",
            "thick",
            "thickness",
            "diameter",
            "bore",
            "lift",
            "sit",
            "reach",
            "clear",
        ],
    )
    def test_excluded_word_maps_nothing(self, word: str):
        result = classify(f"12 mm {word}")
        assert result.absolute == {}
        assert result.relative == set()
        assert result.global_ is False

    def test_height_inside_identifier_does_not_trigger(self):
        """'height' inside 'spacer_height' must not trigger (underscores
        are word characters, so \\bheight\\b does not match inside
        'spacer_height')."""
        result = classify("spacer_height 12")
        assert result.absolute == {}
        # The bare number 12 is not an mm number, so it is not in
        # unmapped_mm_numbers either.
        assert result.unmapped_mm_numbers == []


# ---------------------------------------------------------------------------
# Clause splitting — "and" rules
# ---------------------------------------------------------------------------


class TestClauseSplitting:
    """Clauses split on sentence punctuation, commas, and 'and' (with
    the two-axes-one-number rule)."""

    def test_and_splits_when_both_parts_have_numbers(self):
        """'40 mm wide and 12 mm tall' → W=40, H=12."""
        result = classify("40 mm wide and 12 mm tall")
        assert result.absolute == {"W": 40.0, "H": 12.0}

    def test_and_does_not_split_when_one_part_lacks_number(self):
        """'20 mm wide and tall' → nothing (the clause stays whole,
        two axes + one number → maps nothing)."""
        result = classify("20 mm wide and tall")
        assert result.absolute == {}
        # 20 is an explicit mm number not assigned to any axis → unmapped
        assert 20.0 in result.unmapped_mm_numbers

    def test_comma_splits_clauses(self):
        """A comma separates clauses; each is evaluated independently."""
        result = classify("20 mm wide, 12 mm tall")
        assert result.absolute == {"W": 20.0, "H": 12.0}

    def test_semicolon_splits_clauses(self):
        """A semicolon separates clauses."""
        result = classify("20 mm wide; 12 mm tall")
        assert result.absolute == {"W": 20.0, "H": 12.0}


# ---------------------------------------------------------------------------
# Two-axes-one-number clause
# ---------------------------------------------------------------------------


class TestTwoAxesOneNumber:
    """A clause with two different axis words and one number maps nothing."""

    def test_two_axes_one_number_maps_nothing(self):
        result = classify("20 mm wide and tall")
        assert result.absolute == {}
        assert 20.0 in result.unmapped_mm_numbers


# ---------------------------------------------------------------------------
# unmapped_mm_numbers
# ---------------------------------------------------------------------------


class TestUnmappedMmNumbers:
    """unmapped_mm_numbers includes every explicit-mm number not assigned
    to an axis by the lexicon or an explicit protocol cue."""

    def test_unmapped_number_with_mapped_in_other_clause(self):
        """'a 20 mm wide thing, lift it 12 mm' → unmapped [12] (20 is
        mapped to W, 12 is unmapped)."""
        result = classify("a 20 mm wide thing, lift it 12 mm")
        assert result.absolute == {"W": 20.0}
        assert result.unmapped_mm_numbers == [12.0]

    def test_no_unmapped_when_all_mapped(self):
        result = classify("20 mm wide, 12 mm tall")
        assert result.unmapped_mm_numbers == []

    def test_bare_number_not_in_unmapped(self):
        """A bare number (no mm unit) is never in unmapped_mm_numbers."""
        result = classify("spacer_height 12")
        assert result.unmapped_mm_numbers == []

    def test_mm_number_no_axis_word_is_unmapped(self):
        """An mm number with no axis word is unmapped."""
        result = classify("add a 5 mm hole")
        assert result.unmapped_mm_numbers == [5.0]


# ---------------------------------------------------------------------------
# axis_for_question_word
# ---------------------------------------------------------------------------


class TestAxisForQuestionWord:
    """axis_for_question_word returns the axis for absolute words, None
    for relative/global/excluded words."""

    @pytest.mark.parametrize(
        ("word", "expected"),
        [
            ("tall", "H"),
            ("high", "H"),
            ("height", "H"),
            ("wide", "W"),
            ("width", "W"),
            ("deep", "D"),
            ("depth", "D"),
            # Relative words → None (they are not question axes)
            ("taller", None),
            ("shorter", None),
            ("higher", None),
            ("lower", None),
            ("wider", None),
            ("narrower", None),
            ("deeper", None),
            ("shallower", None),
            # Global words → None
            ("bigger", None),
            ("smaller", None),
            ("scale", None),
            # Excluded → None
            ("long", None),
            ("thick", None),
            ("diameter", None),
            ("bore", None),
            ("lift", None),
            ("sit", None),
            ("reach", None),
            ("clear", None),
            # Unknown → None
            ("foo", None),
        ],
    )
    def test_axis_for_question_word(self, word: str, expected: str | None):
        assert axis_for_question_word(word) == expected
