"""tests/test_issue435_checks_run.py — the pass card lists the checks that ran.

Issue #435: a passing candidate records the closed, ordered set of checks that
actually executed (and passed) — an abstaining post-check is never listed. The
done frame carries the list (omitted when empty), and ``vision`` defaults off so
a caller that omits it is never reviewed.
"""

from __future__ import annotations

import inspect

from d33d.design_loop import DesignResult, run_design_loop
from d33d.design_loop_events import checks_run_fields
from tests.test_design_loop import (
    _box_minus_cylinder,
    _run_through_loop,
)


def test_through_hole_pass_records_size_watertight_and_through_holes(tmp_path):
    """A passing run for a through-hole request ran the bbox gate, the
    watertight render and the through-hole genus check — all three are
    recorded on the kept pass."""
    stl = str(tmp_path / "through.stl")
    _box_minus_cylinder(blind=False).export(stl)
    result = _run_through_loop(
        stl, "drill a 6 mm hole through the middle", through_baseline_genus=0
    )
    assert result.status == "pass"
    # The hollow check also ran (a real solid STL, not a hollow-exempt request).
    assert result.checks_run == ("size", "watertight", "through_holes", "hollow")


def test_abstaining_post_checks_are_not_recorded(tmp_path):
    """A plain box with no through-hole request, no screw size, no declared
    stack and no parent mesh: the through-hole, screw, stack, change and hollow
    checks all abstain, so none of them is listed."""
    result = _run_through_loop(None, "a plain 20 mm box", through_baseline_genus=None)
    assert result.status == "pass"
    assert result.checks_run == ("size", "watertight")
    for key in ("through_holes", "screw_clearance", "stack_height", "change_detected", "hollow"):
        assert key not in result.checks_run


def test_done_frame_fields_carry_checks_run_when_non_empty():
    result = DesignResult(
        status="pass",
        best=None,
        iterations=(),
        checks_run=("size", "watertight", "through_holes"),
    )
    assert checks_run_fields(result) == {"checks_run": ["size", "watertight", "through_holes"]}


def test_done_frame_omits_checks_run_when_empty():
    result = DesignResult(status="pass", best=None, iterations=())
    assert checks_run_fields(result) == {}


def test_vision_defaults_to_off_for_both_entry_points():
    from d33d.design_loop import run_design_loop_async

    assert inspect.signature(run_design_loop).parameters["vision"].default is False
    assert inspect.signature(run_design_loop_async).parameters["vision"].default is False
