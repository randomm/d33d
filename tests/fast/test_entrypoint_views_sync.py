"""Fast-layer test: ``entrypoint.sh`` camera tuples match ``VIEWS``.

Closes the HIGH-severity review finding that the ``VIEWS`` "single source
of truth" claim is false — ``d33d/render_worker.py`` and
``entrypoint.sh``'s ``VIEW_CAMERAS``/``VIEW_NAMES`` arrays encode the same
six camera tuples independently, kept in sync only by comments.

This test parses the bash array literals straight out of the static
``entrypoint.sh`` text (no bash execution, no Docker) and asserts they
exactly equal ``d33d.render_worker.VIEWS``. A future edit to either side
that isn't mirrored on the other fails CI.
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
