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
    _eval_mul,
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
    function call, a vector) is SKIPPED — the check evaluates the
    remaining declarations and finds the stack among them. A SCAD where
    ALL declarations are unresolvable abstains entirely."""
    # `r = sqrt(a)` is unresolvable (function call) — skipped. The
    # remaining declarations resolve: total_height = a + b = 6.
    scad = (
        "a = 4;\nb = 2;\n"
        "r = sqrt(a);\n"
        "total_height = a + b;\n"
        "cube([a, a, a]);\n"
    )
    total, refs = declared_stack_sum(scad)
    assert total == 6.0
    assert set(refs) == {"a", "b"}
    # A SCAD where the ONLY height-named sum depends on an unresolvable
    # declaration abstains (the sum is not in the resolvable env).
    scad2 = (
        "r = sqrt(4);\n"
        "total_height = r + 3;\n"
        "cube([4, 4, 4]);\n"
    )
    assert declared_stack_sum(scad2) is None
    # A vector-only SCAD (no resolvable declarations at all) abstains.
    scad3 = "total_height = [1, 2] + 3;\n"
    assert declared_stack_sum(scad3) is None


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


def test_declared_stack_sum_duplicate_name_first_wins():
    """A name declared twice is SKIPPED (first occurrence wins, the
    second is ignored — the round-1 skip-and-continue behaviour, not a
    file-level abstain). The remaining declarations are evaluated: a
    genuine two-operand sum is still found. (A duplicate of the SAME
    name in a sum, ``total = a + a``, yields one distinct reference and
    abstains for that reason, not because the file aborted.)"""
    # Duplicate ``a`` (4 then 5) is skipped → first wins (a = 4). A
    # distinct second operand makes a genuine two-operand sum → found.
    scad = "a = 4;\na = 5;\nb = 2;\ntotal_height = a + b;\ncube([a, a, a]);\n"
    total, refs = declared_stack_sum(scad)
    assert total == 6.0  # a (first = 4) + b (2)
    assert set(refs) == {"a", "b"}
    # A sum whose only distinct reference is the duplicated name abstains
    # (one distinct ref < 2) — for that reason, not a file abort.
    same = "a = 4;\na = 5;\ntotal_height = a + a;\ncube([a, a, a]);\n"
    assert declared_stack_sum(same) is None


def test_declared_stack_sum_unbalanced_brackets_abstains():
    """Unbalanced brackets anywhere abstain (malformed block)."""
    scad = "a = 4;\nb = 2;\ntotal_height = a + b;\ncube([a, a, a);\n"
    assert declared_stack_sum(scad) is None


def test_declared_stack_sum_deep_nesting_does_not_recurse():
    """Issue #409 (crash regression): a deeply nested, balanced
    expression in a model-authored parameter block (thousands of parens)
    must not raise ``RecursionError`` out of the design loop — the
    evaluator's depth cap turns the over-deep declaration into a clean
    skip (abstain), exactly like any other unresolvable declaration."""
    deep = "total_height = " + "(" * 5000 + "1" + ")" * 5000 + " + a;"
    assert declared_stack_sum(deep) is None

    # The check itself must not raise either — it abstains the same way.
    assert stack_height_check(deep, 4.0) is None

    # Nested but shallow parenthesisation still evaluates (the cap does
    # not degrade normal expressions) — both operands bare parameters.
    nested = "a = 4;\nb = 2;\ntotal_height = (a + b);\ncube([a, a, a]);\n"
    total, refs = declared_stack_sum(nested)
    assert total == 6.0
    assert set(refs) == {"a", "b"}


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


# ---------------------------------------------------------------------------
# Over-abstention: unresolvable and duplicate declarations are SKIPPED,
# not aborts — the gate stays alive on real model SCADs that mix
# resolvable and unresolvable declarations in the same parameter block.
# ---------------------------------------------------------------------------


def test_declared_stack_sum_skips_unresolvable_and_finds_stack():
    """A function call in one declaration does not abort the whole file —
    the unresolvable declaration is skipped and the stack is found among
    the remaining resolvable declarations (the v59 shape: a plate with a
    derived hole_radius alongside a total_height sum)."""
    scad = (
        "plate_width = 120;\n"
        "plate_thickness = 6;\n"
        "screw_hole_diameter = 4;\n"
        "screw_hole_clearance = 0.5;\n"
        "hole_radius = (screw_hole_diameter + screw_hole_clearance) / 2;\n"
        "base_height = 4;\n"
        "skirt_height = 5;\n"
        "total_height = base_height + skirt_height;\n"
        "cube([plate_width, plate_width, total_height]);\n"
    )
    total, refs = declared_stack_sum(scad)
    assert total == 9.0
    assert set(refs) == {"base_height", "skirt_height"}


def test_declared_stack_sum_skips_duplicate_declaration():
    """A name declared twice: the first occurrence wins, the second is
    skipped (the v100 shape: multi-line cylinder arguments that the
    line-leading regex misreads as a re-declaration of ``center``)."""
    scad = (
        "part_width_mm = 120;\n"
        "part_depth_mm = 80;\n"
        "part_height_mm = 6;\n"
        "cut_depth = part_depth_mm + 10;\n"
        "base_height = 4;\n"
        "skirt_height = 5;\n"
        "total_height = base_height + skirt_height;\n"
        "cylinder(d = part_width_mm, h = part_height_mm,\n"
        "         center = true);\n"
        "cube([part_width_mm, part_depth_mm, total_height]);\n"
    )
    # ``center`` appears twice (the multi-line cylinder args) — skipped.
    # The stack is still found.
    total, refs = declared_stack_sum(scad)
    assert total == 9.0
    assert set(refs) == {"base_height", "skirt_height"}


def test_declared_stack_sum_all_unresolvable_abstains():
    """A parameter block where NO declaration resolves to a constant
    abstains (no env → no stack)."""
    scad = (
        "r = sqrt(4);\n"
        "d = atan(1);\n"
        "total_height = r + d;\n"
        "cube([r, r, r]);\n"
    )
    assert declared_stack_sum(scad) is None


def test_declared_stack_sum_height_named_vector_declaration_no_crash():
    """Issue #409 (crash regression): a HEIGHT-NAMED declaration whose RHS
    is a VECTOR containing ``+`` (``lip_outer_top = [W + 2*t, D + 2*t]`` —
    a real shape from ``evals/failures.jsonl`` row 55) is skipped from
    ``env`` (a vector is not a readable atom), so the candidate loop must
    NOT dereference it — the ``name not in env`` guard turns the former
    ``KeyError`` into a clean skip. The rest of the block still resolves:
    the stack among the resolvable declarations is found.
    """
    scad = (
        "flared_lip_wall_thickness = 3;\n"
        "W = 60; D = 45;\n"
        "lip_outer_top = [W + 2*flared_lip_wall_thickness, "
        "D + 2*flared_lip_wall_thickness];\n"
        "base_height = 4;\n"
        "skirt_height = 5;\n"
        "total_height = base_height + skirt_height;\n"
        "cube([W, D, 4]);\n"
    )
    # Does not crash. The vector is skipped; the real stack is found.
    total, refs = declared_stack_sum(scad)
    assert total == 9.0
    assert set(refs) == {"base_height", "skirt_height"}
    # When the vector is the ONLY height-named + containing declaration,
    # the check abstains cleanly (no env entry → no candidate → None),
    # not a crash.
    only_vector = (
        "flared_lip_wall_thickness = 3;\n"
        "W = 60; D = 45;\n"
        "lip_outer_top = [W + 2*flared_lip_wall_thickness, "
        "D + 2*flared_lip_wall_thickness];\n"
        "cube([W, D, 4]);\n"
    )
    assert declared_stack_sum(only_vector) is None


# ---------------------------------------------------------------------------
# Recursion safety: every evaluator path must be bounded
# ---------------------------------------------------------------------------


def test_unary_minus_chain_does_not_recurse_unbounded():
    """Issue #409 (adversarial round 2, PM-flagged gap): ``_eval_mul``'s
    unary-minus branch used to recurse once per leading ``-`` WITHOUT
    advancing ``depth``, so a direct call with a long minus chain raised
    ``RecursionError`` (the public API was safe because ``_eval_add``
    splits on every top-level ``-`` first — but ``_eval_mul`` was not
    self-contained safe; a future caller passing a long minus chain
    directly would crash the design loop). The leading minuses are now
    counted iteratively, so any chain length is safe and parity-preserving:
    ``--a`` → ``a``, ``-a`` → ``-a``."""
    # Direct call: a long minus chain must not raise RecursionError.
    assert _eval_mul("-" * 996 + "a", {"a": 4.0}) == 4.0
    assert _eval_mul("-" * 999 + "a", {"a": 4.0}) == -4.0
    assert _eval_mul("-" * 5000 + "a", {"a": 4.0}) == 4.0

    # Short chains keep their exact semantics.
    assert _eval_mul("-a", {"a": 4.0}) == -4.0
    assert _eval_mul("--a", {"a": 4.0}) == 4.0
    assert _eval_mul("a", {"a": 4.0}) == 4.0
    # Unresolvable operand stays None (not 0, not a crash).
    assert _eval_mul("-" * 996 + "missing", {"a": 4.0}) is None

    # The PM's exact repro through the PUBLIC API: a 5000-long minus chain
    # before a real stack sum must abstain cleanly, never raise.
    deep = "total_height = " + "-" * 5000 + "a + b;\na = 4;\nb = 7;\n"
    assert declared_stack_sum(deep) is None
    assert stack_height_check(deep, 4.0) is None
