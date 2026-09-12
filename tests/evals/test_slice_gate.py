"""Eval harness gate 6 — slice dry run — test suite (issue #9, workstream
task-slice).

Gate 6 is DELEGATED to the print-validation pipeline (ticket #4): the eval
harness only invokes ``validate_stl`` with a custom ``slice_dry_run_fn``
and never implements its own slicer call. The single N/A trigger (per the
resolved open question in issue #9):

    gate 6 reports N/A if and only if neither QIDI headless nor
    PrusaSlicer CLI is invocable — i.e. ``slicer.available_slicers()``
    returns an empty dict.

A partial binary configuration (e.g. PrusaSlicer present but QIDI absent)
is NOT N/A — the partial configuration runs (the real driver has its own
precedence and fallback) and the gate passes/fails on its verdict.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import patch

import pytest
import trimesh

from d33d import slicer
from d33d.evals.slice_gate import (
    SLICE_GATE_LABEL,
    SliceGateResult,
    run_slice_gate,
)
from d33d.slicer import SliceDryRunResult


def _box_stl(tmp_path) -> str:
    """A small, watertight, positive-volume box STL (the ticket #1 golden
    fixture shape — 20 mm cube) for gate 6 inputs."""
    path = tmp_path / "box.stl"
    trimesh.creation.box(extents=[20.0, 20.0, 20.0]).export(str(path))
    return str(path)


def _slicer_result(ok: bool, return_code: int = 0) -> SliceDryRunResult:
    return SliceDryRunResult(
        ok=ok,
        slicer="qidi" if ok else "none",
        gcode_path="/fake/plate_1.gcode" if ok else None,
        gcode_lines=1234 if ok else 0,
        return_code=return_code if ok else -50,
        error_string="" if ok else "no slicer available",
        objects=1 if ok else None,
        detail="slice ok" if ok else "no slicer binary found",
    )


# ---------------------------------------------------------------------------
# N/A trigger — single, testable condition
# ---------------------------------------------------------------------------


def test_na_when_no_slicers_available():
    """N/A iff NO slicer binary is invocable (empty available_slicers).

    The gate short-circuits on the N/A trigger BEFORE touching the file
    system, so the STL path may be nonexistent and the gate still reports
    N/A rather than a load error.
    """
    with patch.object(slicer, "available_slicers", return_value={}):
        result = run_slice_gate("/nonexistent/box.stl")
    assert result.status == "na"


def test_na_result_shape():
    with patch.object(slicer, "available_slicers", return_value={}):
        result = run_slice_gate("/any/path.stl")
    assert result.status == "na"
    assert result.detail.startswith("N/A — slicer not available headless")
    assert result.error_class is None
    assert result.slicer is None
    assert result.ok is False


def test_na_label_constant():
    assert SLICE_GATE_LABEL == "slice_dry_run"


# ---------------------------------------------------------------------------
# Non-N/A path: at least one slicer invocable → real driver runs
# ---------------------------------------------------------------------------


def test_partial_configuration_is_not_na(tmp_path):
    """PrusaSlicer present, QIDI absent → NOT N/A; the driver runs and
    its verdict decides."""
    stl = _box_stl(tmp_path)
    with (
        patch.object(
            slicer, "available_slicers", return_value={"prusa": "/bin/prusa"}
        ),
        patch.object(
            slicer, "slice_dry_run", return_value=_slicer_result(ok=True)
        ) as drv,
    ):
        result = run_slice_gate(stl)
    assert result.status == "pass"
    assert result.slicer == "qidi"  # from the stub result
    assert drv.call_args is not None


def test_pass_when_driver_succeeds(tmp_path):
    stl = _box_stl(tmp_path)
    with (
        patch.object(slicer, "available_slicers", return_value={"qidi": "/bin/q"}),
        patch.object(
            slicer, "slice_dry_run", return_value=_slicer_result(ok=True)
        ),
    ):
        result = run_slice_gate(stl)
    assert result.status == "pass"
    assert result.ok is True
    assert result.error_class is None


def test_fail_tagged_slice_when_driver_fails(tmp_path):
    stl = _box_stl(tmp_path)
    with (
        patch.object(slicer, "available_slicers", return_value={"qidi": "/bin/q"}),
        patch.object(
            slicer,
            "slice_dry_run",
            return_value=_slicer_result(ok=False, return_code=-50),
        ),
    ):
        result = run_slice_gate(stl)
    assert result.status == "fail"
    assert result.ok is False
    assert result.error_class == "slice"
    assert result.slicer == "none"


# ---------------------------------------------------------------------------
# Delegation to the #4 pipeline — validate_stl's gate 6 wiring
# ---------------------------------------------------------------------------


def test_gate6_fails_before_gate7_via_validate_stl(tmp_path):
    """The harness shells into validate_stl's gate 6 — a mesh that clears
    watertight/winding/dimension/volume but fails the slice dry run lands
    in the 'slice' class (never 'envelope' or 'ok')."""
    from d33d.print_validation import validate_stl

    stl = _box_stl(tmp_path)
    result = validate_stl(
        stl,
        stated_mm=(20.0, 20.0, 20.0),
        output_dir=str(tmp_path / "out"),
        slice_dry_run_fn=lambda m, o: _slicer_result(ok=False, return_code=-50),
    )
    assert result.ok is False
    assert result.error_class == "slice"


def test_gate6_pass_reaches_gate7_and_export(tmp_path):
    """With a passing injected slice fn, the box clears gates 6 and 7 and
    the 3MF export succeeds."""
    from d33d.print_validation import validate_stl

    stl = _box_stl(tmp_path)
    result = validate_stl(
        stl,
        stated_mm=(20.0, 20.0, 20.0),
        output_dir=str(tmp_path / "out"),
        slice_dry_run_fn=lambda m, o: _slicer_result(ok=True),
    )
    assert result.ok is True
    assert result.export_3mf is not None


def test_slice_dry_run_fn_signature_is_slicer_agnostic():
    """The injected fn takes (model_path, output_dir) — the eval harness
    never calls a slicer binary directly; the interface is slicer-agnostic
    (QIDI vs PrusaSlicer is a detail inside the #4 driver)."""
    calls: list[tuple[Any, Any]] = []

    def _fn(model_path: str, output_dir: str | None) -> SliceDryRunResult:
        calls.append((model_path, output_dir))
        return _slicer_result(ok=True)

    with patch.object(slicer, "available_slicers", return_value={"qidi": "/bin/q"}):
        result = run_slice_gate("/any/box.stl", slice_fn=_fn)
    assert result.status == "pass"
    assert calls == [("/any/box.stl", None)]
    assert len(calls) == 1
