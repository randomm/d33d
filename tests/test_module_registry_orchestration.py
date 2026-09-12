"""Unit tests for ``build_registry_glb``'s per-call-site classification
and partial-failure handling, with ``subprocess.run`` mocked — no Docker
required. The real Docker-driven end-to-end path is covered by the slow
layer (``tests/slow/test_module_registry_docker.py``).

Exercises every branch ``_classify_isolated_render`` and the populate/
harvest helpers can take: populate failure, non-zero openscad exit,
timeout, oom, empty STL, and the "one bad module never discards the
whole registry" partial-success guarantee.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import trimesh

from d33d.module_registry import build_registry_glb

TWO_MODULE_SCAD = """
module base() { cube([20,20,20]); }
module cap() { sphere(r=8); }
base();
cap();
"""


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _box_stl_bytes() -> bytes:
    mesh = trimesh.creation.box(extents=(1, 1, 1))
    return mesh.export(file_type="stl")


def test_all_call_sites_succeed_assembles_full_registry() -> None:
    stl_bytes = _box_stl_bytes()

    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if any("cat > /work/" in a for a in argv):
            return _completed(0)
        if any("cat /work/" in a for a in argv):
            return _completed(0, stdout=stl_bytes)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert result.failures == ()
    assert set(result.registry_names) == {"base", "cap"}
    assert result.glb_bytes is not None


def test_populate_failure_is_recorded_as_container_error_and_other_sites_still_run() -> None:
    stl_bytes = _box_stl_bytes()
    populate_calls = {"count": 0}

    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if any("cat > /work/" in a for a in argv):
            populate_calls["count"] += 1
            if populate_calls["count"] == 1:
                return _completed(1, stderr=b"disk full")
            return _completed(0)
        if any("cat /work/" in a for a in argv):
            return _completed(0, stdout=stl_bytes)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "container_error"
    assert result.failures[0].site.name == "base"
    # The SECOND call-site still succeeded — partial registry preserved.
    assert result.registry_names == ("cap",)
    assert result.glb_bytes is not None


def test_nonzero_openscad_exit_classifies_container_error() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(1, stderr=b"ERROR: parse error")
        if any("cat > /work/" in a for a in argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 2
    assert all(f.error_class == "container_error" for f in result.failures)
    assert result.glb_bytes is None


def test_openscad_timeout_classifies_as_timeout() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(124)
        if any("cat > /work/" in a for a in argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "timeout" for f in result.failures)
    assert result.glb_bytes is None


def test_openscad_oom_classifies_as_oom() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(137)
        if any("cat > /work/" in a for a in argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "oom" for f in result.failures)
    assert result.glb_bytes is None


def test_empty_stl_harvest_classifies_empty_model() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if any("cat > /work/" in a for a in argv):
            return _completed(0)
        if any("cat /work/" in a for a in argv):
            return _completed(0, stdout=b"")  # empty harvest
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "empty_model" for f in result.failures)
    assert result.glb_bytes is None


def test_source_with_no_call_sites_never_invokes_subprocess() -> None:
    with patch("subprocess.run") as mock_run:
        result = build_registry_glb("cube([1,1,1]);\n")
    mock_run.assert_not_called()
    assert result.glb_bytes is None
    assert result.registry_names == ()
    assert result.failures == ()
