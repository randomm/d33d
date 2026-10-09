"""Issue #432: the screw-clearance identical-repair stop and the per-attempt
timeout override (both loop-level, real STL on disk)."""

from __future__ import annotations

import asyncio

import trimesh

from d33d.design_loop import BboxInfo, run_design_loop, run_design_loop_async
from tests.test_design_loop import (
    PHOTO,
    _plate_scad,
    _render_with_stl_and_bbox,
    _screw_scad_llm,
)

SCREW_META = [{"name": "hole_d", "label": "M4 hole diameter", "unit": "mm"}]
REQUEST = "a 60 × 45 mm plate with an M4 hole"


def _plate_stl(tmp_path) -> str:
    stl = str(tmp_path / "plate.stl")
    trimesh.creation.box(extents=(60.0, 45.0, 8.0)).export(stl)
    return stl


def test_screw_clearance_identical_mesh_stops_at_attempt_2(tmp_path):
    """Issue #432: every attempt fails the screw-clearance post-check on an
    identical mesh → the loop exhausts at attempt 2 (not the cap) with
    ``screw_clearance_wrong``, reports attempt 1 as best, and the repair
    instruction carries the measured bore diameter and the required
    clearance diameter."""
    stl = _plate_stl(tmp_path)
    llm = _screw_scad_llm(_plate_scad(4.0), SCREW_META)
    bbox = BboxInfo(60.0, 45.0, 8.0, 21600.0)
    result = run_design_loop(
        photo=PHOTO,
        stated_dims=(60.0, 45.0, 8.0),
        render_fn=lambda scad, defines: _render_with_stl_and_bbox(stl, bbox),
        llm_fn=lambda role, messages, system: llm,
        bbox_fn=lambda r: bbox,
        request=REQUEST,
    )
    assert result.status == "exhausted"
    assert result.iterations_used == 2
    assert result.failure_reason == "screw_clearance_wrong"
    assert result.best is result.iterations[0]
    instruction = result.iterations[0].repair["instruction"]
    # The measured fact: the bore (4.0 mm) against the required clearance (4.5 mm).
    assert "4.0 mm" in instruction
    assert "4.5 mm" in instruction


def test_timeout_on_attempt_2_overrides_post_check_reason(tmp_path):
    """Issue #432 / #417: the post-check fails on attempt 1, then attempt 2
    hits the per-attempt deadline. The final failure_reason is
    ``design_loop_timed_out`` — the timeout override wins over the post-check
    reason the best candidate carries."""
    stl = _plate_stl(tmp_path)
    good = _screw_scad_llm(_plate_scad(4.0), SCREW_META)
    calls = {"n": 0}

    async def llm_fn(role, messages, system):
        calls["n"] += 1
        if calls["n"] == 1:
            return good
        await asyncio.sleep(5)  # attempt 2 outlives the 0.2 s per-attempt deadline
        return good

    bbox = BboxInfo(60.0, 45.0, 8.0, 21600.0)
    result = asyncio.run(
        run_design_loop_async(
            photo=PHOTO,
            stated_dims=(60.0, 45.0, 8.0),
            render_fn=lambda scad, defines: _render_with_stl_and_bbox(stl, bbox),
            llm_fn=llm_fn,
            bbox_fn=lambda r: bbox,
            request=REQUEST,
            attempt_timeout=0.2,
        )
    )
    assert result.failure_reason == "design_loop_timed_out"
    assert result.best is not None
    assert result.best.repair is not None
    assert result.best.repair["reason"] == "screw_clearance_wrong"
