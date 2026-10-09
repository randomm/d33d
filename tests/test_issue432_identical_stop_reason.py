"""Issue #432: the identical-repair stop never lets the post-check reason
override a gate-derived reason on the N-1 record."""

from __future__ import annotations

from pathlib import Path

import pytest

from d33d import design_loop
from d33d.design_loop import BboxInfo, run_design_loop
from tests.test_design_loop import (
    PHOTO,
    _render_with_stl_and_bbox,
    _through_box_llm,
    _through_box_scad_source,
)


@pytest.fixture
def no_gate_repair(monkeypatch):
    """Gate repairs route to nothing, so the post-check is the only repair
    that can fire on a gate-failing candidate (the state under test)."""
    monkeypatch.setattr(design_loop, "route_repair", lambda **_: None)


def test_identical_stop_keeps_gate_reason_over_post_check_reason(no_gate_repair):
    """N-1 carries a false bbox gate bit (render bbox 30 mm vs stated 20 mm)
    and a mesh_unchanged post-check repair; attempt N repeats the mesh, so the
    post-check repeats. The terminal reason must be the gate reason ``bbox``."""
    fixture_dir = Path(__file__).parent / "fixtures" / "stl"
    parent = str(fixture_dir / "v100-plate.stl")
    base = _through_box_scad_source()
    scads = [f"// attempt one\n{base}", f"// attempt two\n{base}"]
    calls = {"n": 0}

    def llm_fn(role, messages, system):
        calls["n"] += 1
        return _through_box_llm(scads[min(calls["n"], 2) - 1])

    render = _render_with_stl_and_bbox(parent, BboxInfo(30, 20, 20, 8000.0))
    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=lambda scad, defines: render,
        llm_fn=llm_fn,
        bbox_fn=lambda r: BboxInfo(30, 20, 20, 8000.0),
        parent_mesh_stl=parent,
    )
    assert result.status == "exhausted"
    assert result.iterations_used == 2
    assert result.best is result.iterations[0]
    assert result.iterations[0].repair["reason"] == "mesh_unchanged"
    assert result.failure_reason == "bbox_out_of_tolerance"
