"""Issue #87: the design-loop bbox gate read ``RenderResult.stl``, which
pointed into the render worker's ``tempfile.TemporaryDirectory`` — torn
down before ``bbox_fn`` ran — so the gate could never pass in production.

The fix (option (a)): ``render_for_design_loop`` re-points ``stl`` (and
``views``) at the durable copies under ``render_artifact_dir`` (issue #72's
per-render directory, where the STL is ALWAYS named ``model.stl``) when
persistence succeeded; ``csg`` stays on the temp path (it is never
persisted). ``bbox_from_render`` is otherwise unchanged — it now simply
reads a live path.

Fast layer: real ``box_20mm.stl`` fixture bytes on disk + stubbed
``subprocess.run`` (the issue #84 pattern); no Docker.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest
import trimesh

import d33d.design_loop_events as dle
import d33d.render_worker as rw
from d33d.design_loop import BboxInfo
from d33d.render_worker import RenderResult

FIXTURE = Path(__file__).parent / "fixtures" / "stl" / "box_20mm.stl"


def _make_render(
    stl: str | None,
    render_artifact_dir: str | None = None,
    views: tuple[str, ...] = (),
) -> RenderResult:
    """A minimal ok-ish RenderResult for driving bbox_from_render."""
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl=stl,
        csg=None,
        views=views,
        render_artifact_dir=render_artifact_dir,
    )


def test_bbox_from_render_reads_durable_path_after_stl_teardown(tmp_path: Path) -> None:
    """THE decisive seam (issue #87): a RenderResult whose ``.stl`` path
    no longer exists (the tempdir is torn down) but whose
    ``render_artifact_dir`` holds a real STL — after the fix, ``.stl``
    points at the durable ``<dir>/model.stl`` and the bbox gate reads a
    live 20x20x20 box instead of None."""
    artifact_dir = tmp_path / "renders" / "f3aa2fea"  # uuid8-keyed dir name
    artifact_dir.mkdir(parents=True)
    (artifact_dir / "model.stl").write_bytes(FIXTURE.read_bytes())

    # Simulate the post-fix RenderResult: stl points at the durable copy,
    # and the tempdir that would have held the harvested copy is gone.
    result = _make_render(str(artifact_dir / "model.stl"), str(artifact_dir))

    bbox = dle.bbox_from_render(result)

    assert isinstance(bbox, BboxInfo)
    assert bbox.x == pytest.approx(20.0)
    assert bbox.y == pytest.approx(20.0)
    assert bbox.z == pytest.approx(20.0)
    assert bbox.volume == pytest.approx(8000.0)


def test_bbox_from_render_torn_down_stl_no_artifact_dir_returns_none(
    tmp_path: Path,
) -> None:
    """render_artifact_dir is None AND the .stl path is gone (persistence
    disabled/failed, tempdir torn down) → bbox is None, no raise — the
    gate fails exactly as it does today."""
    dead_path = tmp_path / "torn-down" / "model.stl"  # dir never created

    result = _make_render(str(dead_path), None)

    assert dle.bbox_from_render(result) is None


def test_bbox_from_render_artifact_dir_without_stl_returns_none(tmp_path: Path) -> None:
    """render_artifact_dir set but empty (no model.stl — e.g. a partial
    copy failure that left the dir) → bbox is None, no raise."""
    empty_dir = tmp_path / "renders" / "00000000"
    empty_dir.mkdir(parents=True)
    (empty_dir / "view_front.png").write_bytes(b"not-an-stl")

    result = _make_render(str(empty_dir / "model.stl"), str(empty_dir))

    assert dle.bbox_from_render(result) is None


# ---------------------------------------------------------------------------
# The worker seam: RenderResult.stl must survive the tempdir teardown.
# Same fast-layer pattern as test_issue84_watertight_merge.py: plant real
# fixture bytes via the stubbed harvest helper, no Docker.
# ---------------------------------------------------------------------------


def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
    """All docker calls succeed; the harvest helper plants the box-fixture
    STL bytes (plus a CSG and 6 view PNGs) into the ``--volume <dir>:/host``
    dir so the real on-disk checks see a fully successful render."""
    if "cp /work/model.stl /host/" in " ".join(argv):
        for i, tok in enumerate(argv):
            if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                out = Path(tok.rsplit(":", 1)[0])
                out.mkdir(parents=True, exist_ok=True)
                (out / "model.stl").write_bytes(FIXTURE.read_bytes())
                (out / "model.csg").write_bytes(b"fake-csg-bytes")
                for name in [n for n, _cam in rw.VIEWS]:
                    (out / name).write_bytes(b"fake-png-bytes")
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


def test_render_for_design_loop_stl_survives_tempdir_teardown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REGRESSION GUARD (the test that would have caught issue #87): after
    ``render_for_design_loop`` returns — OUTSIDE the TemporaryDirectory
    with-block — the ``.stl`` path on the returned RenderResult must still
    exist on disk (the durable copy under ``render_artifact_dir``), and
    must be the real 20x20x20 box fixture the worker persisted."""
    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000087")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    # The run happens (and its tempdir is torn down) inside the call; all
    # the assertions below execute after it returns.
    result = rw.render_for_design_loop(
        "cube(20);", {}, renders_dir=tmp_path / "renders"
    )

    assert result.error_class == "ok"
    assert result.stl is not None
    assert Path(result.stl).is_file(), (
        f"RenderResult.stl {result.stl!r} is dead after render_for_design_loop "
        "returned — the tempdir was torn down; the fix must re-point it at the "
        "durable artifact path"
    )
    # It is the durable copy (fixed name "model.stl"), not the temp path.
    assert Path(result.stl).name == "model.stl"
    assert result.render_artifact_dir is not None
    assert Path(result.stl).parent == Path(result.render_artifact_dir)
    # Views are re-pointed to the durable copies too (persistence copies
    # all six); the csg is NOT persisted, so it stays None-or-temp-shaped
    # (the harvested temp csg is gone — the worker must not claim it lives).
    assert len(result.views) == 6
    for v in result.views:
        assert Path(v).is_file(), f"view {v!r} not durable"
        assert Path(v).parent == Path(result.render_artifact_dir)
    # The csg was not persisted — the tempdir csg is dead (documented in
    # the worker: csg is never copied by _persist_render_artifacts).
    assert result.csg is None or not Path(result.csg).is_file()
    # And the durable STL is loadable and correct at the bbox_fn moment.
    mesh = trimesh.load(result.stl, process=False)
    extents = mesh.bounds[1] - mesh.bounds[0]
    assert tuple(extents) == pytest.approx((20.0, 20.0, 20.0))


def test_render_for_design_loop_no_persistence_keeps_temp_path_shape(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Fallback: when persistence yields no durable dir (e.g. the persist
    base is None), an ok render keeps ``stl`` on the harvested (temp)
    path exactly as today — never None, never the absent durable path."""
    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000088")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))
    # Make _render_persist_base return None (no base → no persistence).
    monkeypatch.setattr(rw, "_render_persist_base", lambda: None)
    # renders_dir must be None so the env/default base is consulted.
    monkeypatch.delenv("D33D_RENDER_PERSIST_DIR", raising=False)

    result = rw.render_for_design_loop("cube(20);", {})

    assert result.error_class == "ok"
    assert result.render_artifact_dir is None
    assert result.stl is not None
    # The temp path — dead after return (documented degrade path), but NOT
    # None and not a durable path: it is the harvested in-tempdir path.
    assert not Path(result.stl).is_file()
    assert Path(result.stl).name == "model.stl"
