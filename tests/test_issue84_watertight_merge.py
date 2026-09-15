"""Issue #84: the render worker used to load the harvested STL with
``trimesh.load(..., process=False)`` and read ``is_watertight`` directly.
OpenSCAD's STL export writes per-facet DUPLICATED vertices, so a valid
cube loaded unmerged reports ``watertight=False`` and was classified
``empty_model`` (bogus gate-bit-0 failure). The worker now calls
``merge_vertices()`` before reading watertight/volume/vertices; this test
drives the REAL load path (fixture bytes on disk + real ``trimesh``)
through ``render_for_design_loop``'s classify step without Docker.

No test here needs Docker: every ``subprocess.run`` is stubbed and the
"harvest helper" plants real STL bytes on disk under the tempdir.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
import trimesh

import d33d.render_worker as rw

FIXTURE = Path(__file__).parent / "fixtures" / "stl" / "box_20mm.stl"

SIX_VIEW_NAMES = [name for name, _cam in rw.VIEWS]


class _TrimeshLike:
    """Wraps a real trimesh mesh so a test can force ``volume`` to raise
    a specific exception (simulating a malformed/degenerate STL that
    makes ``mesh.volume`` raise a bare ``IndexError``), while
    ``merge_vertices`` and the rest behave as the real mesh."""

    def __init__(self, inner: Any, raise_on_volume: Exception | None = None) -> None:
        self._inner = inner
        self._raise_on_volume = raise_on_volume
        self.is_watertight = inner.is_watertight

    @property
    def vertices(self) -> Any:
        return self._inner.vertices

    def merge_vertices(self, *a: Any, **kw: Any) -> Any:
        return self._inner.merge_vertices(*a, **kw)

    @property
    def volume(self) -> float:
        if self._raise_on_volume is not None:
            raise self._raise_on_volume
        return self._inner.volume


def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
    """All docker calls succeed; the harvest helper plants the box-fixture
    STL bytes (plus a CSG and 6 view PNGs) into the
    ``--volume <dir>:/host`` dir, so the real ``stl.is_file()`` and
    ``out.glob("view_*.png")`` see a fully successful render on disk."""
    if "cp /work/model.stl /host/" in " ".join(argv):
        for i, tok in enumerate(argv):
            if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                out = Path(tok.rsplit(":", 1)[0])
                out.mkdir(parents=True, exist_ok=True)
                (out / "model.stl").write_bytes(FIXTURE.read_bytes())
                (out / "model.csg").write_bytes(b"fake-csg-bytes")
                for name in SIX_VIEW_NAMES:
                    (out / name).write_bytes(b"fake-png-bytes")
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


def test_box_fixture_openscad_style_stl_classifies_ok() -> None:
    """The golden fixture (ASCII STL, 12 facets, 36 duplicated-vertex
    rows, OpenSCAD export style) — loaded through the worker's exact
    load path (process=False → merge_vertices) — now yields
    watertight=True, vertex_count=8, volume=8000.0 and classifies
    ``ok``. Pre-fix, the same load path gave 36 verts / watertight=False
    / ``empty_model``."""
    mesh = trimesh.load(str(FIXTURE), process=False)
    mesh.merge_vertices()  # the worker's fix seam

    vertex_count = len(mesh.vertices)
    watertight = bool(mesh.is_watertight)
    volume = float(mesh.volume)

    # Merging is what makes the numbers correct (36 → 8 verts).
    assert vertex_count == 8
    assert watertight is True
    assert volume == pytest.approx(8000.0)

    result = rw.classify(
        exit_code=0,
        stl_path="model.stl",
        csg_path="model.csg",
        views=list(SIX_VIEW_NAMES),
        stderr="",
        vertex_count=vertex_count,
        watertight=watertight,
        volume=volume,
    )
    assert result == "ok"


def test_box_fixture_unmerged_reports_not_watertight() -> None:
    """Document the bug the fix removes: the SAME fixture loaded with
    process=False and NOT merged reports watertight=False / 36 verts and
    classifies empty_model. This is exactly what the pre-fix worker saw."""
    mesh = trimesh.load(str(FIXTURE), process=False)

    result = rw.classify(
        exit_code=0,
        stl_path="model.stl",
        csg_path="model.csg",
        views=list(SIX_VIEW_NAMES),
        stderr="",
        vertex_count=len(mesh.vertices),
        watertight=bool(mesh.is_watertight),
        volume=float(mesh.volume),
    )
    assert len(mesh.vertices) == 36
    assert mesh.is_watertight is False
    assert result == "empty_model"


def test_real_render_pipeline_classifies_box_fixture_ok(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """End-to-end (Docker-stubbed): a render whose harvested model.stl is
    the OpenSCAD-style box fixture classifies ``ok`` through
    ``render_for_design_loop`` — the pre-fix code path returned
    ``empty_model`` here."""
    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000084")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    result = rw.render_for_design_loop("cube(20);", {}, renders_dir=tmp_path / "renders")

    assert result.error_class == "ok"
    assert result.ok is True


def test_genuinely_empty_mesh_still_empty_model() -> None:
    """A truly empty mesh (0 vertices) still classifies ``empty_model`` —
    the fix must not weaken the check into 'any STL is ok'."""
    empty = trimesh.Trimesh()

    result = rw.classify(
        exit_code=0,
        stl_path="model.stl",
        csg_path="model.csg",
        views=list(SIX_VIEW_NAMES),
        stderr="",
        vertex_count=len(empty.vertices),
        watertight=bool(empty.is_watertight),
        volume=0.0,
    )
    assert result == "empty_model"


def test_zero_volume_degenerate_mesh_still_empty_model_and_no_raise() -> None:
    """A degenerate mesh (coplanar/duplicate facets → volume 0) still
    classifies ``empty_model`` and does NOT raise."""
    import numpy as np

    # Coplanar overlapping triangles: non-empty, zero volume.
    v = np.array(
        [[0, 0, 0], [1, 0, 0], [0, 1, 0], [1, 1, 0], [0, 0, 0], [1, 1, 0]],
        dtype=float,
    )
    f = np.array([[0, 1, 2], [1, 3, 2], [2, 3, 4], [2, 4, 5]])
    m = trimesh.Trimesh(vertices=v, faces=f, process=False)
    m.merge_vertices()

    vol = float(m.volume)  # must not raise
    assert vol == pytest.approx(0.0)

    result = rw.classify(
        exit_code=0,
        stl_path="model.stl",
        csg_path="model.csg",
        views=list(SIX_VIEW_NAMES),
        stderr="",
        vertex_count=len(m.vertices),
        watertight=bool(m.is_watertight),
        volume=vol,
    )
    assert result == "empty_model"


def test_worker_load_path_catches_indexerror_from_volume(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A malformed/degenerate STL whose ``mesh.volume`` raises a bare
    ``IndexError`` must be CAUGHT by the worker's load block and
    classified (empty_model) — pre-fix the except only covered
    ``(OSError, ValueError)`` and the IndexError escaped, crashing
    ``render_for_design_loop`` instead of classifying."""
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000085")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))
    monkeypatch.setattr(rw.subprocess, "run", _record)

    bad = _TrimeshLike(
        trimesh.load(str(FIXTURE), process=False),
        raise_on_volume=IndexError("too many indices for array"),
    )

    class _BadTrimesh:
        @staticmethod
        def load(*a: Any, **kw: Any) -> Any:
            return bad

    monkeypatch.setitem(sys.modules, "trimesh", _BadTrimesh)

    # The IndexError must NOT propagate out of render_for_design_loop —
    # it must be swallowed into the classify inputs (vertex_count=0,
    # watertight=False, volume=0.0 → empty_model).
    result = rw.render_for_design_loop("cube(20);", {}, renders_dir=tmp_path / "renders")

    assert isinstance(result, rw.RenderResult)
    assert result.error_class == "empty_model"
    assert result.ok is False
