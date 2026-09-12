"""Per-gate determinism tests for the eval harness's gates 1–5
(issue #9, workstream ``task-gates``).

Each gate is exercised in isolation and as a whole via
:func:`d33d.evals.gates.run_gates_1_5`. The tests pin:

* gate 1 tags by the OpenSCAD LLM failure class, never "compile failed"
* gate 2 STL export succeeds / fails
* gate 3 watertight AND winding-consistent, asserted separately
* gate 4 bbox within ``max(1% of stated, 0.5mm)`` per axis — the
  expression boundary at 50mm (where 1% == 0.5mm exactly) and the
  regime where each clause dominates
* gate 5 volume > 0 AND face count within the named sanity bounds
* the per-gate taggability table (``geometrically_wrong`` is
  taggable only at gate 4; outcome classes are not taggable at any
  gate)
"""

from __future__ import annotations

from pathlib import Path

import pytest
import trimesh

from d33d import failure_classes as fc
from d33d import print_validation as pv
from d33d.evals import gates as eg
from d33d.render_worker import classify as rw_classify

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures" / "scad"

#: The render-worker stderr markers — used to exercise gate 1's
#: classification path without a Docker container.
_STDERR_BOSL2 = "ERROR: Unknown module bosl2_rounding"
_STDERR_HULL = "ERROR: hull() requires at least two children"
_STDERR_DIFFERENCE = "ERROR: difference() operand inversion"
_STDERR_UNCLASSIFIED = "ERROR: something unrecognised"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _box(extents: tuple[float, float, float]) -> trimesh.Trimesh:
    m = trimesh.creation.box(extents)
    m.units = "mm"
    return m


def _holey_box() -> trimesh.Trimesh:
    """A box with 2 faces removed — not watertight. The raw trimesh
    object (no pymeshfix repair) so the watertight property is the
    honest pre-repair state."""
    m = trimesh.creation.box((20.0, 20.0, 20.0))
    m.faces = m.faces[2:]
    m.units = "mm"
    return m


@pytest.fixture
def box_20mm() -> trimesh.Trimesh:
    """A 20×20×20mm box — the same golden fixture as #1's slow-layer
    test. Watertight, winding-consistent, volume 8000 mm³, 12 faces."""
    return _box((20.0, 20.0, 20.0))


@pytest.fixture
def box_50mm() -> trimesh.Trimesh:
    """A 50×50×50mm box — the exact boundary of the 1% vs 0.5mm
    tolerance expression (1% of 50mm == 0.5mm)."""
    return _box((50.0, 50.0, 50.0))


@pytest.fixture
def box_100mm() -> trimesh.Trimesh:
    """A 100×100×100mm box — 1% (1.0mm) dominates the 0.5mm floor."""
    return _box((100.0, 100.0, 100.0))


def _rw_error_class(
    exit_code: int = 0,
    stl: bool = True,
    csg: bool = True,
    views: bool = True,
    vertex_count: int = 24,
    watertight: bool = True,
    volume: float = 1000.0,
    stderr: str = "",
    timed_out: bool = False,
    oomkilled: bool = False,
) -> str:
    """Build a render-worker ``ErrorClass`` via the real classifier —
    no stubbed class, no forked table."""
    view_paths = tuple(str(f"view_{i:02d}.png") for i in range(6))
    return str(
        rw_classify(
            exit_code=exit_code,
            stl_path="model.stl" if stl else None,
            csg_path="model.csg" if csg else None,
            views=view_paths if views else (),
            stderr=stderr,
            oomkilled=oomkilled,
            timed_out=timed_out,
            vertex_count=vertex_count,
            watertight=watertight,
            volume=volume,
        )
    )


def _syntax_exit0(stl: bool = True) -> str:
    """A render-worker ``ErrorClass`` for a non-zero exit with an
    ``ERROR:`` stderr diagnostic."""
    return _rw_error_class(
        exit_code=1,
        stl=stl,
        stderr=_STDERR_UNCLASSIFIED,
    )


def _ok_result() -> str:
    return _rw_error_class(exit_code=0)


# ---------------------------------------------------------------------------
# Gate 1 — compiles, tagged by failure class
# ---------------------------------------------------------------------------


def test_gate1_ok_passes(box_20mm):
    r = eg.gate1_compiles("ok")
    assert r.gate == "compile"
    assert r.status == "pass"
    assert r.failure_class is None


def test_gate1_timeout_tagged(box_20mm):
    r = eg.gate1_compiles("timeout", stderr="timed out")
    assert r.status == "fail"
    assert r.failure_class == "timeout"
    assert r.failure_class in eg.GATE_TAGGABLE_CLASSES["compile"]


def test_gate1_oom_tagged(box_20mm):
    r = eg.gate1_compiles("oom")
    assert r.status == "fail"
    assert r.failure_class == "oom"


def test_gate1_container_error_tagged(box_20mm):
    r = eg.gate1_compiles("container_error")
    assert r.status == "fail"
    assert r.failure_class == "container_error"


def test_gate1_artifact_error_tagged(box_20mm):
    r = eg.gate1_compiles("artifact_error")
    assert r.status == "fail"
    assert r.failure_class == "artifact_error"


def test_gate1_empty_model_tagged(box_20mm):
    r = eg.gate1_compiles("empty_model")
    assert r.status == "fail"
    assert r.failure_class == "empty_model"


def test_gate1_syntax_error_hallucinated_bosl2(box_20mm):
    r = eg.gate1_compiles("syntax_error", stderr=_STDERR_BOSL2)
    assert r.status == "fail"
    assert r.failure_class == "hallucinated_bosl2"


def test_gate1_syntax_error_hull(box_20mm):
    r = eg.gate1_compiles("syntax_error", stderr=_STDERR_HULL)
    assert r.status == "fail"
    assert r.failure_class == "hull_miskowski_misuse"


def test_gate1_syntax_error_difference(box_20mm):
    r = eg.gate1_compiles("syntax_error", stderr=_STDERR_DIFFERENCE)
    assert r.status == "fail"
    assert r.failure_class == "difference_inversion"


def test_gate1_syntax_error_fallback_unclassified(box_20mm):
    r = eg.gate1_compiles("syntax_error", stderr=_STDERR_UNCLASSIFIED)
    assert r.status == "fail"
    assert r.failure_class == "unclassified_syntax_error"


def test_gate1_rejects_unknown_error_class(box_20mm):
    with pytest.raises(ValueError, match="closed enum"):
        eg.gate1_compiles("not_a_class")


# ---------------------------------------------------------------------------
# Gate 2 — STL export
# ---------------------------------------------------------------------------


def test_gate2_stl_path_present_passes(box_20mm):
    r = eg.gate2_stl_export("/tmp/model.stl")
    assert r.gate == "stl_export"
    assert r.status == "pass"
    assert r.failure_class is None


def test_gate2_missing_stl_fails_tagged_artifact(box_20mm):
    r = eg.gate2_stl_export(None)
    assert r.status == "fail"
    assert r.failure_class == "artifact_error"


def test_gate2_empty_stl_path_fails(box_20mm):
    r = eg.gate2_stl_export("")
    assert r.status == "fail"
    assert r.failure_class == "artifact_error"


# ---------------------------------------------------------------------------
# Gate 3 — watertight AND winding, separately
# ---------------------------------------------------------------------------


def test_gate3_watertight_winding_passes(box_20mm):
    r = eg.gate3_watertight(box_20mm)
    assert r.gate == "watertight"
    assert r.status == "pass"
    assert r.failure_class is None


def test_gate3_not_watertight_fails(box_20mm, tmp_path):
    m = _holey_box()
    r = eg.gate3_watertight(m)
    assert r.status == "fail"
    assert r.failure_class == "artifact_error"
    assert "not watertight" in r.detail


def test_gate3_not_winding_consistent_fails(tmp_path):
    # The winding_inverted fixture from #4's slow-layer tests.
    stl_path = Path(__file__).resolve().parent.parent / "fixtures" / "stl" / "winding_inverted.stl"
    m = trimesh.load(str(stl_path), process=False)
    assert isinstance(m, trimesh.Trimesh)
    r = eg.gate3_watertight(m)
    assert r.status == "fail"
    assert "not winding-consistent" in r.detail or "not watertight" in r.detail


# ---------------------------------------------------------------------------
# Gate 4 — bbox within max(1%, 0.5mm) per axis
# ---------------------------------------------------------------------------


def test_gate4_exact_dimensions_pass(box_20mm):
    r = eg.gate4_bbox(box_20mm, {"x": 20, "y": 20, "z": 20})
    assert r.gate == "bbox"
    assert r.status == "pass"
    assert r.failure_class is None


def test_gate4_no_stated_dims_is_na(box_20mm):
    r = eg.gate4_bbox(box_20mm, None)
    assert r.status == "na"
    assert r.failure_class is None


def test_gate4_50mm_boundary_exact_pass(box_50mm):
    """At exactly 50mm, 1% == 0.5mm — the threshold is exactly 0.5mm.
    A box that is exactly 50mm on each axis passes."""
    r = eg.gate4_bbox(box_50mm, {"x": 50, "y": 50, "z": 50})
    assert r.status == "pass"


def test_gate4_50mm_just_over_threshold_fails():
    """At 50mm stated, the tolerance is exactly 0.5mm. A box 50.5001mm
    on one axis (error 0.5001mm > 0.5mm) fails."""
    m = _box((50.5001, 50.0, 50.0))
    r = eg.gate4_bbox(m, {"x": 50, "y": 50, "z": 50})
    assert r.status == "fail"
    assert r.failure_class == "geometrically_wrong"


def test_gate4_05mm_dominates_small_dims():
    """For stated=10mm, the 0.5mm floor dominates (1% = 0.1mm).
    A box 10.4999mm on one axis (error 0.4999mm < 0.5mm) passes;
    10.5001mm (error 0.5001mm > 0.5mm) fails."""
    m_pass = _box((10.4999, 10.0, 10.0))
    r_pass = eg.gate4_bbox(m_pass, {"x": 10, "y": 10, "z": 10})
    assert r_pass.status == "pass"

    m_fail = _box((10.5001, 10.0, 10.0))
    r_fail = eg.gate4_bbox(m_fail, {"x": 10, "y": 10, "z": 10})
    assert r_fail.status == "fail"
    assert r_fail.failure_class == "geometrically_wrong"


def test_gate4_1pct_dominates_large_dims(box_100mm):
    """For stated=100mm, the 1% clause dominates (1% = 1.0mm).
    A box 100.9999mm on one axis (error 0.9999mm < 1.0mm) passes;
    101.0001mm (error 1.0001mm > 1.0mm) fails."""
    m_pass = _box((100.9999, 100.0, 100.0))
    r_pass = eg.gate4_bbox(m_pass, {"x": 100, "y": 100, "z": 100})
    assert r_pass.status == "pass"

    m_fail = _box((101.0001, 100.0, 100.0))
    r_fail = eg.gate4_bbox(m_fail, {"x": 100, "y": 100, "z": 100})
    assert r_fail.status == "fail"
    assert r_fail.failure_class == "geometrically_wrong"


def test_gate4_each_axis_checked_independently():
    """A box that is 20mm on x and y but 25mm on z fails gate 4 when
    stated z is 20mm — the per-axis check catches the z deviation even
    though x and y are fine."""
    m = _box((20.0, 20.0, 25.0))
    r = eg.gate4_bbox(m, {"x": 20, "y": 20, "z": 20})
    assert r.status == "fail"
    assert r.failure_class == "geometrically_wrong"


def test_gate4_rejects_missing_axis(box_20mm):
    with pytest.raises(ValueError, match="missing axis"):
        eg.gate4_bbox(box_20mm, {"x": 20, "y": 20})


def test_gate4_detail_names_axis():
    m = _box((20.0, 20.0, 30.0))
    r = eg.gate4_bbox(m, {"x": 20, "y": 20, "z": 20})
    assert r.status == "fail"
    assert "z" in r.detail


def test_gate4_tolerance_expression_reuses_pinned_constants(box_100mm):
    """The tolerance expression is the one from #4's
    ``dimension_error_ok`` — not a forked copy. A 100mm box with a
    99.0mm axis (error 1.0mm == 1% of 100mm) passes; 98.9999mm
    (error 1.0001mm) fails."""
    m_boundary = _box((99.0, 100.0, 100.0))
    r_boundary = eg.gate4_bbox(m_boundary, {"x": 100, "y": 100, "z": 100})
    assert r_boundary.status == "pass"

    m_just_over = _box((98.9999, 100.0, 100.0))
    r_just_over = eg.gate4_bbox(m_just_over, {"x": 100, "y": 100, "z": 100})
    assert r_just_over.status == "fail"


# ---------------------------------------------------------------------------
# Gate 5 — volume > 0 AND face count sane
# ---------------------------------------------------------------------------


def test_gate5_positive_volume_pass(box_20mm):
    r = eg.gate5_volume(box_20mm)
    assert r.gate == "volume"
    assert r.status == "pass"
    assert r.failure_class is None


def test_gate5_zero_volume_fails_empty_model(box_20mm, tmp_path):
    """A flat (zero-volume) mesh fails gate 5 with the ``empty_model``
    tag."""
    m = trimesh.creation.box((10.0, 10.0, 0.0))
    r = eg.gate5_volume(m)
    assert r.status == "fail"
    assert r.failure_class == "empty_model"


def test_gate5_face_count_below_min_fails(box_20mm, tmp_path):
    """A mesh with fewer than ``MIN_FACES`` faces fails gate 5 with the
    ``artifact_error`` tag (suspiciously few faces)."""
    # Build a mesh with exactly 2 faces (below MIN_FACES=4).
    m = trimesh.creation.box((10.0, 10.0, 10.0))
    m.faces = m.faces[:2]
    assert len(m.faces) == 2
    r = eg.gate5_volume(m)
    assert r.status == "fail"
    assert r.failure_class == "artifact_error"


def test_gate5_face_count_above_max_fails(box_20mm, tmp_path):
    """A mesh with more than ``MAX_FACES`` faces fails gate 5.
    We cannot build a 5M-face mesh in a fast test, so we patch
    ``MAX_FACES`` to a small value via the module constant."""
    import d33d.print_validation as pv_mod

    original_max = pv_mod.MAX_FACES
    try:
        pv_mod.MAX_FACES = 4  # Force a 12-face box to exceed MAX
        m = _box((10.0, 10.0, 10.0))
        assert len(m.faces) == 12
        r = eg.gate5_volume(m)
        assert r.status == "fail"
        assert r.failure_class == "artifact_error"
    finally:
        pv_mod.MAX_FACES = original_max


def test_gate5_uses_named_constants(box_20mm):
    """Gate 5 reads the face-count bounds from ``print_validation``
    (reused, not redefined). A 12-face box is within [MIN_FACES,
    MAX_FACES] and passes."""
    assert pv.MIN_FACES <= 12 <= pv.MAX_FACES
    r = eg.gate5_volume(box_20mm)
    assert r.status == "pass"


# ---------------------------------------------------------------------------
# Runner — gates 1–5 in pinned order
# ---------------------------------------------------------------------------


def test_runner_all_pass(box_20mm):
    report = eg.run_gates_1_5(
        case_id="test-box-20mm",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=box_20mm,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    assert report.ok is True
    for gate in eg.GATE_NAMES:
        assert report.gate(gate).status == "pass", f"gate {gate} failed"


def test_runner_gate1_failure_short_circuits(box_20mm):
    """A gate 1 failure means gates 2–5 are not reached — the report
    carries only the gate 1 result."""
    report = eg.run_gates_1_5(
        case_id="test-fail-compile",
        render_error_class="timeout",
        stderr="timed out",
        stl_path="model.stl",
        mesh=box_20mm,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    assert report.ok is False
    assert report.gate("compile").status == "fail"
    assert report.gate("stl_export") is None
    assert report.gate("watertight") is None
    assert report.gate("bbox") is None
    assert report.gate("volume") is None


def test_runner_gate2_failure_short_circuits(box_20mm):
    report = eg.run_gates_1_5(
        case_id="test-fail-stl",
        render_error_class="ok",
        stl_path=None,
        mesh=box_20mm,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    assert report.ok is False
    assert report.gate("stl_export").status == "fail"
    assert report.gate("watertight") is None


def test_runner_gate3_failure_short_circuits(box_20mm):
    m = _holey_box()
    report = eg.run_gates_1_5(
        case_id="test-fail-watertight",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=m,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    assert report.ok is False
    assert report.gate("watertight").status == "fail"
    assert report.gate("bbox") is None
    assert report.gate("volume") is None


def test_runner_gate4_failure_short_circuits(box_20mm):
    report = eg.run_gates_1_5(
        case_id="test-fail-bbox",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=box_20mm,
        expected_dims={"x": 20, "y": 20, "z": 40},
    )
    assert report.ok is False
    assert report.gate("bbox").status == "fail"
    assert report.gate("volume") is None


def test_runner_gate4_na_still_reaches_gate5(box_20mm):
    """When gate 4 is ``"na"`` (no stated dims), gate 5 still runs."""
    report = eg.run_gates_1_5(
        case_id="test-na-bbox",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=box_20mm,
        expected_dims=None,
    )
    assert report.ok is True
    assert report.gate("bbox").status == "na"
    assert report.gate("volume").status == "pass"


def test_runner_gate5_failure_reports_all(box_20mm):
    m = trimesh.creation.box((10.0, 10.0, 0.0))
    report = eg.run_gates_1_5(
        case_id="test-fail-volume",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=m,
        expected_dims={"x": 10, "y": 10, "z": 0},
    )
    # A zero-height box: gate 3 may or may not pass (watertightness of a
    # flat box), but gate 5 must fail.
    assert report.gate("volume") is not None
    assert report.gate("volume").status == "fail"
    assert report.gate("volume").failure_class == "empty_model"


def test_runner_to_dict_is_serializable(box_20mm):
    report = eg.run_gates_1_5(
        case_id="test-serialise",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=box_20mm,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    d = report.to_dict()
    assert d["case_id"] == "test-serialise"
    assert d["ok"] is True
    assert "gates" in d
    for name in eg.GATE_NAMES:
        assert name in d["gates"]
        assert d["gates"][name]["status"] == "pass"


def test_runner_no_mesh_fails_gate3(box_20mm):
    """When no mesh is supplied, gate 3 fails (no mesh to validate)."""
    report = eg.run_gates_1_5(
        case_id="test-no-mesh",
        render_error_class="ok",
        stl_path="model.stl",
        mesh=None,
        expected_dims={"x": 20, "y": 20, "z": 20},
    )
    assert report.ok is False
    assert report.gate("watertight").status == "fail"
