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

import pytest

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


def test_default_params_pass_range_validation() -> None:
    # The module defaults must always be valid; from_dict({}) exercises
    # the same validation path as every explicit override.
    params = rw.RenderParams.from_dict({})
    assert params.memory_limit == rw.DEFAULT_MEMORY_LIMIT
    assert params.cpus == rw.DEFAULT_CPU_LIMIT
    assert params.pids_limit == rw.DEFAULT_PID_LIMIT
    assert params.timeout_s == rw.DEFAULT_TIMEOUT_S


@pytest.mark.parametrize(
    "key,value",
    [
        ("memory_limit", "999g"),  # far above MAX_MEMORY_MB
        ("memory_limit", "1m"),  # far below MIN_MEMORY_MB
        ("memory_limit", "not-a-size"),  # malformed
        ("cpus", "1000"),  # far above MAX_CPUS
        ("cpus", "0"),  # at/below MIN_CPUS
        ("cpus", "nope"),  # malformed
        ("pids_limit", 999999),  # far above MAX_PID_LIMIT
        ("pids_limit", 0),  # below MIN_PID_LIMIT
        ("timeout_s", 100000),  # far above MAX_TIMEOUT_S
        ("timeout_s", 0),  # below MIN_TIMEOUT_S
    ],
)
def test_out_of_range_or_malformed_params_raise_value_error(
    key: str, value: object
) -> None:
    # A malicious or malformed params.json must never silently raise the
    # resource ceilings above the DoS guard's intent.
    with pytest.raises(ValueError):
        rw.RenderParams.from_dict({key: value})


@pytest.mark.parametrize(
    "key,value",
    [
        ("memory_limit", "64m"),  # MIN_MEMORY_MB boundary
        ("memory_limit", "8g"),  # MAX_MEMORY_MB boundary (8192 MiB)
        ("cpus", "0.1"),  # MIN_CPUS boundary
        ("cpus", "8"),  # MAX_CPUS boundary
        ("pids_limit", 16),  # MIN_PID_LIMIT boundary
        ("pids_limit", 2048),  # MAX_PID_LIMIT boundary
        ("timeout_s", 1),  # MIN_TIMEOUT_S boundary
        ("timeout_s", 900),  # MAX_TIMEOUT_S boundary
    ],
)
def test_in_range_boundary_params_are_accepted(key: str, value: object) -> None:
    params = rw.RenderParams.from_dict({key: value})
    assert getattr(params, key) == (
        str(value) if key in ("memory_limit", "cpus") else value
    )


def test_build_docker_argv_rejects_out_of_range_dict_params() -> None:
    # build_docker_argv accepts a raw dict and routes it through
    # RenderParams.from_dict — the range guard must not be bypassable by
    # calling it directly with a dict instead of a RenderParams instance.
    with pytest.raises(ValueError):
        rw.build_docker_argv("image", "render-deadbeef", params={"cpus": "1000"})
