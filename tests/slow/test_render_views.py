"""Slow-layer test: byte-stability and framing under real OpenSCAD renders.

Two acceptance gates from issue #111 that require Docker (the ``slow``
marker):

1. **Byte-stability** — rendering the same source twice must produce
   byte-identical view PNGs. The camera-distance fit is a pure function
   of the bounding box (no timestamps, no randomised seeds, no
   wall-clock), so two renders of the same ``.scad`` must yield
   identical PNG bytes.

2. **Framing** — a 20 mm, 60 mm and 300 mm part must ALL be fully
   framed with the same margin rule: the model's pixel bounding box
   must be strictly inside the 800×800 frame (the corners must be the
   background colour, not the model colour).

Both tests skip (not fail) when Docker is unreachable, matching the
slow-layer convention in ``tests/slow/test_module_registry_docker.py``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

import d33d.render_worker as rw

pytestmark = pytest.mark.slow

# The six view filenames the entrypoint produces.
VIEW_FILES = [name for name, _ in rw.VIEWS]


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


def _skip_if_no_docker() -> None:
    if not _docker_available():
        pytest.skip("Docker not reachable on this box")


def _host_tmp_base() -> Path:
    """Directory that host-side temp dirs for this test live under.

    Must NOT be ``/tmp`` or ``pytest``'s ``tmp_path`` (which is under
    ``/tmp`` on macOS) — Docker Desktop on macOS cannot see ``/tmp``,
    so a helper container mounting a ``/tmp`` path sees an empty
    directory. Use ``~/d33d/render-tmp`` (the ``D33D_RENDER_TMP``
    convention) instead.
    """
    path = Path(
        os.environ.get(
            "D33D_RENDER_TMP", str(Path.home() / "d33d" / "render-tmp")
        )
    )
    path.mkdir(parents=True, exist_ok=True)
    return path


def _render(scad_source: str, workdir: Path) -> None:
    """Run one render via the render-worker image and harvest the
    artifacts into ``workdir/out``.

    Uses the same hardened container flags as the production
    ``render_for_design_loop`` (``--network none``, ``--cap-drop ALL``,
    ``--read-only``, tmpfs ``/tmp``, ``--security-opt no-new-privileges``)
    plus a named volume for the work directory. The seed and harvest
    helper containers mount host paths under ``D33D_RENDER_TMP`` (not
    ``/tmp``) so Docker Desktop on macOS can see them.
    """
    base = _host_tmp_base()
    volume = f"d33d-slow-test-{uuid.uuid4().hex[:8]}"
    host_seed = base / f"seed-{uuid.uuid4().hex[:8]}"
    host_out = base / f"out-{uuid.uuid4().hex[:8]}"
    try:
        subprocess.run(
            ["docker", "volume", "create", volume],
            capture_output=True,
            check=False,
        )
        # Seed the volume with the .scad source.
        host_seed.mkdir()
        (host_seed / "model.scad").write_text(scad_source, encoding="utf-8")
        seed_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{volume}:/work",
            "--volume",
            f"{host_seed}:/host:ro",
            "busybox:latest",
            "sh",
            "-c",
            "cp /host/model.scad /work/model.scad && chown 1000:1000 /work",
        ]
        seed_proc = subprocess.run(seed_argv, capture_output=True, check=False)
        assert seed_proc.returncode == 0, (
            f"seed failed: {seed_proc.stderr.decode()}"
        )
        # Run the render worker.
        argv = rw.build_docker_argv(
            image=rw.RENDER_WORKER_IMAGE,
            name=f"render-{volume[-8:]}",
            workdir_volume=volume,
            params=rw.RenderParams(),
        )
        proc = rw.run_container(argv, timeout_s=120)
        assert proc.returncode == 0, (
            f"render failed (exit {proc.returncode}): "
            f"{rw.truncate_stderr(proc.stderr)}"
        )
        # Harvest the artifacts.
        host_out.mkdir()
        harvest_argv = [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--volume",
            f"{volume}:/work",
            "--volume",
            f"{host_out}:/host",
            "busybox:latest",
            "sh",
            "-c",
            (
                "cp /work/model.stl /host/ 2>/dev/null; "
                "cp /work/model.csg /host/ 2>/dev/null; "
                "cp /work/view_*.png /host/ 2>/dev/null; true"
            ),
        ]
        subprocess.run(harvest_argv, capture_output=True, check=False)
        # Copy harvested files to the test's workdir.
        out_dir = workdir / "out"
        out_dir.mkdir(parents=True, exist_ok=True)
        for f in host_out.iterdir():
            shutil.copy2(f, out_dir)
    finally:
        subprocess.run(
            ["docker", "volume", "rm", "-f", volume],
            capture_output=True,
            check=False,
        )
        shutil.rmtree(host_seed, ignore_errors=True)
        shutil.rmtree(host_out, ignore_errors=True)


def test_byte_stability_two_renders_produce_identical_view_bytes(
    tmp_path: Path,
) -> None:
    """Rendering the same source twice must produce byte-identical view
    PNGs. The camera-distance fit is a pure function of the bounding
    box — no timestamps, no randomised seeds, no wall-clock, no
    dict-iteration-order dependence — so two renders of the same
    ``.scad`` must yield identical PNG bytes (issue #111 acceptance
    gate)."""
    _skip_if_no_docker()
    scad = "cube([30,30,30], center=true);\n"
    workdir1 = tmp_path / "run1"
    workdir2 = tmp_path / "run2"
    _render(scad, workdir1)
    _render(scad, workdir2)
    for view in VIEW_FILES:
        png1 = workdir1 / "out" / view
        png2 = workdir2 / "out" / view
        assert png1.is_file(), f"run 1 missing {view}"
        assert png2.is_file(), f"run 2 missing {view}"
        bytes1 = png1.read_bytes()
        bytes2 = png2.read_bytes()
        assert bytes1 == bytes2, (
            f"{view}: two renders of the same source produced different "
            f"PNG bytes ({len(bytes1)} vs {len(bytes2)} bytes)"
        )


def test_views_frame_the_whole_model_with_margin(
    tmp_path: Path,
) -> None:
    """A part at 20 mm, 60 mm and 300 mm must ALL be fully framed with
    the same margin rule: the model's pixel bounding box must be
    strictly inside the 800×800 frame (the four corners must be the
    background colour, not the model colour).

    The background colour of the pinned image's "Tomorrow Night"
    scheme is (29, 31, 33). If the model overflows the frame, the
    corners will be the model colour instead — the exact bug issue #111
    fixes.
    """
    _skip_if_no_docker()
    BG = (29, 31, 33)
    for extent in (20, 60, 300):
        scad = f"cube([{extent},{extent},{extent}], center=true);\n"
        workdir = tmp_path / f"framing_{extent}"
        _render(scad, workdir)
        for view in VIEW_FILES:
            png = workdir / "out" / view
            assert png.is_file(), f"{extent} mm: missing {view}"
            # Decode the PNG and check that the four corners are the
            # background colour (not the model colour). If the model
            # overflows, at least one corner will be model colour.
            _check_corners_are_background(png, BG, extent, view)


def _check_corners_are_background(
    png_path: Path, bg: tuple[int, int, int], extent: int, view: str
) -> None:
    """Decode a PNG and assert the four corners are the background
    colour. Uses a minimal PNG decoder (struct + zlib) to avoid a
    Pillow dependency in the slow-layer test."""
    import struct
    import zlib

    data = png_path.read_bytes()
    pos = 8
    idat = b""
    w = h = 0
    bitdepth = 0
    color = 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        if ctype == b"IHDR":
            w, h, bitdepth, color = struct.unpack(">IIBB", chunk[:10])
        elif ctype == b"IDAT":
            idat += chunk
        pos += 12 + ln
    raw = zlib.decompress(idat)
    bpp = 4 if color == 6 else 3
    stride = w * bpp
    out = bytearray(h * stride)
    ri = 0
    for y in range(h):
        f = raw[ri]
        ri += 1
        line = raw[ri : ri + stride]
        ri += stride
        prev = out[(y - 1) * stride : y * stride] if y > 0 else bytes(stride)
        cur = bytearray(stride)
        for i in range(stride):
            x = line[i]
            a = cur[i - bpp] if i >= bpp else 0
            b = prev[i]
            c = prev[i - bpp] if i >= bpp else 0
            if f == 0:
                cur[i] = x
            elif f == 1:
                cur[i] = (x + a) & 255
            elif f == 2:
                cur[i] = (x + b) & 255
            elif f == 3:
                cur[i] = (x + (a + b) // 2) & 255
            else:
                p = a + b - c
                pa = abs(p - a)
                pb = abs(p - b)
                pc = abs(p - c)
                pr = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (x + pr) & 255
        out[y * stride : (y + 1) * stride] = cur

    def _px(x: int, y: int) -> tuple[int, int, int]:
        i = (y * w + x) * bpp
        return (out[i], out[i + 1], out[i + 2])

    corners = [
        _px(0, 0),
        _px(w - 1, 0),
        _px(0, h - 1),
        _px(w - 1, h - 1),
    ]
    for ci, corner in enumerate(corners):
        assert corner == bg, (
            f"{extent} mm {view}: corner {ci} is {corner}, expected "
            f"background {bg} — the model overflows the 800×800 frame"
        )
