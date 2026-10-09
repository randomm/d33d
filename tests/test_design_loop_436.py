"""Issue #436: loop-level wiring for the wrong-typed ``center=[...]`` routing
and the near-hollow (``hollow_solid``) post-check.

The helpers come from ``tests.test_design_loop``; these tests drive the
whole loop so the gating (``next_repair``, the ``_*_repair_fired`` guards,
the pass-path suppression) is exercised, not just the pure checks.
"""

from __future__ import annotations

from pathlib import Path

from d33d.design_loop import BboxInfo, run_design_loop
from tests.test_design_loop import (
    MAX_ITERATIONS,
    PHOTO,
    _render,
    _render_with_stl_and_bbox,
    _scad_llm,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCAD = FIXTURES / "scad"
STL = FIXTURES / "stl"
BBOX = BboxInfo(20, 20, 20, 8000.0)


def _loop_with_render(scad: str, render, *, request: str = "") -> object:
    llm = _scad_llm(scad)

    def render_fn(_scad, _defines):
        return render

    return run_design_loop(
        photo=PHOTO,
        stated_dims=(0.0, 0.0, 0.0),
        render_fn=render_fn,
        llm_fn=lambda role, messages, system: llm,
        bbox_fn=lambda r: BBOX,
        request=request,
        max_iterations=MAX_ITERATIONS,
    )


def test_vector_center_on_ok_render_routes_unknown_variable_repair():
    """Issue #436: an exit-0 render whose SCAD passes ``center=[...]`` to a
    primitive is NOT a pass; iteration 1 is classified ``unknown_variable``
    with evidence naming ``center`` and a repair is routed."""
    scad = (SCAD / "groove-center-vector.scad").read_text(encoding="utf-8")
    result = _loop_with_render(scad, _render())
    assert result.status != "pass"
    first = result.iterations[0]
    assert first.failure_class == "unknown_variable"
    assert first.repair is not None
    assert first.repair["failure_class"] == "unknown_variable"
    assert "center" in first.repair["evidence"]


def test_boolean_center_on_ok_render_passes():
    """Issue #436: the boolean ``center = true`` form is NOT routed by the
    center check (no ``unknown_variable`` class, no ``center`` evidence). The
    remaining bits are the stub vision critique's, which is out of scope."""
    scad = (SCAD / "groove-center-true.scad").read_text(encoding="utf-8")
    result = _loop_with_render(scad, _render())
    first = result.iterations[0]
    assert first.failure_class != "unknown_variable"
    assert first.repair is None or "center" not in first.repair["evidence"]


def test_near_hollow_render_routes_hollow_solid_repair():
    """Issue #436: an ok render whose solid is under 10% of its bounding box
    for a plain box request is routed ``geometrically_wrong`` with the
    ``hollow_solid`` loop reason; the candidate does not pass."""
    render = _render_with_stl_and_bbox(str(STL / "hollow-shell.stl"), BBOX)
    result = _loop_with_render("W = 20;\ncube([W, W, W]);\n", render, request="a box")
    assert result.status != "pass"
    first = result.iterations[0]
    assert first.failure_class == "geometrically_wrong"
    assert first.repair is not None
    assert first.repair["reason"] == "hollow_solid"
    assert "bounding box" in first.repair["evidence"]


def test_near_hollow_render_exempt_for_thin_request_passes():
    """Issue #436: a request naming a thin/hollow form (here "a shell") is
    exempt; the same near-hollow render passes with no hollow repair."""
    render = _render_with_stl_and_bbox(str(STL / "hollow-shell.stl"), BBOX)
    result = _loop_with_render("W = 20;\ncube([W, W, W]);\n", render, request="a shell")
    assert result.status == "pass"
    assert result.iterations[0].repair is None


def test_solid_render_passes_hollow_gate():
    """Issue #436: a solid render for a plain box request passes the hollow
    gate (ratio well above 10%) and passes the loop."""
    render = _render_with_stl_and_bbox(str(STL / "solid-block.stl"), BBOX)
    result = _loop_with_render("W = 20;\ncube([W, W, W]);\n", render, request="a box")
    assert result.status == "pass"
    assert result.iterations[0].repair is None
