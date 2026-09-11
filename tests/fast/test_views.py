"""Fast-layer test: the ``VIEWS`` six-view contract.

No Docker required — this asserts the module-level constant's shape and
contents, which is the single source of truth read by the entrypoint,
the caller and the slow-layer golden fixture test.

The 7-element camera tuple is ``(tx, ty, tz, rx, ry, rz, dist)`` — the
form the pinned image (``openscad/openscad:trixie``, OpenSCAD 2026.01.19)
accepts via ``--camera``, verified empirically. If a future build wants a
different arity, ``VIEWS`` and this test move together in the same commit.
"""

from __future__ import annotations

import d33d.render_worker as rw

EXPECTED_FIVE_VIEWS = [
    "view_00_front.png",
    "view_01_back.png",
    "view_02_left.png",
    "view_03_right.png",
    "view_04_top.png",
    "view_05_iso.png",
]


def test_views_is_a_module_level_constant_list_of_tuples() -> None:
    assert isinstance(rw.VIEWS, list)


def test_views_yields_exactly_the_six_ordered_filenames() -> None:
    assert [name for name, _ in rw.VIEWS] == EXPECTED_FIVE_VIEWS


def test_views_has_exactly_six_entries() -> None:
    assert len(rw.VIEWS) == 6


def test_each_camera_tuple_is_seven_numbers() -> None:
    """Each camera is a 7-element ``(tx, ty, tz, rx, ry, rz, dist)`` tuple
    of numbers. The spec pins the annotation as a 7-float tuple; int
    values are accepted because Python does not distinguish them at the
    value level and the entrypoint stringifies them."""
    for name, cam in rw.VIEWS:
        assert isinstance(name, str)
        assert isinstance(cam, tuple)
        assert len(cam) == 7, f"camera for {name!r} has {len(cam)} elements, expected 7"
        for v in cam:
            assert isinstance(v, (int, float)), f"camera element {v!r} is not a number"


def test_common_flags_include_autocenter_and_projection_and_imgsize() -> None:
    """The verified working invocation carries ``--autocenter``,
    ``--projection o`` and ``--imgsize 800,800``. ``--autocenter`` and
    ``--colorscheme`` were confirmed present in the pinned image via
    ``openscad --help`` (see the module docstring and
    ``docs/bosl2-pinning.md``) — the spec forbade assuming them, so they
    are now asserted from the empirical record."""
    assert "--autocenter" in rw.OPENSCAD_COMMON_FLAGS
    assert "--projection" in rw.OPENSCAD_COMMON_FLAGS
    assert "o" in rw.OPENSCAD_COMMON_FLAGS
    assert "--imgsize" in rw.OPENSCAD_COMMON_FLAGS
    assert "800,800" in rw.OPENSCAD_COMMON_FLAGS


def test_render_flags_include_render_and_colorscheme() -> None:
    assert "--render" in rw.OPENSCAD_RENDER_FLAGS
    assert "--colorscheme" in rw.OPENSCAD_RENDER_FLAGS
    assert "Tomorrow Night" in rw.OPENSCAD_RENDER_FLAGS


def test_render_size_is_800x800() -> None:
    assert rw.RENDER_SIZE == (800, 800)
