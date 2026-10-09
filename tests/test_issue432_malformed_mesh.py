"""Issue #432: fingerprint load and through-hole span never raise on malformed meshes."""

from __future__ import annotations

import struct

import numpy as np
import pytest
import trimesh as _tm

from d33d.through_hole_check import THROUGH_HOLE_INSTRUCTION, _instruction_with_span
from d33d.unchanged_mesh_check import fingerprint_stl


@pytest.mark.parametrize("exc", [struct.error("bad header"), IndexError("bad index")])
def test_fingerprint_stl_malformed_load_returns_none(tmp_path, monkeypatch, exc):
    stl = tmp_path / "bad.stl"
    stl.write_bytes(b"\x00" * 84)

    def _raise(*args, **kwargs):
        raise exc

    monkeypatch.setattr(_tm, "load", _raise)
    assert fingerprint_stl(str(stl)) is None


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf")])
def test_span_non_finite_bounds_fall_back_to_bare_instruction(tmp_path, monkeypatch, bad):
    stl = tmp_path / "part.stl"
    stl.write_bytes(b"\x00" * 84)

    class _Mesh:
        bounds = np.array([[0.0, 0.0, 0.0], [10.0, 10.0, bad]])

    monkeypatch.setattr(_tm, "load", lambda *a, **k: _Mesh())
    assert _instruction_with_span(THROUGH_HOLE_INSTRUCTION, str(stl)) == THROUGH_HOLE_INSTRUCTION
