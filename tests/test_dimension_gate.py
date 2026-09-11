"""Fast-layer tests for the dimension gate (ticket #4).

The rule is pinned as a single expression:
    error <= max(1% of stated, 0.5mm)
applied per axis. Boundary tests at exactly the threshold (pass) and
threshold+epsilon (fail) for each axis, plus the cases where 0.5mm
dominates (small stated dims) and where 1% dominates (large dims).
"""

from __future__ import annotations

import math

from d33d.print_validation import dimension_error_ok


# ---------------------------------------------------------------------------
# The pinned expression
# ---------------------------------------------------------------------------


def test_exact_threshold_passes():
    """At exactly the threshold (error == max(1%, 0.5mm)) the gate PASSES."""
    # 1% dominates: stated=100mm, threshold=max(1.0, 0.5)=1.0mm
    assert dimension_error_ok(actual=99.0, stated=100.0) is True
    # 0.5mm dominates: stated=10mm, threshold=max(0.1, 0.5)=0.5mm
    assert dimension_error_ok(actual=10.5, stated=10.0) is True
    # 0.5mm dominates: stated=2mm, threshold=max(0.02, 0.5)=0.5mm
    assert dimension_error_ok(actual=2.5, stated=2.0) is True


def test_threshold_plus_epsilon_fails():
    """At threshold+epsilon the gate FAILS. The error must exceed the
    threshold. For stated=100mm, threshold=1.0mm, so we need actual < 99.0
    (i.e. error > 1.0mm). We use actual = stated - threshold - eps."""
    eps = 1e-4
    # 1% dominates: stated=100mm, threshold=1.0mm
    # actual = 100 - 1.0 - eps = 98.9999 -> error = 1.0001 > 1.0 -> FAILS
    assert dimension_error_ok(actual=98.9999, stated=100.0) is False
    # 0.5mm dominates: stated=10mm, threshold=0.5mm
    # actual = 10 - 0.5 - eps = 9.4999 -> error = 0.5001 > 0.5 -> FAILS
    assert dimension_error_ok(actual=9.4999, stated=10.0) is False
    # 0.5mm dominates: stated=2mm, threshold=0.5mm
    # actual = 2 - 0.5 - eps = 1.4999 -> error = 0.5001 > 0.5 -> FAILS
    assert dimension_error_ok(actual=1.4999, stated=2.0) is False


def test_under_threshold_passes():
    """Well under the threshold always passes."""
    assert dimension_error_ok(actual=100.0, stated=100.0) is True
    assert dimension_error_ok(actual=99.5, stated=100.0) is True
    assert dimension_error_ok(actual=10.2, stated=10.0) is True
    assert dimension_error_ok(actual=2.2, stated=2.0) is True


# ---------------------------------------------------------------------------
# 0.5mm dominates (small stated dims)
# ---------------------------------------------------------------------------


def test_half_mm_dominates_small_dims():
    """For stated dims where 1% < 0.5mm (i.e. stated < 50mm), the 0.5mm
    floor dominates."""
    # stated=10mm: 1%=0.1mm, threshold=0.5mm
    assert dimension_error_ok(actual=10.4, stated=10.0) is True
    assert dimension_error_ok(actual=10.5, stated=10.0) is True  # boundary
    assert dimension_error_ok(actual=9.4999, stated=10.0) is False  # error=0.5001 > 0.5
    # stated=1mm: 1%=0.01mm, threshold=0.5mm
    assert dimension_error_ok(actual=1.4, stated=1.0) is True
    assert dimension_error_ok(actual=1.5, stated=1.0) is True  # boundary
    assert dimension_error_ok(actual=0.4999, stated=1.0) is False  # error=0.5001 > 0.5
    # stated=49mm: 1%=0.49mm, threshold=0.5mm (0.5 still dominates)
    assert dimension_error_ok(actual=49.49, stated=49.0) is True
    assert dimension_error_ok(actual=49.5, stated=49.0) is True  # boundary
    assert dimension_error_ok(actual=48.4999, stated=49.0) is False  # error=0.5001 > 0.5


# ---------------------------------------------------------------------------
# 1% dominates (large stated dims)
# ---------------------------------------------------------------------------


def test_one_percent_dominates_large_dims():
    """For stated dims where 1% > 0.5mm (i.e. stated > 50mm), the 1%
    clause dominates."""
    # stated=100mm: 1%=1.0mm, threshold=1.0mm
    assert dimension_error_ok(actual=99.0, stated=100.0) is True  # boundary
    assert dimension_error_ok(actual=98.9999, stated=100.0) is False  # error=1.0001 > 1.0
    # stated=200mm: 1%=2.0mm, threshold=2.0mm
    assert dimension_error_ok(actual=198.0, stated=200.0) is True  # boundary
    assert dimension_error_ok(actual=197.9999, stated=200.0) is False  # error=2.0001 > 2.0
    # stated=500mm: 1%=5.0mm, threshold=5.0mm
    assert dimension_error_ok(actual=495.0, stated=500.0) is True  # boundary
    assert dimension_error_ok(actual=494.9999, stated=500.0) is False  # error=5.0001 > 5.0


# ---------------------------------------------------------------------------
# Per-axis independence
# ---------------------------------------------------------------------------


def test_each_axis_is_independent():
    """The rule is applied per axis. A mesh can pass on X, Y, and Z
    independently."""
    # X passes, Y fails
    assert dimension_error_ok(actual=20.0, stated=20.0) is True  # X: 0 error
    assert dimension_error_ok(actual=21.0, stated=20.0) is False  # Y: 1mm error, threshold=max(0.2,0.5)=0.5mm
    # Z passes
    assert dimension_error_ok(actual=30.0, stated=30.0) is True


def test_all_axes_must_pass():
    """All three axes must independently satisfy the rule."""
    stated = (20.0, 20.0, 20.0)
    actual = (20.0, 20.0, 20.0)
    for i in range(3):
        assert dimension_error_ok(actual[i], stated[i])


# ---------------------------------------------------------------------------
# Single pinned expression (not three magic numbers)
# ---------------------------------------------------------------------------


def test_rule_is_single_expression():
    """The dimension rule must be a single pinned expression, not three
    separate per-axis magic numbers. The 1% and 0.5mm sub-clauses must
    each be exercisable by distinct fixtures (which they are, as shown
    above)."""
    # Verify the rule handles both sub-clauses correctly
    # 0.5mm sub-clause (small dims)
    assert dimension_error_ok(actual=1.5, stated=1.0) is True
    # 1% sub-clause (large dims)
    assert dimension_error_ok(actual=99.0, stated=100.0) is True
    # Neither sub-clause is hard-coded to a single axis
    for stated_val in (2.0, 10.0, 100.0, 500.0):
        threshold = max(0.01 * stated_val, 0.5)
        assert dimension_error_ok(actual=stated_val, stated=stated_val) is True
        assert dimension_error_ok(actual=stated_val + threshold * 0.5, stated=stated_val) is True
        assert dimension_error_ok(actual=stated_val + threshold * 1.5, stated=stated_val) is False
