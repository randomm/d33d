"""tests/test_stack_height_check.py — the stack-height post-check (issue
#409).

The v65 repro: a lid written as a stack of height-named features
(plate 4 + rim drop 2 + skirt 5 = 11 mm) where the model committed the
stack with ``difference()`` instead of a union — the skirt was
subtracted from a plate it doesn't touch, the render is a flat 4 mm
slab, and the model's own declared stack
(``skirt_total_height = base_height + skirt_height``) says 11 mm.

Covers:
- The declared-stack-sum resolver: literal sums, nested derived sums
  (v65's ``base_height = a + b; total = base_height + c``), non-literal
  operands (abstain), and the largest qualifying sum wins.
- The comparison against the measured Z: the disagrees-major threshold
  ``max(20% of declared, 5 mm)`` — strict ``>``, the same pair as bit 5.
- The fixture-level behaviour: the v65 lid (FIRES), the correct lid
  (PASSES).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d.stack_height_check import (
    declared_stack_sum,
    is_height_name,
    stack_height_check,
)

FIXTURES = Path(__file__).parent / "fixtures" / "scad"


def _scad(name: str) -> str:
    return (FIXTURES / name).read_text()


# ---------------------------------------------------------------------------
# The declared-stack-sum resolver: parameter-block sum resolution
# ---------------------------------------------------------------------------


def test_declared_stack_sum_literal_sum():
    """A plain literal sum of two positive params is the stack."""
    scad = "a = 4;\nb = 2;\ntotal_height = a + b;\ncube([a, a, a]);\n"
    total, refs = declared_stack_sum(scad)
    assert total == 6.0
    assert set(refs) == {"a", "b"}


def test_declared_stack_sum_nested_derived_sums_v65_shape():
    """Issue #409 (v65's shape): nested derived sums —
    ``base_height = a + b; skirt_total_height = base_height + c`` —
    resolve transitively; the LARGEST qualifying sum wins (the total,
    not the sub-total)."""
    scad = (
        "lid_base_thickness = 4;\n"
        "rim_drop = 2;\n"
        "skirt_height = 5;\n"
        "base_height = lid_base_thickness + rim_drop;\n"
        "skirt_total_height = base_height + skirt_height;\n"
        "cube([4, 4, 4]);\n"
    )
    total, refs = declared_stack_sum(scad)
    assert total == 11.0
    assert set(refs) == {"base_height", "skirt_height"}


def test_declared_stack_sum_largest_qualifying_wins():
    """Several qualifying sums: the largest (the declared overall
    height) wins — a sub-total never outranks the total."""
    scad = (
        "a = 3;\nb = 4;\nc = 4;\n"
        "sub_height = a + b;\n"
        "total_height = sub_height + c;\n"
        "cube([a, a, a]);\n"
    )
    total, refs = declared_stack_sum(scad)
    assert total == 11.0
    assert set(refs) == {"sub_height", "c"}


def test_declared_stack_sum_prefers_total_height_name():
    """``total_height`` (the prompt-declared name) is the declared
    overall height: with ``total_height`` present it wins over a larger
    non-height-named sum is impossible — but among height-named
    candidates the largest still wins, and a width-named sum never
    qualifies."""
    scad = (
        "w = 65;\nd = 50;\n"
        "skirt_width = 55 + 2 * 5;\n"
        "h1 = 6;\nh2 = 5;\n"
        "total_height = h1 + h2;\n"
        "cube([w, d, h1]);\n"
    )
    # ``skirt_width`` is NOT height-named — it never competes.
    total, refs = declared_stack_sum(scad)
    assert total == 11.0
    assert set(refs) == {"h1", "h2"}


def test_declared_stack_sum_non_literal_operand_abstains():
    """Issue #409 edge case: an operand containing arithmetic
    (``2 * rim_width``) is NOT a bare parameter reference — a sum whose
    only non-literal operand is arithmetic abstains (it is not a sum of
    resolvable parameters)."""
    scad = (
        "rim_width = 5;\n"
        "lid_width = 55;\n"
        "skirt_width = lid_width + 2 * rim_width;\n"
        "cube([lid_width, 4, 4]);\n"
    )
    # ``skirt_width`` is not height-named in the first place, but the
    # arithmetic operand must also disqualify a height-named sum.
    scad2 = (
        "rim_width = 5;\n"
        "lid_height = 55;\n"
        "stack_height = lid_height + 2 * rim_width;\n"
        "cube([lid_height, 4, 4]);\n"
    )
    assert declared_stack_sum(scad) is None
    assert declared_stack_sum(scad2) is None


def test_declared_stack_sum_no_plus_abstains():
    """A parameter block with no ``+`` at all abstains (no sum to
    compare)."""
    assert declared_stack_sum("W = 20;\ncube([W, W, W]);\n") is None
    assert declared_stack_sum("cube([20, 20, 20]);\n") is None


def test_declared_stack_sum_unresolvable_rhs_abstains():
    """A declaration whose RHS is not a resolvable constant (a
    function call, a vector) abstains — never a fabricated stack."""
    scad = (
        "a = 4;\nb = 2;\n"
        "r = sqrt(a);\n"
        "total_height = a + b;\n"
        "cube([a, a, a]);\n"
    )
    assert declared_stack_sum(scad) is None
    scad2 = "total_height = [1, 2] + 3;\n"
    assert declared_stack_sum(scad2) is None


def test_declared_stack_sum_not_a_true_sum_abstains():
    """``a = b + c - c`` evaluates to ``b`` — the value must equal the
    sum of its referenced params, so a pseudo-sum is not a stack sum
    (abstain, never a false fire)."""
    scad = (
        "b = 4;\nc = 2;\n"
        "total_height = b + c - c;\n"
        "cube([b, b, b]);\n"
    )
    assert declared_stack_sum(scad) is None


def test_declared_stack_sum_zero_param_not_a_summand():
    """A zero-valued parameter is not a positive summand — a sum
    ``total = a + zero`` with one real operand is not a sum of >= 2
    positive params (abstain)."""
    scad = (
        "a = 4;\nzero_pad = 0;\n"
        "total_height = a + zero_pad;\n"
        "cube([a, a, a]);\n"
    )
    assert declared_stack_sum(scad) is None


def test_declared_stack_sum_comment_does_not_split():
    """A ``;`` inside a ``//`` comment must not truncate the
    declaration (``total_height = a + b; // 6`` is one declaration, and
    the comment's numbers are never values)."""
    scad = (
        "a = 4;\nb = 2;\n"
        "total_height = a + b; // 6 — the plate + the rim\n"
        "cube([a, a, a]);\n"
    )
    total, refs = declared_stack_sum(scad)
    assert total == 6.0
    assert set(refs) == {"a", "b"}


def test_declared_stack_sum_duplicate_name_abstains():
    """A parameter declared twice abstains (the block is ambiguous —
    never guess which value wins)."""
    scad = "a = 4;\na = 5;\ntotal_height = a + a;\ncube([a, a, a]);\n"
    assert declared_stack_sum(scad) is None


def test_declared_stack_sum_unbalanced_brackets_abstains():
    """Unbalanced brackets anywhere abstain (malformed block)."""
    scad = "a = 4;\nb = 2;\ntotal_height = a + b;\ncube([a, a, a);\n"
    assert declared_stack_sum(scad) is None


def test_is_height_name_tokens():
    """The height-name token test: height/thickness/drop/skirt/rim/lip/
    base/total/overall qualify; width/depth/count do not (``width`` is
    not a height token — a ``skirt_width`` sum is a width, never a
    stack)."""
    for name in (
        "skirt_total_height",
        "base_height",
        "lid_base_thickness",
        "total_height",
        "overall_height",
        "rim_drop",
        "skirt_height",
        "lip_height",
        "BaseHeight",
        "baseHeight",
    ):
        assert is_height_name(name), name
    for name in (
        "lid_width",
        "hole_count",
        "",
    ):
        assert not is_height_name(name), name
    # ``rim_width`` / ``skirt_width`` contain the ``rim`` / ``skirt``
    # tokens — a rim or skirt IS a stack feature (the v65 repro's rim drop
    # and skirt are components of the stack), so a rim/skirt-named
    # parameter qualifies even when it is also a width. Only names with
    # no height token at all are rejected.
    assert is_height_name("rim_width")
    assert is_height_name("skirt_width")


def test_declared_stack_sum_no_declaration_line_abstains():
    """Geometry without a parameter block abstains."""
    assert declared_stack_sum("cube([20, 20, 20]);\n") is None
    assert declared_stack_sum("") is None


# ---------------------------------------------------------------------------
# The comparison: declared sum vs measured Z
# ---------------------------------------------------------------------------


def test_stack_height_check_fires_v65_fixture():
    """Issue #409 (v65 repro): the real QA lid's SCAD declares a stack
    of 11 mm; a measured Z of 4 (the rendered slab) disagrees by 7 mm
    > max(20% of 11, 5) → FIRES with the detection tuple."""
    v65 = _scad("v65-lid-difference-inversion.scad")
    det = stack_height_check(v65, 4.0)
    assert det is not None
    declared, measured = det
    assert declared == 11.0
    assert measured == 4.0


def test_stack_height_check_passes_correct_lid_fixture():
    """Issue #409: the correct lid declares the same stack (11 mm) and
    measures 11 mm → the diff is 0 ≤ the threshold → PASSES (None)."""
    lid = _scad("lid-correct-union.scad")
    assert stack_height_check(lid, 11.0) is None


def test_stack_height_check_threshold_is_disagrees_major():
    """The threshold is ``max(20% of declared, 5 mm)`` — the same pair
    as bit 5. Declared 11: threshold 5 mm; a diff strictly greater than
    5 fires, exactly 5 passes."""
    scad = (
        "a = 6;\nb = 5;\n"
        "total_height = a + b;\n"
        "cube([a, a, a]);\n"
    )
    # 11 vs 6.0 → diff 5.0, AT the threshold (max(2.2, 5) = 5) → pass.
    assert stack_height_check(scad, 6.0) is None
    # 11 vs 5.9 → diff 5.1 > 5 → fire.
    det = stack_height_check(scad, 5.9)
    assert det is not None and det == (11.0, 5.9)
    # 11 vs 16.0 → diff 5.0 → pass (symmetric).
    assert stack_height_check(scad, 16.0) is None
    # 11 vs 16.1 → diff 5.1 > 5 → fire.
    det = stack_height_check(scad, 16.1)
    assert det is not None and det == (11.0, 16.1)


def test_stack_height_check_small_stack_uses_relative_threshold():
    """For a small stack (declared 20), 20% = 4 < 5 — the 5 mm floor
    still wins; for declared 50, 20% = 10 > 5 — the relative threshold
    wins (a diff of 8 mm passes: 8 ≤ 10)."""
    scad_small = "a = 10;\nb = 10;\ntotal_height = a + b;\ncube([a, a, a]);\n"
    assert stack_height_check(scad_small, 16.0) is None  # diff 4 ≤ 5
    det = stack_height_check(scad_small, 14.9)
    assert det is not None  # diff 5.1 > 5
    scad_big = "a = 25;\nb = 25;\ntotal_height = a + b;\ncube([a, a, a]);\n"
    assert stack_height_check(scad_big, 42.0) is None  # diff 8 ≤ 10
    det = stack_height_check(scad_big, 39.9)
    assert det is not None  # diff 10.1 > 10


def test_stack_height_check_no_measured_z_abstains():
    """No mesh / no bbox (``measured_z`` is None) abstains — even with
    a declared sum and a fired diff the check cannot fire without a
    measurement."""
    scad = "a = 6;\nb = 5;\ntotal_height = a + b;\ncube([a, a, a]);\n"
    assert stack_height_check(scad, None) is None


def test_stack_height_check_no_stack_abstains_with_z():
    """No declared sum (the common case — 78 of 81 audited SCADs)
    abstains even with a measured Z."""
    assert stack_height_check("W = 20;\ncube([W, 20, 20]);\n", 20.0) is None


# ---------------------------------------------------------------------------
# Fixtures: the two committed SCADs (the audit's discriminating pair)
# ---------------------------------------------------------------------------


def test_v65_fixture_declared_stack_is_eleven():
    """The v65 fixture's declared stack resolves to 11 (the audit's
    single true positive)."""
    v65 = _scad("v65-lid-difference-inversion.scad")
    total, refs = declared_stack_sum(v65)
    assert total == 11.0
    assert set(refs) == {"base_height", "skirt_height"}


def test_correct_lid_fixture_declared_stack_is_eleven():
    """The correct-lid fixture's declared stack resolves to 11 as well
    (the audit's pass case)."""
    lid = _scad("lid-correct-union.scad")
    total, refs = declared_stack_sum(lid)
    assert total == 11.0
    assert set(refs) == {"base_height", "skirt_height"}


@pytest.mark.parametrize(
    "z, expected",
    [
        (4.0, (11.0, 4.0)),  # the v65 render — fires
        (11.0, None),  # the correct lid — passes
        (6.0, None),  # diff 5, at the threshold — passes
        (16.0, None),  # diff 5, at the threshold — passes
        (16.1, (11.0, 16.1)),  # diff 5.1 — fires
        (None, None),  # no mesh — abstains
    ],
)
def test_fixture_gate_verdicts(z, expected):
    """The audit's verdict table, pinned on the committed fixtures:
    v65 at Z 4 fires; the correct lid at Z 11 passes; threshold
    behaviour at the 5 mm edge; no-mesh abstains."""
    for fixture in ("v65-lid-difference-inversion.scad", "lid-correct-union.scad"):
        det = stack_height_check(_scad(fixture), z)
        assert det == expected, f"{fixture} @ Z={z}: {det!r} != {expected!r}"
