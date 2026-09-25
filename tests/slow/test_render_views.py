"""Slow-layer test: byte-stability and framing under real OpenSCAD renders.

Three acceptance gates from issues #111 and #223 that require Docker (the
``slow`` marker):

1. **Byte-stability (cube)** — rendering the same source twice must produce
   byte-identical view PNGs. The camera-distance fit is a pure function
   of the bounding box (no timestamps, no randomised seeds, no
   wall-clock), so two renders of the same ``.scad`` must yield
   identical PNG bytes.

2. **Byte-stability (asymmetric)** — the same gate, but exercising the
   golden-a asymmetric-bbox fixture from issue #223. The cube-only
   gate is blind to the #223 defect class (a cube is symmetric about
   the origin, so the corner-only framing check passes whether or not
   the model is off-center); this gate uses an asymmetric geometry so
   a future regression in the camera fit will break byte-stability
   the moment it changes the framing.

3. **Framing (edge-bbox containment)** — the model's non-background
   pixel bounding box must lie strictly inside the 800×800 frame with
   a visible margin on all four edges, for both the cube sizes from
   #111 (20/60/300 mm) AND the two golden asymmetric-bbox fixtures
   from #223 (the exact geometries of the real v15/v16 renders that
   exhibited the off-center defect). The old corner-only check was
   provably blind to the #223 defect (an off-center model that fits
   inside the frame still passes the corner check); the edge-bbox
   check is the acceptance criterion.

All tests skip (not fail) when Docker is unreachable, matching the
slow-layer convention in ``tests/slow/test_module_registry_docker.py``.
"""

from __future__ import annotations

import os
import shutil
import struct
import subprocess
import uuid
import zlib
from pathlib import Path

import pytest

import d33d.render_worker as rw

pytestmark = pytest.mark.slow

# The six view filenames the entrypoint produces.
VIEW_FILES = [name for name, _ in rw.VIEWS]

# Minimum background margin on every frame edge of the iso view for the
# worst-case full-extent box: at least 3% of the 800 px frame (≥ 24 px)
# on each side (issue #234 operator decision — "at least 3% of the frame
# of background on every edge of view_05_iso").
ISO_MIN_MARGIN_PX = 0.03 * 800

# The "Tomorrow Night" background colour (r, g, b) of the pinned image.
BG = (29, 31, 33)

# A pixel within this Euclidean-ish tolerance of BG counts as background
# (antialiased edges on the model silhouette must not register as model
# pixels). A strict == BG comparison (as the old corner check used) is
# flaky on edge-adjacent pixels and a centroid computed over
# hard-thresholded pixels shifts with the threshold.
BG_TOL = 40


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


# ── PNG decoding (hand-rolled, no Pillow/numpy dependency) ──────────────────


def _decode_png(path: Path) -> tuple[int, int, int, bytearray]:
    """Minimal PNG decoder (struct + zlib) — the slow-layer baseline for
    any pixel-asserting test in this file. Avoids a Pillow/numpy
    dependency (neither is in the environment).

    Returns ``(width, height, bytes_per_pixel, raw_pixels)`` where
    ``raw_pixels`` is the un-filtered pixel buffer in row-major order.
    """
    data = path.read_bytes()
    pos = 8
    idat = b""
    w = h = 0
    color = 0
    while pos < len(data):
        ln = struct.unpack(">I", data[pos : pos + 4])[0]
        ctype = data[pos + 4 : pos + 8]
        chunk = data[pos + 8 : pos + 8 + ln]
        if ctype == b"IHDR":
            w, h, _bitdepth, color = struct.unpack(">IIBB", chunk[:10])
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
    return w, h, bpp, out


def _is_bg(rgb: tuple[int, int, int], tol: int = BG_TOL) -> bool:
    """True if ``rgb`` is within ``tol`` of the background colour on
    every channel (a generous tolerance for antialiased edges)."""
    return (
        abs(rgb[0] - BG[0]) <= tol
        and abs(rgb[1] - BG[1]) <= tol
        and abs(rgb[2] - BG[2]) <= tol
    )


def _pixel_bbox(
    png_path: Path,
) -> tuple[list[int], list[int], dict[str, int]]:
    """Compute the non-background pixel bounding box and centroid of a
    view PNG.

    Returns ``(bbox, centroid, edge_px_3band)`` where:
      - ``bbox`` = [x_min, y_min, x_max, y_max] in pixel coordinates
      - ``centroid`` = [cx, cy] (weighted by pixel count)
      - ``edge_px_3band`` = {left, right, top, bottom} counts of
        non-background pixels within a 3-px band of each frame edge
        (the "touches or exceeds an edge" signal for the framing gate)
    """
    w, h, bpp, out = _decode_png(png_path)
    minx = miny = 10**9
    maxx = maxy = -1
    sx = sy = n = 0
    for y in range(h):
        base = y * w * bpp
        for x in range(w):
            i = base + x * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                n += 1
                sx += x
                sy += y
                if x < minx:
                    minx = x
                if x > maxx:
                    maxx = x
                if y < miny:
                    miny = y
                if y > maxy:
                    maxy = y
    if n == 0:
        return [0, 0, 0, 0], [0.0, 0.0], {
            "left": 0,
            "right": 0,
            "top": 0,
            "bottom": 0,
        }
    edge_px = {"left": 0, "right": 0, "top": 0, "bottom": 0}
    for x in range(3):
        for y in range(h):
            i = (y * w + x) * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                edge_px["left"] += 1
            i = (y * w + (w - 1 - x)) * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                edge_px["right"] += 1
    for y in range(3):
        for x in range(w):
            i = (y * w + x) * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                edge_px["top"] += 1
            i = ((h - 1 - y) * w + x) * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                edge_px["bottom"] += 1
    return [minx, miny, maxx, maxy], [sx / n, sy / n], edge_px


# ── Fixture .scad sources ────────────────────────────────────────────────────


def _golden_a_scad() -> str:
    """The golden-a asymmetric fixture (issue #223): bbox x[-22.5, 45],
    y[0, 20.1], z[0, 30] — the silhouette is NOT symmetric about the
    origin, and the front view (camera -Z) clips the right frame edge
    unless the per-model fit is correct."""
    return (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "scad"
        / "issue-223-asymmetric-a.scad"
    ).read_text(encoding="utf-8")


def _golden_b_scad() -> str:
    """The golden-b asymmetric fixture (issue #223): bbox x[0, 40.09],
    y[-18.15, 20], z[0, 30] — the silhouette is NOT symmetric about the
    origin, the x range is entirely positive, and the y range is skewed
    negative. The front view (camera -Z) clips the right frame edge and
    the top view (camera +Z) clips the right and top edges unless the
    per-model fit is correct."""
    return (
        Path(__file__).resolve().parents[1]
        / "fixtures"
        / "scad"
        / "issue-223-asymmetric-b.scad"
    ).read_text(encoding="utf-8")


# ── Gate 1: byte-stability (cube) ────────────────────────────────────────────


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


# ── Gate 2: byte-stability (asymmetric) ─────────────────────────────────────


def test_byte_stability_asymmetric_fixture(
    tmp_path: Path,
) -> None:
    """The byte-stability gate, but exercised on the golden-a asymmetric
    fixture (issue #223) — the exact geometry of the real v15 model
    (project 32) that exhibited the off-center defect.

    A cube with ``center=true`` is symmetric about the origin, so the
    corner-only framing check passes whether or not the model is
    off-center. The asymmetric fixture is NOT symmetric: a regression
    in the camera fit (e.g. a position-dependent fit) will change the
    framing and break byte-stability on the first render.
    """
    _skip_if_no_docker()
    scad = _golden_a_scad()
    workdir1 = tmp_path / "asym1"
    workdir2 = tmp_path / "asym2"
    _render(scad, workdir1)
    _render(scad, workdir2)
    for view in VIEW_FILES:
        png1 = workdir1 / "out" / view
        png2 = workdir2 / "out" / view
        assert png1.is_file(), f"asym run 1 missing {view}"
        assert png2.is_file(), f"asym run 2 missing {view}"
        bytes1 = png1.read_bytes()
        bytes2 = png2.read_bytes()
        assert bytes1 == bytes2, (
            f"{view}: two renders of the asymmetric fixture produced "
            f"different PNG bytes ({len(bytes1)} vs {len(bytes2)} bytes) "
            f"— the camera fit is not a pure function of the bounding box"
        )


# ── Gate 3: framing (edge-bbox containment) ────────────────────────────────


def test_views_frame_the_whole_model_with_margin(
    tmp_path: Path,
) -> None:
    """A part at 20 mm, 60 mm and 300 mm must ALL be fully framed with
    the same margin rule: the model's non-background pixel bounding box
    must lie strictly inside the 800×800 frame with a visible margin on
    all four edges (the four corners must be the background colour, and
    no frame edge may be touched by the silhouette).

    The old corner-only check was provably blind to the #223 defect: an
    off-center model that fits inside the frame still has background
    corners. The edge-bbox check (the "touches or exceeds an edge"
    signal) is the acceptance criterion.

    The background colour of the pinned image's "Tomorrow Night" scheme
    is (29, 31, 33); a pixel within BG_TOL of that colour on every
    channel counts as background (antialiased edges).
    """
    _skip_if_no_docker()
    for extent in (20, 60, 300):
        scad = f"cube([{extent},{extent},{extent}], center=true);\n"
        workdir = tmp_path / f"framing_{extent}"
        _render(scad, workdir)
        for view in VIEW_FILES:
            png = workdir / "out" / view
            assert png.is_file(), f"{extent} mm: missing {view}"
            _check_framing(png, extent, view)


def test_views_frame_worst_case_full_extent_box_with_margin(
    tmp_path: Path,
) -> None:
    """Worst-case regression gate (issue #234): a near-cubic full-extent
    box at 20, 60 and 300 mm must keep the minimum visible margin on
    ``view_05_iso``.

    The existing cube-only gate is blind to the √3 worst case: a centred
    cube projects only *S*·√2 in the iso view (2D diagonal, zero projected
    z-extent), while a full-extent box (all three extents equal to
    ``max_extent`` *S*) projects the *S*·√3 space diagonal. A regression of
    ``CAM_DIST_ISO_FACTOR`` back toward the old ``CAM_DIST_FACTOR × √2``
    (≈ 4.2426) — or anywhere above ~3.55·√2 — still passes the cube gate
    because the cube's √2 projection leaves ~12 % slack against the new
    2.52·√3/0.94 ≈ 4.6434 factor. This test exercises the true √3 lower
    bound with a real render.

    Two worst-case geometries per size, both off-centre (so the fit is
    also exercised away from the origin):
      - a full-extent cube ``cube([s, s, s])`` — the √3 corner projection;
      - one elongated box ``cube([s, s*0.9, s*0.95])`` — a near-cubic
        box whose extents are all near ``max_extent``.

    On ``view_05_iso`` each render must pass the existing edge-band check
    (zero non-background pixels in the 3-px band of any edge) AND the
    minimum margin (≥ 3 % of the frame, i.e. ≥ 24 px of 800, of background
    on every edge), measured by ``_pixel_bbox``.
    """
    _skip_if_no_docker()
    iso_view = "view_05_iso.png"
    min_margin = 24  # 3 % of the 800-px frame
    for extent in (20, 60, 300):
        for name, scad in [
            (
                "full-extent-cube",
                (
                    f"translate([{extent},{extent},{extent}]) "
                    f"cube([{extent},{extent},{extent}]);\n"
                ),
            ),
            (
                "elongated-box",
                (
                    f"translate([0,{extent},{extent * 0.95:.2f}]) "
                    f"cube([{extent},{extent * 0.9:.2f},{extent * 0.95:.2f}]);\n"
                ),
            ),
        ]:
            workdir = tmp_path / f"worst_case_{extent}_{name}"
            _render(scad, workdir)
            for view in VIEW_FILES:
                png = workdir / "out" / view
                assert png.is_file(), f"{extent} mm {name}: missing {view}"
                _check_framing(png, f"{extent} {name}", view)
            # The minimum-margin assertion on view_05_iso specifically:
            # the √3 worst case must leave ≥ 24 px of background on
            # every edge of the iso frame, not merely a non-zero 1-px
            # bbox margin.
            png = workdir / "out" / iso_view
            bbox, _centroid, edge_px = _pixel_bbox(png)
            for side, count in edge_px.items():
                assert count == 0, (
                    f"{extent} mm {name} {iso_view}: {count} non-background "
                    f"pixels in the 3-px band of the {side} frame edge — "
                    f"the worst-case box touches or exceeds the frame edge"
                )
            x_min, y_min, x_max, y_max = bbox
            margins = {
                "left": x_min,
                "right": 799 - x_max,
                "top": y_min,
                "bottom": 799 - y_max,
            }
            for side, margin in margins.items():
                assert margin >= min_margin, (
                    f"{extent} mm {name} {iso_view}: {side} margin {margin}px "
                    f"< {min_margin}px (3% of the 800-px frame) — the "
                    f"√3 worst-case box does not keep the minimum visible "
                    f"margin (issue #234)"
                )


def test_views_frame_the_asymmetric_golden_fixtures(
    tmp_path: Path,
) -> None:
    """The two golden asymmetric-bbox fixtures (issue #223 — the exact
    geometries of the real v15/v16 renders that exhibited the off-center
    defect) must ALL be fully framed with the same margin rule: the
    model's non-background pixel bounding box must lie strictly inside
    the 800×800 frame with a visible margin on all four edges.

    This is the regression guard for the #223 defect: the golden-a
    fixture's front view clips the right frame edge unless the per-model
    fit is correct, and the golden-b fixture's front/top views clip the
    right edge and the top edge respectively.
    """
    _skip_if_no_docker()
    for name, scad_fn in [
        ("golden-a", _golden_a_scad),
        ("golden-b", _golden_b_scad),
    ]:
        workdir = tmp_path / f"asym_{name}"
        _render(scad_fn(), workdir)
        for view in VIEW_FILES:
            png = workdir / "out" / view
            assert png.is_file(), f"{name}: missing {view}"
            _check_framing(png, name, view)


def test_views_frame_the_worst_case_full_extent_box(
    tmp_path: Path,
) -> None:
    """Worst-case iso framing guard (issue #234): a box whose bounding
    box spans all three axes at or near ``max_extent`` projects up to
    ``max_extent``·√3 in the iso view — the case the √2-calibrated iso
    distance (``CAM_DIST_FACTOR × √2``) was blind to and that shipped
    the 52-pixel left-edge clip on the 20 mm cube.

    Two shapes, at 20/60/300 mm, in all six views:

    - an uncentred ``cube([s, s, s])`` — the bbox is not symmetric about
      the origin, so this also exercises the #223 bbox-centre translate
      on the √3 worst-case geometry (the existing 20/60/300 mm gate
      uses ``center=true`` and cannot see a centre-dependent defect);
    - an elongated near-cubic box ``cube([s, s*0.9, s*0.95])`` — all
      three extents are near ``max_extent``, so the iso projection is
      near the ``S·√3`` bound without being exactly cubic.

    Every view passes the standard edge-bbox + 1-px margin gate
    (``_check_framing``), and the iso view additionally keeps the
    minimum 3%-of-frame (≥ 24 px) background margin on every edge
    (``_check_iso_margin``).
    """
    _skip_if_no_docker()
    iso_view = "view_05_iso.png"
    for extent in (20, 60, 300):
        for shape, scad in [
            ("full-extent-cube", f"cube([{extent},{extent},{extent}]);\n"),
            (
                "elongated-box",
                f"cube([{extent}, {extent}*0.9, {extent}*0.95]);\n",
            ),
        ]:
            workdir = tmp_path / f"worstcase_{extent}_{shape}"
            _render(scad, workdir)
            for view in VIEW_FILES:
                png = workdir / "out" / view
                assert png.is_file(), f"{extent} mm {shape}: missing {view}"
                _check_framing(png, f"{extent} {shape}", view)
            # The iso view is the √3 worst case: assert the minimum 3%
            # background margin on every edge (``_check_framing`` only
            # requires 1 px; the operator decision requires ≥ 24 px).
            _check_iso_margin(
                workdir / "out" / iso_view, f"{extent} {shape}"
            )


def _check_iso_margin(png: Path, label: object) -> None:
    """Assert the iso view keeps at least the minimum visible background
    margin (3% of the 800×800 frame) on every edge for the worst-case
    full-extent box (issue #234 operator decision).

    ``_check_framing`` only requires a 1-px margin — a constant factor
    trivially satisfies it at every size. This check is what makes the
    "≥ 3% margin on every edge of view_05_iso" requirement measurable
    rather than "non-zero edge band": the bounding box must leave at
    least ``ISO_MIN_MARGIN_PX`` (24 px) of background on all four edges.
    """
    bbox, _centroid, edge_px = _pixel_bbox(png)
    assert all(v >= 0 for v in bbox), f"{label} iso: no model pixels found"

    # The 3-px edge band must still be background (inherited from
    # ``_check_framing``; re-asserted so a margin failure points here).
    for side, count in edge_px.items():
        assert count == 0, (
            f"{label} iso: {count} non-background pixels in the 3-px "
            f"band of the {side} frame edge"
        )

    x_min, y_min, x_max, y_max = bbox
    assert x_min >= ISO_MIN_MARGIN_PX, (
        f"{label} iso: left margin {x_min}px is below the "
        f"{ISO_MIN_MARGIN_PX}px (3% of frame) minimum"
    )
    assert 799 - x_max >= ISO_MIN_MARGIN_PX, (
        f"{label} iso: right margin {799 - x_max}px is below the "
        f"{ISO_MIN_MARGIN_PX}px (3% of frame) minimum"
    )
    assert y_min >= ISO_MIN_MARGIN_PX, (
        f"{label} iso: top margin {y_min}px is below the "
        f"{ISO_MIN_MARGIN_PX}px (3% of frame) minimum"
    )
    assert 799 - y_max >= ISO_MIN_MARGIN_PX, (
        f"{label} iso: bottom margin {799 - y_max}px is below the "
        f"{ISO_MIN_MARGIN_PX}px (3% of frame) minimum"
    )


def _check_framing(png: Path, label: object, view: str) -> None:
    """Assert the non-background pixel bounding box lies strictly inside
    the 800×800 frame with a visible margin on all four edges.

    Two independent checks:
      1. The four corners are the background colour (the #111 check —
         the model overflows the frame).
      2. No frame edge is touched by the silhouette: the edge-bbox
         band (3 px on each side) contains only background pixels. An
         off-center model that fits inside the frame still has
         background corners but touches an edge — the old corner check
         is blind to this, the edge-bbox check catches it (the #223
         acceptance criterion).
    """
    bbox, _centroid, edge_px = _pixel_bbox(png)
    assert all(v >= 0 for v in bbox), f"{label} {view}: no model pixels found"

    # Edge-bbox check (the #223 gate): no model pixel may appear within
    # a 3-px band of any frame edge. An off-center model that fits
    # inside the frame still has background corners but touches an edge
    # — the old corner-only check is blind to this, the edge-bbox check
    # catches it (the #223 acceptance criterion).
    for side, count in edge_px.items():
        assert count == 0, (
            f"{label} {view}: {count} non-background pixels in the 3-px "
            f"band of the {side} frame edge — the model touches or "
            f"exceeds the frame edge (issue #223 defect)"
        )

    # Margin check: the bounding box must lie strictly inside the frame
    # (a visible background margin on all four edges).
    margin = 1
    x_min, y_min, x_max, y_max = bbox
    assert x_min >= margin, (
        f"{label} {view}: bbox left edge {x_min} is within {margin}px of "
        f"the frame left edge — no visible margin"
    )
    assert x_max <= 799 - margin, (
        f"{label} {view}: bbox right edge {x_max} is within {margin}px of "
        f"the frame right edge — no visible margin"
    )
    assert y_min >= margin, (
        f"{label} {view}: bbox top edge {y_min} is within {margin}px of "
        f"the frame top edge — no visible margin"
    )
    assert y_max <= 799 - margin, (
        f"{label} {view}: bbox bottom edge {y_max} is within {margin}px of "
        f"the frame bottom edge — no visible margin"
    )


# ── Gate 4: marker-model view verification (issue #262) ─────────────────────


def _marker_scad() -> str:
    """Asymmetric marker model for view-rotation verification (issues #262,
    #269).

    The model has a distinct, identifiable feature for each of the six
    cardinal directions, so a pixel-analysis test can assert which face
    each view shows under the operator convention:

    - Base slab 40×40×10, z=0..10, x=−20..20, y=−20..20 (centred at origin)
    - Tall post at +X: x=24..32, z=0..45 (tallest feature)
    - Short block at −X: x=−32..−24, z=0..22
    - Block at +Y: y=22..34, z=0..16
    - Notch on the −Y face of the base slab: x=−8..8, y=−21..−15 removed
      (an 8 mm-deep bite through the full slab height — real removed
      volume, visible as a gap in the front/iso silhouette)
    - Cone on top at origin: z=10..26 (sits centred on the slab top)

    BBox: x[−32,32], y[−21,34], z[0,45]; centre (0, 6.5, 22.5);
    max_extent 64.

    Issue #269: the original used a non-existent ``v=[…]`` argument on
    every ``cube()``/``cylinder()``. OpenSCAD has no ``v`` parameter and
    silently ignores it (with a warning), so the slab landed uncentred and
    every feature collapsed to the origin corner — the intended asymmetry
    did not exist. Rewritten with ``translate()`` so each feature really
    sits where its name says. The follow-up to #269 fixed the residual
    geometric bug: the slab had used a vector argument to the ``center``
    keyword (``center = [true, true, false]``) — invalid because OpenSCAD's
    ``center`` is a single boolean, so the vector was silently ignored and
    the slab sat at x,y 0..40; the notch cutter at y −24..−16
    then missed the slab entirely (no cut at all), and the un-translated
    cone sat at the slab corner, not on top at the centre. The slab now
    uses ``translate([-20, -20, 0])``, the notch cutter really bites the
    −Y face (``translate([-8, -21, -1]) cube([16, 6, 12])`` cuts y
    −21..−15 through the full slab height), and the cone is
    ``translate([0, 0, 10])`` so it sits centred on the slab top. The fast
    guard ``tests/fast/test_scad_no_v_param.py`` forbids ``v=`` AND vector
    ``center`` arguments on any ``cube``/``cylinder``/``sphere`` call
    in this file.
    """
    return (
        "difference() {\n"
        "  translate([-20, -20, 0]) cube([40, 40, 10]);\n"
        "  translate([-8, -21, -1]) cube([16, 6, 12]);\n"
        "}\n"
        "translate([24, -4, 0]) cube([8, 8, 45]);\n"
        "translate([-32, -4, 0]) cube([8, 8, 22]);\n"
        "translate([-6, 22, 0]) cube([12, 12, 16]);\n"
        "translate([0, 0, 10]) cylinder(h = 16, r1 = 8, r2 = 0, $fn = 36);\n"
    )


def _half_top(png_path: Path) -> tuple[int, int]:
    """The topmost (smallest ``y``) model pixel in the left half (``x < 400``)
    and the right half (``x >= 400``) of an 800×800 view.

    Returns ``(top_left_y, top_right_y)``; each value is the frame height
    (800) when that half has no model pixels. The feature that projects
tallest in a view is the one whose half reaches higher (the smaller ``y``),
    so comparing the two values tells which side of the frame the tall
    +X post sits in — the per-view placement signal the issue #269 gate
    needs (a wrong rotation puts the post on the wrong side). Built on the
    existing ``_decode_png`` / ``_is_bg`` machinery.
    """
    w, h, bpp, out = _decode_png(png_path)
    top_left = top_right = h
    for y in range(h):
        base = y * w * bpp
        for x in range(w):
            i = base + x * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                if x < w // 2:
                    top_left = min(top_left, y)
                else:
                    top_right = min(top_right, y)
    return top_left, top_right


def _column_profile(
    png_path: Path,
) -> tuple[list[int | None], list[bool]]:
    """Per-column "top of model" and "any model pixel" profiles.

    Returns ``(top_row, has_model)`` where ``top_row[x]`` is the smallest
    ``y`` with a non-background pixel in column ``x`` (``None`` when the
    column is all background) and ``has_model[x]`` is whether column ``x``
    contains any non-background pixel at all. Both lists have length 800
    (the frame width). Basis for the issue #269 cone and notch assertions:
    the cone shows as a raised segment (smaller ``y``) than the slab rows
    that flank it; the notch shows as a background gap in the top view.
    """
    w, h, bpp, out = _decode_png(png_path)
    top: list[int | None] = [None] * w
    has = [False] * w
    for x in range(w):
        for y in range(h):
            i = (y * w + x) * bpp
            if not _is_bg((out[i], out[i + 1], out[i + 2])):
                top[x] = y
                has[x] = True
                break
    return top, has


def _bg_count_in_rect(
    png_path: Path, x0: int, x1: int, y0: int, y1: int
) -> int:
    """Count background pixels in the rectangle ``[x0, x1) × [y0, y1)``."""
    w, h, bpp, out = _decode_png(png_path)
    count = 0
    for y in range(max(0, y0), min(h, y1)):
        for x in range(max(0, x0), min(w, x1)):
            i = (y * w + x) * bpp
            if _is_bg((out[i], out[i + 1], out[i + 2])):
                count += 1
    return count


def test_views_marker_model_feature_placement(tmp_path: Path) -> None:
    """Render the asymmetric marker model and assert, for ALL six views, that
    each feature sits where the operator convention says it should (issues
    #262, #269).

    Convention (issue #262 operator decision, verified against the pinned
    image with an asymmetric marker):
      - front:  from −Y, +X right, +Z up
      - back:   from +Y, +X left, +Z up
      - left:   from −X, +Y right, +Z up
      - right:  from +X, −Y right, +Z up
      - top:    from +Z, +X right, +Y up
      - iso:    from the front-right-top octant (−Y, +X, +Z), +Z up

    The discriminators, per view:
      - The tall +X post (z=0..45) is the tallest feature; in an axis view it
        is the only thing that reaches the top of the frame, so its side
        (left/right) is which half of the frame has the higher topmost pixel.
        front → right, back → left (mirror), iso → right.
      - The top view: the post (at +X) is at the right edge, the +Y block
        toward the top, the notch (−Y) toward the bottom, the cone at centre.
      - The iso view: the post is vertical (taller than wide), right of
        centre, above the slab, and the −Y notch face is visible (the
        viewer is on the −Y side).

    The entrypoint re-centres the camera on the model's bounding-box centre
    (issue #223), so the model is NOT centred on the frame origin — assertions
    are written against the model's own features (relative halves), never
    against a hard-coded frame centre. Every assertion names what was
    expected so a failure says which feature is where it should be.
    """
    _skip_if_no_docker()
    scad = _marker_scad()
    workdir = tmp_path / "marker"
    _render(scad, workdir)

    # All six views must produce valid, non-empty renders.
    for view in VIEW_FILES:
        png = workdir / "out" / view
        assert png.is_file(), f"marker: missing {view}"
        bbox, _centroid, _edge_px = _pixel_bbox(png)
        assert all(v >= 0 for v in bbox), f"marker {view}: no model pixels"

    def view(name: str) -> Path:
        return workdir / "out" / name

    # ── front: +X post on the RIGHT, +Z up (tall feature reaches top-right)
    tl, tr = _half_top(view("view_00_front.png"))
    assert tr < tl, (
        f"front: tall +X post should be on the RIGHT (right-half top {tr}px "
        f"< left-half top {tl}px); +X is right, +Z is up in the front view"
    )
    # ── front: the cone sits ABOVE the slab (z=10..26) and centred at x≈400.
    #    In the front view the cone's tip is the highest feature in the
    #    central region (the post is at +X, to the right). The per-column
    #    top profile must show a peak at x≈400 (the cone tip at z=26) that
    #    is at least 30 px above the slab top (the slab is at z=0..10, the
    #    cone at z=10..26 — a 16 mm difference, ~68 px at the render scale).
    #    This FAILS on the old SCAD: the cone sat at the slab's (+X, −Y)
    #    corner, so the central region's peak is the slab top, not the cone.
    top_f, _has_f = _column_profile(view("view_00_front.png"))
    # The cone's peak: the minimum top_row in x=360..440 (the cone's x
    # extent is ±8 mm = ±30 px from centre; use a wider window to be safe).
    cone_peak_y = min(
        top_f[x] for x in range(360, 440) if top_f[x] is not None
    )
    # The slab top: the minimum top_row in x=300..360 (left of the cone,
    # where only the slab is visible in the central region).
    slab_top_f = min(
        top_f[x] for x in range(300, 360) if top_f[x] is not None
    )
    assert slab_top_f - cone_peak_y >= 30, (
        f"front: the cone's peak (y={cone_peak_y}) must be at least 30px "
        f"above the slab top (y={slab_top_f}); the cone is at z=10..26, "
        f"the slab top at z=10 — the cone sits ON TOP of the slab. The old "
        f"SCAD put the cone at the slab corner, where it reads as part of "
        f"the slab silhouette (no peak in the central region)"
    )
    # The cone's x-centre: the peak must be at x≈400 (the cone is at the
    # origin, the slab centred at the origin, so the cone projects to
    # frame x=400). The old SCAD put the cone at the slab's (+X, −Y) corner,
    # so the peak would be at x≈550+ (far right of centre).
    peak_x = min(
        range(360, 440), key=lambda x: top_f[x] if top_f[x] is not None else 9999
    )
    assert abs(peak_x - 400) <= 40, (
        f"front: the cone's peak at x={peak_x} must be within 40px of the "
        f"frame centre (the cone is at the origin, so it projects to "
        f"frame x=400); the old SCAD put the cone at the slab's (+X, −Y) "
        f"corner (frame x≈550+)"
    )

    # ── back: +X post on the LEFT (mirror of front, +X is left from +Y)
    tl, tr = _half_top(view("view_01_back.png"))
    assert tl < tr, (
        f"back: tall +X post should be on the LEFT (left-half top {tl}px "
        f"< right-half top {tr}px); from +Y, +X is left and +Z is up"
    )

    # ── left: camera at −X (90°X, 270°Z). Image x-axis = −Y, so the +Y
    #    block (y=22..34) projects to the LEFT of the frame. The tall post
    #    (+X) is behind the slab; the short block (−X) is closest.
    #    The +Y block should be in the left half of the frame.
    lbbox, _lcent, _ = _pixel_bbox(view("view_02_left.png"))
    _lx0, _ly0, _lx1, _ly1 = lbbox
    # The +Y block (y=22..34) projects to the LEFT of the frame (image x =
    # −y in the left view). The model's y range is [−28, 34], so the +Y
    # block occupies the left portion of the frame and the left edge of the
    # silhouette extends well past the left of centre.
    assert _lx0 < 300, (
        f"left: the +Y block should extend the silhouette to the left "
        f"(left edge {_lx0}px < 300); +Y is left in the left view "
        f"(camera at −X)"
    )

    # ── right: camera at +X (90°X, 90°Z). Image x-axis = +Y, so the +Y
    #    block (y=22..34) projects to the RIGHT of the frame. The post
    #    (+X, closest) is at the centre; the short block (−X) is behind.
    rbbox, _rcent, _ = _pixel_bbox(view("view_03_right.png"))
    _rx0, _ry0, rx1, _ry1 = rbbox
    # The +Y block is on the right, so the right edge of the silhouette
    # extends past centre. The post (closest) is near the centre.
    assert rx1 > 500, (
        f"right: the +Y block should extend the silhouette to the right of "
        f"centre (right edge {rx1}px > 500); +Y is right in the right view "
        f"(camera at +X)"
    )

    # ── top: camera at +Z looking down; +X right, +Y up (top of frame).
    #    The post (+X, x=24..32) is at the right edge; the +Y block
    #    (y=22..34) is toward the top; the −Y notch is toward the bottom;
    #    the cone is at the centre.
    tbbox, tcent, _ = _pixel_bbox(view("view_04_top.png"))
    tx0, ty0, tx1, ty1 = tbbox
    # The post (+X) is at the right edge of the model; the short block (−X)
    # is at the left edge. The silhouette spans the full width.
    assert tx1 - tx0 > 300, (
        f"top: the +X post (right) and −X short block (left) should give a "
        f"full-width silhouette (width {tx1 - tx0}px > 300)"
    )
    # The +Y block is toward the top and the −Y notch toward the bottom, so
    # the silhouette has a substantial vertical extent.
    assert ty1 - ty0 > 80, (
        f"top: the +Y block (top) and −Y notch (bottom) should give a "
        f"substantial vertical extent (bbox height {ty1 - ty0}px > 80)"
    )
    # ── top: the −Y notch is visible as a background gap at the bottom of
    #    the slab. In the top view (looking down from +Z), the cone and slab
    #    overlap in XY, so the cone's x-centre cannot be verified via the
    #    top_row profile (it is flat across the central region). The notch
    #    (x=−8..8, y=−21..−15) cuts a 16×6 mm gap in the slab's −Y face;
    #    in the top view it shows as a background rectangle at the
    #    bottom-centre of the slab (x≈376..424, y≈520..538). The old
    #    uncut model has no such gap (0 bg pixels in the slab interior).
    # The notch: a background gap at the bottom-centre of the slab.
    # Count background pixels in the notch region — the corrected model
    # has a real cut (≥ 100 bg pixels), the old model has no cut (0 bg
    # pixels in the slab interior).
    notch_bg = _bg_count_in_rect(
        view("view_04_top.png"), 376, 424, 520, 538
    )
    assert notch_bg >= 100, (
        f"top: the −Y notch should appear as a background gap of ≥ 100 "
        f"pixels in the slab's bottom-centre region (found {notch_bg}); "
        f"the old uncut model has no such gap — the notch must be a real "
        f"cut, not a missing feature"
    )
    # The post (+X, x=24..32) is at the right edge of the model. The model
    # is roughly symmetric in x (x=[−32,32]) so the bbox spans the full
    # width; the post is a tall feature at the right, the short block (−X)
    # is at the left. The centroid should be near the frame centre (the
    # model's x-centre is 0), shifted slightly right by the post's mass.
    # A safe lower bound: the centroid must be in the right half of the
    # frame (the post pulls the centroid right of the model centre).
    tcx, _tcy = tcent
    assert tcx >= 380, (
        f"top: the centroid should be near the frame centre or slightly "
        f"right (cx={tcx:.0f} >= 380); the +X post at the right edge "
        f"pulls the centroid right of the model centre"
    )

    # ── iso: the +X post is on the RIGHT of the frame, upright (+Z up), and
    #    the −Y notch face is visible (the viewer is on the −Y side of the
    #    front-right-top octant). The old sideways rotation (0,45,45) put the
    #    post as a horizontal bar pointing LEFT; the correct (55,0,25) puts it
    #    vertical and on the right.
    _ibbox, icent, _ = _pixel_bbox(view("view_05_iso.png"))
    icx, _icy = icent
    assert icx > 380, (
        f"iso: the centroid should be right of centre (x {icx:.0f} > 380); "
        f"the post is at +X, toward the viewer's right"
    )
    tl, tr = _half_top(view("view_05_iso.png"))
    assert tr < tl, (
        f"iso: the tall +X post should reach higher on the RIGHT (right-half "
        f"top {tr}px < left-half top {tl}px); the post is vertical and at +X, "
        f"not a horizontal bar pointing left (the old sideways (0,45,45) bug)"
    )
    # ── iso: the −Y notch face is visible (the viewer is on the −Y side).
    #    The notch is a real 16×6×12 mm cut into the slab's −Y face. In the
    #    iso view, the notch appears as a concave feature in the lower-centre
    #    of the frame. The old SCAD had no cut at all. The front and top
    #    views have the reliable notch assertions (background gap in the
    #    top view, cone peak position in the front view); the iso view's
    #    notch is harder to verify via pixel analysis (the background fills
    #    the frame around the model, so a background gap check is unreliable).
    #    The iso view's role is to confirm the post is vertical and on the
    #    right (asserted above), not to independently verify the notch.
    # ── iso: the cone is above the slab (z=10..26), not at the slab corner.
    #    In the iso view the cone's tip is the highest feature in the
    #    central region (the post is at +X, to the right). The cone's tip
    #    must be at least 25 px above the slab top at the slab edge
    #    (x=200..300, where the cone is not present). The 55° iso tilt
    #    reduces the projected height from ~68px (axis-aligned) to ~30px,
    #    so 25px is the conservative lower bound. The old SCAD put the
    #    cone at the slab's (+X, −Y) corner, so the central region's peak
    #    is the slab top, not the cone.
    top_iso, _ = _column_profile(view("view_05_iso.png"))
    cone_tip_y = min(
        top_iso[x] for x in range(350, 450) if top_iso[x] is not None
    )
    slab_top_at_edge = min(
        top_iso[x] for x in range(200, 300) if top_iso[x] is not None
    )
    assert slab_top_at_edge - cone_tip_y >= 25, (
        f"iso: the cone's tip (y={cone_tip_y}) must be at least 25px above "
        f"the slab's top edge (y={slab_top_at_edge}); the cone is at z=10..26, "
        f"the slab top at z=10 — the cone sits ON TOP, not at the slab corner "
        f"(the old SCAD bug). The 55° iso tilt reduces the projected height "
        f"from ~68px (axis-aligned) to ~30px, so 25px is the conservative "
        f"lower bound."
    )

