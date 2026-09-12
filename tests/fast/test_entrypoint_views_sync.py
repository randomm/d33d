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
from pathlib import Path

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
    """``VIEW_CAMERAS[i]`` (comma-separated 7-float string) must equal
    the stringified ``VIEWS[i][1]`` camera tuple, in order."""
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
        assert parsed == tuple(float(v) for v in cam_tuple), (
            f"VIEW_CAMERAS[{i}] {cam_str!r} != VIEWS[{i}] camera tuple {cam_tuple!r}"
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
