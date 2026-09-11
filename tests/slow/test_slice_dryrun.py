"""Slow-layer tests for the headless slice dry run (ticket #4, gate 6).

These tests drive a REAL slicer binary (QIDI Studio → OrcaSlicer →
PrusaSlicer fallback, in that precedence) and therefore only run on a
box where a slicer is installed — the linux/amd64 target box, or a
dev machine with the app bundle.  Run with ``pytest -m slow``.

They are skipped (not failed) when no slicer binary is reachable —
the spike must not block the ticket, and a machine without a slicer
has no printability signal to assert.

Anchor fixture: ``tests/fixtures/stl/box_20mm.stl`` (the golden
render-worker STL, 20×20×20 mm).  It must slice to non-empty G-code,
and — for the acceptance criterion "a valid STL becomes a 3MF in mm
that loads in QIDI Studio and slices to non-empty G-code" — the 3MF
the pipeline exports from that STL must itself load and slice in the
target slicer.
"""

from __future__ import annotations

import inspect
import tempfile
from pathlib import Path

import pytest

from d33d import print_validation as pv
from d33d.slicer import (
    SliceDryRunResult,
    available_slicers,
    find_slicer,
    slice_dry_run,
)

STL_FIXTURE = Path(__file__).parent.parent / "fixtures" / "stl" / "box_20mm.stl"

pytestmark = pytest.mark.slow


def _slicer_available() -> bool:
    return bool(available_slicers())


def _skip_if_no_slicer():
    if not _slicer_available():
        pytest.skip("no slicer binary found (QIDI/Orca/Prusa) on this box")


def _stl_to_3mf(stl_path: Path, out_path: Path) -> Path:
    """Export the STL to a real 3MF (the pipeline's format) via trimesh."""
    import trimesh

    mesh = trimesh.load(str(stl_path), process=False)
    if isinstance(mesh, trimesh.Scene):
        mesh = next(iter(mesh.geometry.values()))
    mesh.export(str(out_path))
    return out_path


def test_stl_slices_to_non_empty_gcode():
    """A known-good STL slices to non-empty G-code via the real binary."""
    _skip_if_no_slicer()
    assert STL_FIXTURE.is_file(), f"missing fixture {STL_FIXTURE}"

    result = slice_dry_run(str(STL_FIXTURE))

    assert isinstance(result, SliceDryRunResult)
    assert result.ok, f"slice dry run failed ({result.slicer}): {result.error_string}"
    assert result.slicer in ("qidi", "orca", "prusa")
    assert result.gcode_path is not None
    assert Path(result.gcode_path).is_file()
    assert result.gcode_lines > 0
    # The gcode must contain movement/extrusion, not be a stub file.
    content = Path(result.gcode_path).read_text(errors="replace")
    assert any(
        line.startswith(("G1 ", "G0 ", "G0;", "G1;")) for line in content.splitlines()
    ), "G-code contains no G0/G1 movement lines"


def test_qidi_3mf_loads_and_slices():
    """Acceptance: the 3MF the pipeline emits loads and slices in the
    target slicer (QIDI Studio binary family) to non-empty G-code."""
    _skip_if_no_slicer()
    qidi = find_slicer("qidi") or find_slicer("orca")
    if qidi is None:
        pytest.skip(
            "no QIDI/Orca binary to prove the 3MF loads (Prusa fallback alone can't)"
        )

    import trimesh

    assert STL_FIXTURE.is_file(), f"missing fixture {STL_FIXTURE}"
    with tempfile.TemporaryDirectory(prefix="d33d_3mf_slice_") as tmp:
        three_mf = Path(tmp) / "model.3mf"
        _stl_to_3mf(STL_FIXTURE, three_mf)

        # Sanity: the 3MF must be a valid OPC zip declaring millimetre
        # units (unlike STL's unitless default), and the mesh must come
        # back in mm (20mm box → extents 20, not microns).
        import zipfile

        with zipfile.ZipFile(three_mf) as zf:
            model_xml = zf.read("3D/3dmodel.model").decode("utf-8")
        assert 'unit="millimeter"' in model_xml, (
            f"3MF does not declare millimetre units: {model_xml[:400]}"
        )
        loaded = trimesh.load(str(three_mf), process=False)
        if isinstance(loaded, trimesh.Scene):
            loaded = next(iter(loaded.geometry.values()))
        extents = loaded.extents
        assert max(extents) < 100.0, f"3MF mesh extents look non-mm: {extents}"

        result = slice_dry_run(str(three_mf))
        assert result.slicer in ("qidi", "orca"), f"unexpected slicer {result.slicer}"
        assert result.ok, f"3MF failed to load/slice: {result.error_string}"
        assert result.gcode_path is not None
        assert result.gcode_lines > 0


def test_over_envelope_slices_fail_loudly():
    """A mesh exceeding the build plate must fail the dry run with the
    slicer's own diagnosable error, not silently produce G-code."""
    _skip_if_no_slicer()
    qidi = find_slicer("qidi") or find_slicer("orca")
    if qidi is None:
        pytest.skip(
            "over-envelope behaviour is a QIDI/Orca plate check; no such binary here"
        )

    import trimesh

    # 350mm > 320mm X envelope (QIDI_PLUS_5_ENVELOPE_MM).
    with tempfile.TemporaryDirectory(prefix="d33d_oversize_") as tmp:
        big_stl = Path(tmp) / "over_env.stl"
        trimesh.creation.box((350.0, 30.0, 30.0)).export(str(big_stl))

        result = slice_dry_run(str(big_stl))

    assert not result.ok, f"over-envelope mesh unexpectedly sliced: {result.gcode_path}"
    assert result.slicer in ("qidi", "orca")
    assert result.gcode_path is None
    # The slicer reports the failure loudly (return_code -50 / error string
    # naming an empty or out-of-plate plate); we only require that the
    # driver surfaced a non-empty diagnosable error, not the exact text.
    assert result.error_string or result.detail, "failure had no diagnosable error"
    assert result.return_code != 0 or result.return_code == -1


def test_validate_stl_default_is_real_slicer():
    """Wiring proof for the slow/real-hardware path: when validate_stl is
    called WITHOUT an explicit slice_dry_run_fn override, the default must
    be the real ``slicer.slice_dry_run`` (not a stub). This is the wiring
    contract that makes gate 6 load-bearing on a real-hardware box where a
    slicer binary is installed."""
    sig = inspect.signature(pv.validate_stl)
    default_fn = sig.parameters["slice_dry_run_fn"].default
    assert default_fn is slice_dry_run, (
        f"validate_stl's default slice_dry_run_fn is {default_fn!r}, "
        "expected the real slicer.slice_dry_run"
    )


def test_missing_input_reported_as_failure():
    """A missing input file is a clean gate failure, never a crash."""
    _skip_if_no_slicer()
    result = slice_dry_run("/nonexistent/nope.stl")
    assert not result.ok
    assert result.slicer == "none"
    assert "does not exist" in result.detail


def test_result_contract_shape():
    """SliceDryRunResult exposes the fields the gate report needs —
    each field present, correct type, and the class table stays closed."""
    if _slicer_available():
        result = slice_dry_run(str(STL_FIXTURE))
    else:
        result = slice_dry_run("/nonexistent/nope.stl")

    assert isinstance(result, SliceDryRunResult)
    assert isinstance(result.ok, bool)
    assert isinstance(result.gcode_lines, int)
    assert isinstance(result.return_code, int)
    assert isinstance(result.error_string, str)
    assert (result.gcode_path is None) or isinstance(result.gcode_path, str)
    assert (result.objects is None) or isinstance(result.objects, int)
    if result.ok:
        assert result.gcode_lines > 0
        assert result.slicer in ("qidi", "orca", "prusa")
    else:
        assert result.gcode_path is None
