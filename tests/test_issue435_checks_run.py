"""tests/test_issue435_checks_run.py — the pass card lists the checks that ran.

Issue #435: a passing candidate records the closed, ordered set of checks that
actually executed AND passed. Each post-check reports itself at its own pass
point; an abstaining post-check is never listed. The done frame carries the
list (omitted when empty), and ``vision`` defaults off so a caller that omits
it is never reviewed.
"""

from __future__ import annotations

import inspect

import trimesh

from d33d.design_loop import BboxInfo, DesignResult, run_design_loop
from d33d.design_loop_events import checks_run_fields
from tests.test_design_loop import (
    PHOTO,
    _box_minus_cylinder,
    _render_with_stl,
    _run_through_loop,
    _scad_llm,
)

_BOX_SCAD = "W = 20;\nD = 20;\nH = 20;\ncube([W, D, H]);\n"


def _run_box(stl: str, request: str, **kwargs) -> DesignResult:
    llm = _scad_llm(_BOX_SCAD)

    def llm_fn(role, messages, system):
        return llm

    return run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=lambda scad, defines: _render_with_stl(stl),
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(20.0, 20.0, 20.0, 8000.0),
        request=request,
        **kwargs,
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


def test_abstaining_change_check_without_parent_fingerprint_is_not_listed(tmp_path):
    """An edit turn whose parent STL is missing has NO parent baseline: the
    unchanged-mesh check abstains (a missing file is never a fabricated
    baseline), so the card must not claim "change detected"."""
    stl = str(tmp_path / "candidate.stl")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(stl)
    result = _run_box(
        stl,
        "a 20 mm box",
        parent_mesh_stl="/missing/model.stl",
        parent_fingerprint=None,
    )
    assert result.status == "pass"
    assert "change_detected" not in result.checks_run


def test_abstaining_hollow_check_on_exempt_request_is_not_listed(tmp_path):
    """A request that names a thin or hollow form is exempt from the hollow
    check: it abstains, so "hollow" is never listed even on a solid render."""
    stl = str(tmp_path / "solid.stl")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(stl)
    result = _run_box(stl, "a thin hollow box")
    assert result.status == "pass"
    assert "hollow" not in result.checks_run


def test_abstaining_screw_check_with_two_sizes_is_not_listed(tmp_path):
    """Two named metric sizes against one undersize hole param: the screw
    check finds two undersize hits and abstains, so "screw clearance" is not
    listed."""
    stl = str(tmp_path / "candidate.stl")
    trimesh.creation.box(extents=(20.0, 20.0, 20.0)).export(stl)
    llm = _scad_llm("hole_d = 4;\nW = 20;\nD = 20;\nH = 20;\ncube([W, D, H]);\n")

    def llm_fn(role, messages, system):
        return llm

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=lambda scad, defines: _render_with_stl(stl),
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(20.0, 20.0, 20.0, 8000.0),
        request="a 20 mm plate with an M4 and an M5 screw hole",
    )
    assert result.status == "pass"
    assert "screw_clearance" not in result.checks_run


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
