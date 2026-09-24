"""Issue #100: the dimensional gate is unsatisfiable for multi-part requests.

The fix has two independent parts:

1. **Bbox gate** (``d33d/design_loop.py`` + ``d33d/design_loop_events.py``):
   ``bbox_from_render`` now runs ``merge_vertices()`` +
   ``split(only_watertight=True)`` internally and carries the per-component
   breakdown on ``BboxInfo.components``. The stated triple is compared
   against the BEST-MATCHING component (total ordering: per-axis-difference
   ASC, volume DESC, min_x ASC, min_y ASC, min_z ASC) instead of the
   whole-assembly bbox. A zero-component split returns None (the gate
   fails — never a vacuous pass). The abstain path (``target <= 0``) runs
   before any component work, exactly as before.

2. **Magic-number gate** (``d33d/failure_classes.py``):
   ``detect_magic_numbers`` now honours the ``stated_dimensions`` argument
   (exact float equality) and exempts numeric elements of
   ``translate``/``rotate`` placement vectors. A nested SIZE literal inside
   the vector is still flagged. ``mirror`` is NOT added to the regex
   alternation (it was never flagged; adding it would widen the gate).

Fast layer: in-memory trimesh meshes + the real ``box_20mm.stl`` fixture.
No Docker, no real LLM.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import numpy as np
import pytest
import trimesh

from d33d.design_loop import (
    BboxInfo,
    _bbox_within_tolerance,
    best_match_component,
    score,
)
from d33d.design_loop_events import bbox_from_render
from d33d.failure_classes import detect_magic_numbers
from d33d.render_worker import RenderResult

FIXTURES_STL = Path(__file__).parent / "fixtures" / "stl"
FIXTURES_SCAD = Path(__file__).parent / "fixtures" / "scad"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_render(stl: str | None) -> RenderResult:
    return RenderResult(
        ok=True, exit_code=0, duration_ms=1, error_class="ok",
        stderr="", stl=stl, csg=None, views=(),
    )


def _two_body_mesh() -> trimesh.Trimesh:
    """20mm box + 10mm sphere (r=5) disjoint at x=25 — the reported case."""
    box = trimesh.creation.box(extents=[20, 20, 20])
    sphere = trimesh.creation.icosphere(subdivisions=3, radius=5)
    sphere.apply_translation([25, 0, 0])
    return trimesh.util.concatenate([box, sphere])


def _export_mesh(mesh: trimesh.Trimesh) -> str:
    fd, path = tempfile.mkstemp(suffix=".stl")
    os.close(fd)
    mesh.export(path)
    return path


# ---------------------------------------------------------------------------
# Bbox gate: two-body mesh
# ---------------------------------------------------------------------------


def test_two_body_bbox_bit_true_via_best_component() -> None:
    """Decisive: 20mm box + 10mm sphere at x=25, stated (20,20,20) →
    bbox bit True via the best-matching component (the box), not the
    whole-assembly bbox (40mm wide)."""
    stl = _export_mesh(_two_body_mesh())
    try:
        bbox = bbox_from_render(_make_render(stl))
        assert bbox is not None
        assert bbox.x == pytest.approx(40.0)
        assert len(bbox.components) == 2
        best = best_match_component(bbox, (20.0, 20.0, 20.0))
        assert best == pytest.approx((20.0, 20.0, 20.0))
        assert _bbox_within_tolerance(bbox, (20.0, 20.0, 20.0)) is True
    finally:
        os.unlink(stl)


def test_two_body_bbox_bit_true_for_sphere_stated() -> None:
    """Stated (10,10,10) → best match is the sphere."""
    stl = _export_mesh(_two_body_mesh())
    try:
        bbox = bbox_from_render(_make_render(stl))
        assert bbox is not None
        best = best_match_component(bbox, (10.0, 10.0, 10.0))
        assert best == pytest.approx((10.0, 10.0, 10.0))
        assert _bbox_within_tolerance(bbox, (10.0, 10.0, 10.0)) is True
    finally:
        os.unlink(stl)


def test_two_body_bbox_bit_false_for_mismatch() -> None:
    """Stated (30,30,30) → no component matches → bbox bit False."""
    stl = _export_mesh(_two_body_mesh())
    try:
        bbox = bbox_from_render(_make_render(stl))
        assert bbox is not None
        assert _bbox_within_tolerance(bbox, (30.0, 30.0, 30.0)) is False
    finally:
        os.unlink(stl)


# ---------------------------------------------------------------------------
# Single-body: today's behaviour preserved
# ---------------------------------------------------------------------------


def test_single_body_passes_with_matching_dims() -> None:
    bbox = BboxInfo(20.0, 20.0, 20.0, 8000.0)
    assert _bbox_within_tolerance(bbox, (20.0, 20.0, 20.0)) is True


def test_single_body_fails_with_mismatched_dims() -> None:
    bbox = BboxInfo(20.0, 20.0, 20.0, 8000.0)
    assert _bbox_within_tolerance(bbox, (30.0, 30.0, 30.0)) is False


# ---------------------------------------------------------------------------
# Production fixture: box_20mm.stl
# ---------------------------------------------------------------------------


def test_box_20mm_splits_to_one_component_and_passes() -> None:
    """The production face-disconnected STL splits to exactly ONE
    component AFTER merge_vertices and passes the gate."""
    stl = str(FIXTURES_STL / "box_20mm.stl")
    bbox = bbox_from_render(_make_render(stl))
    assert bbox is not None
    assert bbox.x == pytest.approx(20.0)
    assert bbox.volume == pytest.approx(8000.0)
    assert len(bbox.components) == 1
    assert bbox.components[0][:3] == pytest.approx((20.0, 20.0, 20.0))
    assert _bbox_within_tolerance(bbox, (20.0, 20.0, 20.0)) is True


def test_box_20mm_unmerged_yields_zero_components() -> None:
    """RED-CHECK GUARD: without merge_vertices, the production STL yields
    ZERO components. If this ever fails, the merge may no longer be
    needed — report to PM before changing the code."""
    stl = str(FIXTURES_STL / "box_20mm.stl")
    mesh = trimesh.load(stl, process=False)
    assert len(mesh.split(only_watertight=True)) == 0


def test_merge_vertices_is_load_bearing() -> None:
    """Documents that merge_vertices() is load-bearing: unmerged → 0
    components, merged → exactly 1. A test that cannot fail on the bug
    it guards is decoration."""
    stl = str(FIXTURES_STL / "box_20mm.stl")
    mesh = trimesh.load(stl, process=False)
    assert len(mesh.split(only_watertight=True)) == 0
    mesh.merge_vertices()
    assert len(mesh.split(only_watertight=True)) == 1


# ---------------------------------------------------------------------------
# Zero-component split → gate fails (never a vacuous pass)
# ---------------------------------------------------------------------------


def test_zero_component_split_returns_none() -> None:
    """A mesh that splits to zero components (genuinely broken) →
    bbox_from_render returns None (the gate fails) — never a vacuous
    pass.

    A real mesh is hard to force through the STL round-trip AND
    merge_vertices into the empty-split state (trimesh's STL export
    often repairs topology), so ``trimesh.load`` is mocked to return a
    mesh whose ``split(only_watertight=True)`` yields an EMPTY LIST —
    exercising the exact production branch (``if not split_result:
    return None``), not a hand-built BboxInfo."""

    class _ZeroSplitMesh:
        bounds = np.array([[0.0, 0.0, 0.0], [40.0, 20.0, 20.0]])
        volume = 12000.0

        def merge_vertices(self) -> _ZeroSplitMesh:
            return self

        def split(self, only_watertight: bool = True) -> list:
            return []

    with patch("trimesh.load", return_value=_ZeroSplitMesh()):
        assert bbox_from_render(_make_render(str(FIXTURES_STL / "box_20mm.stl"))) is None


def test_split_exception_returns_none_not_whole_part() -> None:
    """A split that RAISES on a loaded mesh → bbox_from_render returns
    None (the gate fails loudly) — never the legacy whole-part path,
    which would silently re-create the issue #100 defect for every
    multi-part mesh. (A zero-assertion test of the contract it names
    was this branch's original home — this one asserts the branch.)"""

    class _SplitRaisesMesh:
        bounds = np.array([[0.0, 0.0, 0.0], [40.0, 20.0, 20.0]])
        volume = 12000.0

        def merge_vertices(self) -> _SplitRaisesMesh:
            return self

        def split(self, only_watertight: bool = True) -> list:
            raise RuntimeError("synthetic split failure")

    with patch("trimesh.load", return_value=_SplitRaisesMesh()):
        assert bbox_from_render(_make_render(str(FIXTURES_STL / "box_20mm.stl"))) is None


def test_none_bbox_gate_bit_false() -> None:
    """bbox=None → bbox bit False with failure_reason
    'bbox_out_of_tolerance' — never a silent pass."""
    render = RenderResult(
        ok=True, exit_code=0, duration_ms=1, error_class="ok",
        stderr="", stl=None, csg=None, views=("v",) * 6,
    )
    s = score(render, (20.0, 20.0, 20.0), bbox=None)
    assert s.bits[2] is False
    assert s.bbox_within_tolerance is False


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_best_match_deterministic_across_orderings() -> None:
    """Best-match selection is identical across repeated runs and
    independent of component ordering.

    The two components have DIFFERENT extents (10 vs 20 on axis 0) and the
    stated triple is (10, 10, 10), so the 10-box (component A, per-axis
    diff 0) wins regardless of ordering — the returned value identifies
    WHICH component was selected (a broken ordering that picked the
    20-box would fail this assertion)."""
    comp_a = (10.0, 10.0, 10.0, 8000.0, 0.0, 0.0, 0.0)
    comp_b = (20.0, 20.0, 20.0, 5000.0, 25.0, 0.0, 0.0)
    bbox_1 = BboxInfo(30.0, 20.0, 20.0, 13000.0, components=(comp_a, comp_b))
    bbox_2 = BboxInfo(30.0, 20.0, 20.0, 13000.0, components=(comp_b, comp_a))
    stated = (10.0, 10.0, 10.0)
    best_1 = best_match_component(bbox_1, stated)
    best_2 = best_match_component(bbox_2, stated)
    assert best_1 == best_2 == pytest.approx((10.0, 10.0, 10.0))
    for _ in range(5):
        assert best_match_component(bbox_1, stated) == best_1


def test_best_match_tie_broken_by_volume() -> None:
    """Two components TIE on per-axis diff (both sum to 20 against the
    stated 10mm triple) with DIFFERENT extents and DIFFERENT volumes, so
    which one is selected is observable in the returned value: the
    volume-desc tie-break picks the larger-volume (20, 20, 10) box. An
    ordering that broke the tie by volume ASC (or min_x ASC, or
    arbitrarily) would return the other box's (30, 10, 10) extents and
    fail this assertion.

    comp_a: extents (30, 10, 10), volume 3000, min_x 15 — per-axis diff
            |30-10| + 0 + 0 = 20
    comp_b: extents (20, 20, 10), volume 4000, min_x 0  — per-axis diff
            10 + 10 + 0 = 20

    Equal diff sums, different volumes (3000 vs 4000): only the volume
    tie-break can distinguish them. (If the volumes were equal too, the
    break would fall through to min_x, so the distinct volumes are what
    make this a volume-break test rather than a min_x-break test.)"""
    comp_a = (30.0, 10.0, 10.0, 3000.0, 15.0, 0.0, 0.0)  # diff 20, vol 3000
    comp_b = (20.0, 20.0, 10.0, 4000.0, 0.0, 0.0, 0.0)  # diff 20, vol 4000
    stated = (10.0, 10.0, 10.0)
    bbox = BboxInfo(50.0, 20.0, 10.0, 7000.0, components=(comp_a, comp_b))
    best = best_match_component(bbox, stated)
    # Volume-desc tie-break: comp_b (larger volume) wins; comp_a has the
    # smaller min_x, so min_x-asc would pick comp_a — the returned extents
    # are the observable selection.
    assert best == pytest.approx((20.0, 20.0, 10.0))
    # Same selection regardless of component order.
    bbox_rev = BboxInfo(50.0, 20.0, 10.0, 7000.0, components=(comp_b, comp_a))
    best_rev = best_match_component(bbox_rev, stated)
    assert best_rev == best == pytest.approx((20.0, 20.0, 10.0))


# ---------------------------------------------------------------------------
# Abstain (issue #91 semantics preserved)
# ---------------------------------------------------------------------------


def test_abstain_before_component_work() -> None:
    """Unknown axis (<=0) → abstain BEFORE any component matching."""
    bbox = BboxInfo(20.0, 20.0, 20.0, 8000.0, components=(
        (20.0, 20.0, 20.0, 8000.0, 0.0, 0.0, 0.0),
    ))
    assert _bbox_within_tolerance(bbox, (20.0, 0.0, 20.0)) is True
    assert _bbox_within_tolerance(bbox, (30.0, 30.0, 30.0)) is False
    assert _bbox_within_tolerance(bbox, (20.0, 20.0, 20.0)) is True


def test_partial_triple_checks_only_confirmed_axes() -> None:
    """Issue #247: a PARTIAL confirmed triple (only H confirmed, W/D
    unconfirmed) checks ONLY the confirmed axis — the unconfirmed axes
    are SKIPPED (per-axis abstention), not a whole-gate abstention.

    - H=20 confirmed, render z=20 → H is within tolerance → True.
    - H=20 confirmed, render z=30 → H is OUT of tolerance → False (the
      10 mm error is caught — the old all-or-nothing rule would have
      abstained and missed it).
    - No axis confirmed (all zero) → the gate abstains (True)."""
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)
    # Only H confirmed (z is the axis that matters here).
    assert _bbox_within_tolerance(bbox, (0.0, 0.0, 20.0)) is True
    # H out of tolerance → False (the 10 mm error is caught).
    bbox_tall = BboxInfo(x=20.0, y=20.0, z=30.0, volume=12000.0)
    assert _bbox_within_tolerance(bbox_tall, (0.0, 0.0, 20.0)) is False
    # No axis confirmed → abstain (True).
    assert _bbox_within_tolerance(bbox, (0.0, 0.0, 0.0)) is True


def test_partial_triple_multi_part_uses_whole_mesh_extents() -> None:
    """Issue #247: with a PARTIAL confirmed set and a multi-part mesh,
    the confirmed axes are compared against the WHOLE-MESH extents (not
    a component — there is no well-defined component selection without a
    full triple). A component's z can pass while the assembly's z fails.
    """
    # Two components: a 20mm body (z=20) and a 30mm body (z=30).
    bbox = BboxInfo(
        x=50.0, y=30.0, z=30.0,  # whole-mesh extents
        volume=27000.0,
        components=(
            (20.0, 20.0, 20.0, 8000.0, 0.0, 0.0, 0.0),  # small body
            (30.0, 30.0, 30.0, 27000.0, 0.0, 0.0, 0.0),  # tall body
        ),
    )
    # Only H confirmed (20mm): the small component's z=20 would pass, but
    # the whole-mesh z=30 is OUT of tolerance → False.
    assert _bbox_within_tolerance(bbox, (0.0, 0.0, 20.0)) is False
    # H=30 confirmed: the whole-mesh z=30 is within tolerance → True.
    assert _bbox_within_tolerance(bbox, (0.0, 0.0, 30.0)) is True


def test_partial_single_axis_h_12_z_19_3_fails() -> None:
    """Issue #247 ACCEPTANCE CRITERION: a message confirming only H=12
    with a candidate whose z=19.3 → gate FAILS on H (the 7 mm error is
    caught). H=12 and z=12.2 → passes (within max(0.12, 0.5mm) tol)."""
    # H=12 confirmed, z=19.3 → |19.3-12| = 7.3 > max(12*0.01, 0.5)=0.5 → FAIL.
    bbox = BboxInfo(x=21.21, y=21.43, z=19.3, volume=6369.8)
    assert _bbox_within_tolerance(bbox, (0.0, 0.0, 12.0)) is False
    # H=12 confirmed, z=12.2 → |12.2-12| = 0.2 <= 0.5 → PASS.
    bbox_ok = BboxInfo(x=21.21, y=21.43, z=12.2, volume=6369.8)
    assert _bbox_within_tolerance(bbox_ok, (0.0, 0.0, 12.0)) is True


# ---------------------------------------------------------------------------
# Magic numbers: both directions
# ---------------------------------------------------------------------------


def test_magic_multi_part_scad_passes() -> None:
    """The reported multi-part SCAD passes the named-params bit: the
    placement literal in translate is exempt."""
    scad = (
        "W = 20;\nD = 20;\nH = 20;\n"
        "cube([W, D, H]);\n"
        "translate([20, 0, 0])\n"
        "sphere(d = 10);\n"
    )
    assert detect_magic_numbers(scad) is False
    assert detect_magic_numbers(
        scad, stated_dimensions={"W": 20.0, "D": 20.0, "H": 20.0}
    ) is False


def test_magic_multi_part_scad_fixture_passes() -> None:
    """The reported multi-part SCAD (issue #100 live repro: "create a
    20mm cube with a 10mm sphere next to it") pinned against the
    archived fixture — the acceptance criterion, not a hand-written
    stand-in. The fixture is the parametric W/D/H block + the placement
    translate on its own line + the sphere's d=10 inlined.

    The gate's regex matches only the FIRST literal immediately after a
    call's opening paren (or its optional ``[``): for this fixture that
    is the ``20`` in ``translate([20, 0, 0])``. The ``10`` in
    ``sphere(d = 10)`` is invisible to the regex (the ``d = `` between
    the paren and the literal breaks the match). So the gate's verdict
    on this fixture is determined by the 20 alone: declared → exempt
    (both the no-stated-dims call and the explicit stated-dims call
    pass). The 10 being invisible is a pre-existing regex limitation,
    not a regression — the fully-parametric variant below (where the 10
    is declared as ``S = 10``) is the shape the gate was designed for.
    """
    scad = _load_fixture("issue-100-multipart.scad")
    # The /chat path (stated dims supplied): the 20 is a stated value —
    # exempt (the acceptance criterion).
    assert detect_magic_numbers(
        scad, stated_dimensions={"W": 20.0, "D": 20.0, "H": 20.0, "S": 10.0}
    ) is False
    # The loop's gate-4 call (no stated dims): the 20 is declared —
    # exempt (declared values are stated values, issue #100).
    assert detect_magic_numbers(scad) is False


def test_magic_named_param_scad_passes_without_dims() -> None:
    """The fully parametric variant (W/D/H + declared sphere diameter,
    placement literal) passes the gate even with NO stated dimensions
    — the idealized acceptance shape, where every literal is either
    declared or a placement element."""
    scad = (
        "W = 20;\nD = 20;\nH = 20;\nS = 10;\n"
        "cube([W, D, H]);\n"
        "translate([20, 0, 0]) sphere(d = S);\n"
    )
    assert detect_magic_numbers(scad) is False


def test_magic_reported_scad_with_inlined_sphere_fails() -> None:
    """A multi-part SCAD with an UNDECLARED, UNSTATED 2-digit literal in
    the FIRST position after a call's paren (the sphere's inlined d=33
    as the first arg — the shape the gate's regex actually sees)
    flags the 33: the gate's documented purpose (catch an undeclared
    fit-critical literal) survives the placement exemption. The
    placement 20 in translate is exempt (a stated/declared value);
    only the inlined 33 fails the gate.

    The live measurement's failure reason was bbox_out_of_tolerance, not
    stated_dims_not_named_parameters, so the named-params bit was passing
    on the model's actual SCAD (fully parametric); this test pins the
    boundary of the fix: an undeclared, unstated literal in a position
    the gate's regex can see is still magic."""
    scad = (
        "W = 20;\nD = 20;\nH = 20;\n"
        "cube([W, D, H]);\n"
        "translate([20, 0, 0])\n"
        "sphere(33);\n"
    )
    # The 33 is the first literal after sphere's paren — the gate sees it.
    # Not declared, not stated → flagged.
    assert detect_magic_numbers(
        scad, stated_dimensions={"W": 20.0, "D": 20.0, "H": 20.0}
    ) is True


def test_magic_genuinely_hardcoded_still_fails() -> None:
    """A hardcoded fit-critical literal (not stated, not placement)
    STILL fails."""
    scad = "cube([25, 20, 30]);"
    assert detect_magic_numbers(scad) is True
    assert detect_magic_numbers(scad, stated_dimensions={"W": 20.0}) is True


def test_magic_stated_dimension_passes() -> None:
    """A literal equal to a stated dimension (exact float) passes."""
    scad = "cube([20, 25, 30]);"
    assert detect_magic_numbers(
        scad, stated_dimensions={"width": 20.0}
    ) is False
    # Float trap: "20" vs "20.0"
    scad2 = "cube([20.0, 25, 30]);"
    assert detect_magic_numbers(
        scad2, stated_dimensions={"width": 20.0}
    ) is False
    # Non-equal value still fails
    assert detect_magic_numbers(
        scad, stated_dimensions={"width": 20.5}
    ) is True


def test_magic_placement_in_translate_passes() -> None:
    scad = "translate([20, 0, 0]) cube(5);"
    assert detect_magic_numbers(scad) is False


def test_magic_placement_in_rotate_passes() -> None:
    scad = "rotate([0, 0, 20]) cube(5);"
    assert detect_magic_numbers(scad) is False


def test_magic_rotate_named_axis_vector_passes_with_stated() -> None:
    """rotate(angle, v=[x,y,z]) named-argument axis vector: the gate's
    regex matches only the FIRST literal after the call paren (the
    angle, not the axis vector elements). The angle passes when stated;
    the axis vector elements are invisible to the gate (a pre-existing
    regex limitation, not a regression)."""
    scad = "rotate(45, v=[0, 0, 20]) cube(5);"
    # 45 stated → exempt; the 20 in the vector is invisible to the regex
    assert detect_magic_numbers(
        scad, stated_dimensions={"z": 20.0, "a": 45.0}
    ) is False
    # 45 NOT stated → flagged (the gate's only visible literal)
    assert detect_magic_numbers(
        scad, stated_dimensions={"z": 20.0}
    ) is True


def test_magic_rotate_vector_first_element_passes_with_stated() -> None:
    """rotate([20, 0, 0]) — the 20 is the first element of the axis
    vector (immediately after ``[``): passes when 20 is stated."""
    scad = "rotate([20, 0, 0]) cube(5);"
    assert detect_magic_numbers(
        scad, stated_dimensions={"z": 20.0}
    ) is False


def test_magic_placement_chain_passes_with_stated_dims() -> None:
    """A standard placement chain on one line (the adversarial review's
    probe ``translate([10,0,0]) rotate(20) cube(5);``): the 20 is a
    rotate ARGUMENT, not a vector element — with no stated dimensions
    it stays flagged (the gate flags MORE on an imperfect line, never
    less); with the stated dims the loop actually carries it passes —
    the shape the live model's output needed."""
    scad = "translate([10, 0, 0]) rotate(20) cube(5);"
    assert detect_magic_numbers(scad) is True
    assert detect_magic_numbers(
        scad, stated_dimensions={"a": 20.0}
    ) is False


def test_magic_nested_size_in_vector_still_flagged() -> None:
    """A SIZE literal nested inside a translate vector is still flagged."""
    scad = "translate([cube(20), 0, 0]);"
    assert detect_magic_numbers(scad) is True


def test_magic_stated_dimensions_now_honoured() -> None:
    """The stated_dimensions argument is now HONORED (was previously
    ignored — the old test asserted the parameter was inert)."""
    scad = "cube([20, 25, 30]);"
    stated = {"width": 20.0, "height": 25.0, "depth": 30.0}
    assert detect_magic_numbers(scad, stated_dimensions=stated) is False
    assert detect_magic_numbers(scad) is True


def _load_fixture(name: str) -> str:
    """Load a .scad fixture from tests/fixtures/scad/."""
    path = FIXTURES_SCAD / name
    assert path.exists(), f"Missing fixture: {name}"
    return path.read_text()
