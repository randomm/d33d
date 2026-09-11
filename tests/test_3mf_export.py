"""Fast-layer tests for 3MF export (ticket #4).

The output 3MF must declare millimetre units explicitly (not STL's
unitless default), contain a single part in v1, and the return contract
must be shaped for N parts now.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest
import trimesh

from d33d import print_validation as pv
from d33d.slicer import SliceDryRunResult


def _passing_slice_fn(model_path: str, output_dir: str | None) -> SliceDryRunResult:
    """Injected gate-6 stub that passes without a real slicer binary (see
    test_validation_pipeline._passing_slice_fn for the full rationale)."""
    return SliceDryRunResult(
        ok=True,
        slicer="stub",
        gcode_path="stub.gcode",
        gcode_lines=1,
        return_code=0,
        error_string="",
        objects=1,
        detail="fast-layer stub (no real slicer binary needed)",
    )


@pytest.fixture(scope="module")
def valid_stl(tmp_path_factory) -> Path:
    unit = trimesh.creation.box((1.0, 1.0, 1.0))
    unit.apply_scale(20.0)
    path = tmp_path_factory.mktemp("valid") / "model.stl"
    unit.export(path)
    return path


@pytest.fixture(scope="module")
def validated(valid_stl) -> pv.ValidationResult:
    result = pv.validate_stl(str(valid_stl), slice_dry_run_fn=_passing_slice_fn)
    assert result.ok
    return result


# ---------------------------------------------------------------------------
# 3MF declares millimetre units explicitly
# ---------------------------------------------------------------------------


def test_3mf_declares_millimetre_units(validated):
    """The output 3MF must declare millimetre units explicitly, not STL's
    unitless default."""
    assert validated.export_3mf is not None
    path = validated.export_3mf
    assert path.endswith(".3mf")
    with zipfile.ZipFile(path) as z:
        names = z.namelist()
        assert any(n.endswith(".model") or n == "3D/3dmodel.model" for n in names)
        model_xml = None
        for n in names:
            if n.endswith(".model"):
                model_xml = z.read(n).decode("utf-8")
                break
        assert model_xml is not None
        # The 3MF XML must declare unit="millimeter" explicitly
        assert 'unit="millimeter"' in model_xml or 'unit="mm"' in model_xml


def test_3mf_is_valid_zip(validated):
    """The 3MF file must be a valid ZIP archive."""
    assert validated.export_3mf is not None
    with zipfile.ZipFile(validated.export_3mf) as z:
        assert z.testzip() is None


def test_3mf_contains_single_part(validated):
    """In v1 the 3MF contains exactly one part."""
    assert len(validated.parts) == 1
    assert validated.parts[0].name is not None
    assert len(validated.parts[0].name) > 0


# ---------------------------------------------------------------------------
# N-part contract shape
# ---------------------------------------------------------------------------


def test_contract_shaped_for_n_parts(validated):
    """The return contract is shaped for N parts now:
    {"parts": [Part] (len 1),
     "assembly": {"joints": [], "layout": []},
     "export": {"3mf": path}}
    so multi-part is not a retrofit later."""
    # parts is a list of length 1 in v1
    assert isinstance(validated.parts, list)
    assert len(validated.parts) == 1

    # assembly has empty joints and layout
    assert isinstance(validated.assembly_joints, list)
    assert len(validated.assembly_joints) == 0
    assert isinstance(validated.assembly_layout, list)
    assert len(validated.assembly_layout) == 0

    # export carries the 3MF path
    assert isinstance(validated.export_3mf, str)
    assert validated.export_3mf.endswith(".3mf")
    # The file must exist
    assert Path(validated.export_3mf).exists()


def test_contract_is_not_a_retrofit(validated):
    """The contract shape means adding a second part later doesn't
    require changing the return type — parts is already a list."""
    # parts is a list, not a single Part
    assert isinstance(validated.parts, list)
    # assembly_joints and assembly_layout are lists, not single values
    assert isinstance(validated.assembly_joints, list)
    assert isinstance(validated.assembly_layout, list)


# ---------------------------------------------------------------------------
# 3MF loads and is valid
# ---------------------------------------------------------------------------


def test_3mf_loads_in_trimesh(validated):
    """The exported 3MF must load back in trimesh as a valid mesh."""
    assert validated.export_3mf is not None
    loaded = trimesh.load(validated.export_3mf, process=False)
    # trimesh may return a Scene or a Trimesh
    if isinstance(loaded, trimesh.Scene):
        assert len(loaded.geometry) == 1
        geo = next(iter(loaded.geometry.values()))
        assert geo.is_watertight
        assert geo.is_winding_consistent
        assert geo.volume > 0
    else:
        assert loaded.is_watertight
        assert loaded.is_winding_consistent
        assert loaded.volume > 0


def test_3mf_units_are_millimeters(validated):
    """The 3MF declares millimetre units explicitly."""
    assert validated.export_3mf is not None
    loaded = trimesh.load(validated.export_3mf, process=False)
    if isinstance(loaded, trimesh.Scene):
        geo = next(iter(loaded.geometry.values()))
        assert geo.units == "millimeter" or geo.units == "mm"
    else:
        assert loaded.units == "millimeter" or loaded.units == "mm"
