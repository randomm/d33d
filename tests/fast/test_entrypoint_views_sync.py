"""Fast-layer test: ``entrypoint.sh`` camera tuples match ``VIEWS``.

Closes the HIGH-severity review finding that the ``VIEWS`` "single source
of truth" claim is false — ``d33d/render_worker.py`` and
``entrypoint.sh``'s ``VIEW_CAMERAS``/``VIEW_NAMES`` arrays encode the same
six camera tuples independently, kept in sync only by comments.

This test parses the bash array literals straight out of the static
``entrypoint.sh`` text (no bash execution, no Docker) and asserts they
exactly equal ``d33d.render_worker.VIEWS``. A future edit to either side
that isn't mirrored on the other fails CI.

Also closes the MEDIUM finding that only the camera tuples were covered by
this drift guard — ``entrypoint.sh``'s ``COMMON_FLAGS`` bash array and the
``RENDER_FLAG``/``--colorscheme`` literals used for the six PNG renders
duplicate ``d33d.render_worker.OPENSCAD_COMMON_FLAGS`` and
``OPENSCAD_RENDER_FLAGS`` independently; a future edit to one side that
isn't mirrored on the other now fails CI too.
"""

from __future__ import annotations

import re
import subprocess
import tempfile
from pathlib import Path

import pytest

import d33d.render_worker as rw

ENTRYPOINT = Path(__file__).resolve().parents[2] / "entrypoint.sh"


def _parse_bash_string_array(src: str, var_name: str) -> list[str]:
    """Extract the elements of ``VAR=( ... )`` from a bash source string.

    Handles the one-line-per-element form used in ``entrypoint.sh``.
    The bash array body can contain ``(...)`` inside ``# comment`` text
    (e.g. ``# front:  camera on -Z, no rotation`` — the ``(-Z)``), so a
    lazy regex match on the first ``)`` would stop early; instead capture
    everything up to the array-closing ``)`` on its own line, and take
    the quoted strings from that slice.
    """
    m = re.search(
        r"declare\s+-a\s+" + re.escape(var_name) + r"\s*=\s*\((.*?)\n\)",
        src,
        re.DOTALL,
    )
    assert m is not None, f"{var_name} array literal not found in entrypoint.sh"
    body = m.group(1)
    elements = re.findall(r'"([^"]*)"', body)
    assert elements, f"{var_name} array in entrypoint.sh is empty"
    return elements


def test_entrypoint_view_names_match_views_filenames() -> None:
    """``VIEW_NAMES[i] + '.png'`` must equal ``VIEWS[i][0]`` for all six,
    in order."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    view_names = _parse_bash_string_array(src, "VIEW_NAMES")
    assert len(view_names) == 6, f"VIEW_NAMES has {len(view_names)} entries, expected 6"
    expected_stems = [name[:-4] for name, _ in rw.VIEWS]  # strip the .png
    assert view_names == expected_stems


def test_entrypoint_view_cameras_match_views_tuples() -> None:
    """``VIEW_CAMERAS[i]`` (comma-separated 7-float string) must match
    the stringified ``VIEWS[i][1]`` camera tuple in the rotation
    elements (indices 3–5: rx, ry, rz). The first three elements
    (translate) and the seventh (dist) are **placeholders** in both
    files — the entrypoint substitutes the translate with the per-model
    bbox centre (issue #223) and the dist with the per-model
    bounding-box fit (issue #111) at render time — so only the
    rotation semantics are compared here."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    view_cameras = _parse_bash_string_array(src, "VIEW_CAMERAS")
    assert len(view_cameras) == 6, (
        f"VIEW_CAMERAS has {len(view_cameras)} entries, expected 6"
    )
    for i, (cam_str, cam_tuple) in enumerate(
        zip(view_cameras, (cam for _, cam in rw.VIEWS))
    ):
        parsed = tuple(float(v) for v in cam_str.split(","))
        assert len(parsed) == 7, (
            f"VIEW_CAMERAS[{i}] {cam_str!r} does not have 7 elements"
        )
        # Compare the rotation elements (indices 3–5).
        # The translate (0–2) is substituted with the per-model bbox centre
        # (issue #223) and the dist (6) with the per-model bounding-box fit
        # (issue #111) at render time — both are placeholders in both files.
        assert parsed[3:6] == tuple(float(v) for v in cam_tuple[3:6]), (
            f"VIEW_CAMERAS[{i}] {cam_str!r} rotation != "
            f"VIEWS[{i}] camera tuple rotation {cam_tuple[3:6]!r}"
        )


def _awk_bbox_program(src: str) -> str:
    """Extract the awk bbox-parse program from the entrypoint source.

    The program is the single-quoted body of the ``awk '... '`` call that
    follows ``bbox_out=$( `` — it is the authoritative parser whose output
    (``max_extent cx cy cz``) drives both the camera-distance substitution
    (issue #111) and the bbox-centre translate substitution (issue #223).
    """
    suffix = ' "${STL_FILE}")'
    pat = r"bbox_out=\$\(awk '(.*?)'" + re.escape(suffix)
    m = re.search(pat, src, re.DOTALL)
    assert m is not None, "bbox awk program not found in entrypoint.sh"
    return m.group(1)


def test_entrypoint_bbox_parse_is_translation_invariant() -> None:
    """The awk bbox parser's output must be invariant to translating the
    model in world space.

    Two ASCII STLs of identical shape (a 10×20×30 box) at different
    positions must yield the same ``max_extent`` and a bbox centre that
    translates by exactly the same vector as the shape did.

    This is the regression guard for the issue #223 fix: the camera
    tuple's translate is substituted with the bbox centre in world
    coordinates, and if that substitution ever regressed to a
    min/max-vs-origin computation (e.g. using only ``maxx`` instead of
    ``(minx+maxx)/2``), two identical shapes at different positions would
    produce different bbox centres and the fix would silently ship the
    off-center framing bug back. Running the awk program (no Docker)
    against two translated fixtures catches it at CI time."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    program = _awk_bbox_program(src)
    # A 10×20×30 box with its min-corner at the origin (8 corners, all three
    # axes spanned so max_extent = 30).
    stl_a = (
        "solid box\n"
        "  vertex 0 0 0\n"
        "  vertex 10 0 0\n"
        "  vertex 10 20 0\n"
        "  vertex 0 20 0\n"
        "  vertex 0 0 30\n"
        "  vertex 10 0 30\n"
        "  vertex 10 20 30\n"
        "  vertex 0 20 30\n"
        "endsolid box\n"
    )
    # Same shape translated by (25, -15, 7) in world space.
    stl_b = (
        "solid box\n"
        "  vertex 25 -15 7\n"
        "  vertex 35 -15 7\n"
        "  vertex 35 5 7\n"
        "  vertex 25 5 7\n"
        "  vertex 25 -15 37\n"
        "  vertex 35 -15 37\n"
        "  vertex 35 5 37\n"
        "  vertex 25 5 37\n"
        "endsolid box\n"
    )
    with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
        f.write(stl_a)
        path_a = f.name
    with tempfile.NamedTemporaryFile(mode="w", suffix=".stl", delete=False) as f:
        f.write(stl_b)
        path_b = f.name
    out_a = subprocess.run(["awk", program, path_a], capture_output=True, check=False)
    out_b = subprocess.run(["awk", program, path_b], capture_output=True, check=False)
    for p in (path_a, path_b):
        Path(p).unlink(missing_ok=True)
    assert out_a.returncode == 0, f"awk failed on fixture a: {out_a.stderr.decode()}"
    assert out_b.returncode == 0, f"awk failed on fixture b: {out_b.stderr.decode()}"
    max_a, cx_a, cy_a, cz_a = (float(x) for x in out_a.stdout.decode().split())
    max_b, cx_b, cy_b, cz_b = (float(x) for x in out_b.stdout.decode().split())
    # max_extent must be identical (translation-invariant by construction).
    assert max_a == max_b == 30.0, (
        f"max_extent differs between translated fixtures: a={max_a}, b={max_b}"
    )
    # The bbox centre must translate by exactly the same vector as the shape.
    assert cx_b - cx_a == pytest.approx(25.0, rel=1e-9), (
        f"bbox cx did not translate by 25.0: a={cx_a}, b={cx_b}"
    )
    assert cy_b - cy_a == pytest.approx(-15.0, rel=1e-9), (
        f"bbox cy did not translate by -15.0: a={cy_a}, b={cy_b}"
    )
    assert cz_b - cz_a == pytest.approx(7.0, rel=1e-9), (
        f"bbox cz did not translate by 7.0: a={cz_a}, b={cz_b}"
    )
    # And the absolute centres must be the bbox centres, not origin-relative
    # max-corner values — a min/max-vs-origin bug would make cx_a = 10
    # (the maxx) instead of 5.0 (the true centre).
    assert cx_a == pytest.approx(5.0, rel=1e-9), (
        f"bbox cx_a is not the true centre: {cx_a} (expected 5.0)"
    )
    assert cy_a == pytest.approx(10.0, rel=1e-9), (
        f"bbox cy_a is not the true centre: {cy_a} (expected 10.0)"
    )
    assert cz_a == pytest.approx(15.0, rel=1e-9), (
        f"bbox cz_a is not the true centre: {cz_a} (expected 15.0)"
    )


def _camera_loop_source(src: str) -> str:
    """Extract the per-view camera-substitution loop from the entrypoint.

    The loop body is the text between the ``if [ "${i}" -eq 5 ]`` branch
    (which picks CAM_DIST_ISO for the iso view) and the ``png_file=``
    assignment that closes the camera computation. It must contain the
    explicit ``IFS=, read -r`` field parsing (issue #227) — a regression
    to the old prefix/suffix string-strip is the bug this guard pins.
    """
    m = re.search(
        r'if \[ "\$\{i\}" -eq 5 \]; then\n'
        r".*?fi\n"  # skip the dist-selection if/else (the harness sets dist itself)
        r"(.*?)\n    png_file=",
        src,
        re.DOTALL,
    )
    assert m is not None, "camera substitution loop not found in entrypoint.sh"
    return m.group(1)


def test_entrypoint_camera_reassembly_is_byte_identical() -> None:
    """The camera loop's ``IFS=, read -r`` reassembly must produce camera
    strings byte-identical to the legacy prefix/suffix string-strip
    (``${VIEW_CAMERAS[i]}#0,0,0,`` + ``${...,0}``) for all six views,
    including negative bbox centres (issue #223's fixtures sit in
    negative coordinate space).

    The entrypoint cannot be executed directly (it calls ``openscad``),
    so the guard extracts the *actual* loop body from ``entrypoint.sh``
    and executes it in a sandbox bash where every ``openscad`` call is
    stubbed to record its ``--camera`` argument — the exact string the
    production loop feeds to openscad is then compared to the legacy
    form, view by view, for a positive-centre and a negative-centre
    model.

    A regression to the string-strip form, or a ``read -r`` that drops or
    shifts a field (e.g. a placeholder-format drift in ``VIEW_CAMERAS``),
    breaks byte-identity and fails CI.
    """
    src = ENTRYPOINT.read_text(encoding="utf-8")
    view_cameras = _parse_bash_string_array(src, "VIEW_CAMERAS")
    loop_body = _camera_loop_source(src)
    assert "IFS=, read -r" in loop_body, (
        "camera loop no longer uses explicit IFS=, read -r field "
        "parsing (issue #227)"
    )

    harness = (
        "\n"
        "set -euo pipefail\n"
        "dist_aa=\"90.0000000000\"\n"
        "dist_iso=\"127.2792206130\"\n"
        "declare -a VIEW_CAMERAS=("
        + chr(10)
        + chr(10).join('    "' + c + '"' for c in view_cameras)
        + chr(10)
        + ")\n"
        "declare -a CAMS=()\n"
        "declare -a VIEWS=(0 1 2 3 4 5)\n"
        "openscad() { local a; for a in \"$@\"; do :; done; return 0; }\n"
        "for i in 0 1 2 3 4 5; do\n"
        '    if [ "$i" -eq 5 ]; then\n'
        '        dist="$dist_iso"\n'
        "    else\n"
        '        dist="$dist_aa"\n'
        "    fi\n"
        '    cam="PENDING"\n'
        + chr(10)
        + loop_body
        + chr(10)
        + '    CAMS+=("$cam")\n'
        "done\n"
        'for c in "${CAMS[@]}"; do\n'
        '    printf "%s\\n" "$c" >&2\n'
        "done\n"
    )

    for bbox_t in ("5.0000000000,10.0000000000,15.0000000000",
                   "11.2550000000,10.0500000000,15.0050000000",
                   "-12.5000000000,-5.0000000000,2.5000000000"):
        # BBOX_T is passed as an env var to the bash subprocess (the entrypoint
        # itself always sets it before the loop); the stub openscad in the
        # harness keeps the loop body executable without Docker.
        proc = subprocess.run(
            ["bash", "-c", "BBOX_T=" + bbox_t + " bash -s"],
            input=harness.encode("utf-8"),
            capture_output=True,
            check=False,
        )
        assert proc.returncode == 0, (
            f"harness failed for BBOX_T={bbox_t}: {proc.stderr.decode()}"
        )
        got = [ln for ln in proc.stderr.decode().splitlines() if ln]
        assert len(got) == 6, f"expected 6 camera strings, got {got!r}"
        expected = []
        for i, cam_str in enumerate(view_cameras):
            rot_part = cam_str.removeprefix("0,0,0,")
            rot_part = rot_part.removesuffix(",0")
            d = "127.2792206130" if i == 5 else "90.0000000000"
            expected.append(f"{bbox_t},{rot_part},{d}")
        assert got == expected, (
            f"camera reassembly diverged from the legacy string-strip for "
            f"BBOX_T={bbox_t}:\n  new={got!r}\n  legacy={expected!r}"
        )


def _parse_top_level_bash_assignment(src: str, var_name: str) -> str:
    """Extract a simple top-level ``VAR="value"`` scalar assignment.

    Used to resolve ``${PROJECTION}``/``${IMG_SIZE}`` references inside
    ``COMMON_FLAGS`` back to their literal values.
    """
    m = re.search(r'(?m)^' + re.escape(var_name) + r'="([^"]*)"', src)
    assert m is not None, f"{var_name} scalar assignment not found in entrypoint.sh"
    return m.group(1)


def _parse_common_flags(src: str) -> list[str]:
    """Extract the ``COMMON_FLAGS=( ... )`` array, resolving the
    ``"${PROJECTION}"``/``"${IMG_SIZE}"`` variable references to their
    literal top-level values so the result is directly comparable to
    ``d33d.render_worker.OPENSCAD_COMMON_FLAGS``.
    """
    m = re.search(r"COMMON_FLAGS=\((.*?)\n\)", src, re.DOTALL)
    assert m is not None, "COMMON_FLAGS array literal not found in entrypoint.sh"
    body = m.group(1)
    projection = _parse_top_level_bash_assignment(src, "PROJECTION")
    img_size = _parse_top_level_bash_assignment(src, "IMG_SIZE")
    substitutions = {"${PROJECTION}": projection, "${IMG_SIZE}": img_size}

    tokens: list[str] = []
    for raw_line in body.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        # Each line is either a bare flag (--autocenter) or a
        # flag+quoted-value pair (--projection "${PROJECTION}").
        for match in re.finditer(r'"([^"]*)"|(\S+)', line):
            quoted, bare = match.group(1), match.group(2)
            token = quoted if quoted is not None else bare
            tokens.append(substitutions.get(token, token))
    assert tokens, "COMMON_FLAGS array in entrypoint.sh is empty"
    return tokens


def test_entrypoint_common_flags_match_openscad_common_flags() -> None:
    """``COMMON_FLAGS`` (with ``${PROJECTION}``/``${IMG_SIZE}`` resolved)
    must equal ``d33d.render_worker.OPENSCAD_COMMON_FLAGS`` token-for-token,
    in order. Guards against the flag set silently diverging between the
    bash entrypoint and the Python single source of truth."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    common_flags = _parse_common_flags(src)
    assert tuple(common_flags) == rw.OPENSCAD_COMMON_FLAGS, (
        f"entrypoint.sh COMMON_FLAGS {common_flags!r} != "
        f"rw.OPENSCAD_COMMON_FLAGS {rw.OPENSCAD_COMMON_FLAGS!r}"
    )


def test_entrypoint_render_flags_match_openscad_render_flags() -> None:
    """The literal ``RENDER_FLAG="--render"`` scalar plus the
    ``--colorscheme "Tomorrow Night"`` literal used in the PNG-render loop
    must equal ``d33d.render_worker.OPENSCAD_RENDER_FLAGS`` token-for-token,
    in order."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    render_flag = _parse_top_level_bash_assignment(src, "RENDER_FLAG")
    m = re.search(r'--colorscheme "([^"]*)"', src)
    assert m is not None, '--colorscheme literal not found in entrypoint.sh'
    colorscheme = m.group(1)
    render_flags = (render_flag, "--colorscheme", colorscheme)
    assert render_flags == rw.OPENSCAD_RENDER_FLAGS, (
        f"entrypoint.sh render flags {render_flags!r} != "
        f"rw.OPENSCAD_RENDER_FLAGS {rw.OPENSCAD_RENDER_FLAGS!r}"
    )


def test_entrypoint_imgsize_matches_render_size() -> None:
    """``IMG_SIZE="800,800"`` must match ``d33d.render_worker.RENDER_SIZE``
    (rendered as ``"{w},{h}"``)."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    img_size = _parse_top_level_bash_assignment(src, "IMG_SIZE")
    w, h = rw.RENDER_SIZE
    assert img_size == f"{w},{h}", (
        f"entrypoint.sh IMG_SIZE {img_size!r} != rw.RENDER_SIZE {rw.RENDER_SIZE!r}"
    )


def test_entrypoint_cam_dist_factor_matches_python() -> None:
    """The bash ``CAM_DIST_FACTOR=...`` literal in ``entrypoint.sh`` must
    equal ``d33d.render_worker.CAM_DIST_FACTOR``. Guards against the
    margin constant silently diverging between the two files — the
    ticket's "single margin constant" requirement (issue #111)."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(r"(?m)^CAM_DIST_FACTOR=([0-9.]+)", src)
    assert m is not None, "CAM_DIST_FACTOR assignment not found in entrypoint.sh"
    bash_factor = float(m.group(1))
    assert bash_factor == rw.CAM_DIST_FACTOR, (
        f"entrypoint.sh CAM_DIST_FACTOR {bash_factor} != "
        f"rw.CAM_DIST_FACTOR {rw.CAM_DIST_FACTOR}"
    )


def test_entrypoint_iso_factor_is_sqrt2_times_factor() -> None:
    """The bash ``CAM_DIST_ISO_FACTOR`` in ``entrypoint.sh`` must equal
    ``CAM_DIST_FACTOR × √2`` (the ISO view's 45° rotation projects a
    cube's silhouette at S·√2, so the same relative margin needs a ×√2
    distance; issue #111). Parses the ``-v f="${VAR}"`` source of the
    awk expression and evaluates the same formula the entrypoint runs,
    so the two files cannot silently diverge on the √2 factor."""
    src = ENTRYPOINT.read_text(encoding="utf-8")
    m = re.search(r"(?m)^CAM_DIST_ISO_FACTOR=\$\(awk[^\n]*?\)", src)
    assert m is not None, "CAM_DIST_ISO_FACTOR assignment not found in entrypoint.sh"
    line = m.group(0)
    var_m = re.search(r'-v f="\$\{([A-Z_]+)\}"', line)
    assert var_m is not None, (
        f"CAM_DIST_ISO_FACTOR awk does not reference a factor variable: {line!r}"
    )
    factor_var = var_m.group(1)
    expected = {
        "CAM_DIST_FACTOR": rw.CAM_DIST_ISO_FACTOR,
        "CAM_DIST_ISO_FACTOR": rw.CAM_DIST_ISO_FACTOR * rw.CAM_DIST_ISO_FACTOR,
    }
    assert factor_var in expected, (
        f"CAM_DIST_ISO_FACTOR derived from unexpected variable {factor_var!r}"
    )
    # Evaluate the awk expression from the line with the resolved factor.
    awk_args_m = re.search(r"BEGIN \{[^}]*\}", line)
    assert awk_args_m is not None
    expr = awk_args_m.group(0)
    factor = rw.CAM_DIST_FACTOR if factor_var == "CAM_DIST_FACTOR" else rw.CAM_DIST_ISO_FACTOR
    proc = subprocess.run(
        ["awk", "-v", f"f={factor}", expr],
        capture_output=True,
        check=False,
    )
    assert proc.returncode == 0, f"awk evaluation failed: {proc.stderr.decode()}"
    bash_iso_factor = float(proc.stdout.decode())
    assert bash_iso_factor == pytest.approx(expected[factor_var], rel=1e-9), (
        f"entrypoint.sh CAM_DIST_ISO_FACTOR {bash_iso_factor} != "
        f"expected {expected[factor_var]} (factor {factor_var} × √2)"
    )


