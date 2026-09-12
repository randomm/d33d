"""Slow-layer test: the full named-module registry orchestrator against
the REAL pinned OpenSCAD image (``docker.io/openscad/openscad:trixie``).

Drives ``build_registry_glb`` end to end: parses a 3-module ``.scad``
fixture, runs three ``!``-masked ``openscad`` invocations via the SAME
hardened container flags as the render worker's single-render contract,
and asserts the assembled GLB round-trips through trimesh with exactly
the three module names as registry keys — the same shape the empirical
spike validated by hand before this ticket started.

Skipped (not failed) when Docker is unreachable, matching the slow-layer
convention in ``tests/slow/test_slice_dryrun.py`` — a machine without
Docker has no signal to assert.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest
import trimesh

from d33d.module_registry import build_registry_glb

pytestmark = pytest.mark.slow

THREE_MODULE_SCAD = """
module base() { cube([20,20,20]); }
module post(h=30) { translate([0,0,20]) cylinder(h=h, r=5); }
module cap() { translate([0,0,50]) sphere(r=8); }

base();
post(h=30);
cap();
"""


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        proc = subprocess.run(
            ["docker", "info"], capture_output=True, timeout=10, check=False
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _skip_if_no_docker():
    if not _docker_available():
        pytest.skip("Docker not reachable on this box")


def test_three_module_registry_builds_named_glb_via_real_openscad() -> None:
    _skip_if_no_docker()
    result = build_registry_glb(THREE_MODULE_SCAD)

    assert result.failures == ()
    assert set(result.registry_names) == {"base", "post", "cap"}
    assert result.glb_bytes is not None

    import io

    reloaded = trimesh.load(io.BytesIO(result.glb_bytes), file_type="glb")
    assert isinstance(reloaded, trimesh.Scene)
    assert set(reloaded.geometry.keys()) == {"base", "post", "cap"}
    # Each isolated module's mesh is non-degenerate.
    for name, geom in reloaded.geometry.items():
        assert geom.vertices.shape[0] > 0, f"{name} isolated to an empty mesh"


def test_no_call_sites_returns_none_glb_without_invoking_docker() -> None:
    """A ``.scad`` with zero top-level module call-sites (bare primitives
    only) must short-circuit before spending any Docker invocation."""
    result = build_registry_glb("cube([1,1,1]);\n")
    assert result.glb_bytes is None
    assert result.registry_names == ()
    assert result.failures == ()
