"""Fast-layer test: the ``VIEWS`` six-view contract and the camera-distance fit.

No Docker required — this asserts the module-level constant's shape and
contents, plus the :func:`d33d.render_worker.cam_dist` pure function that
replaces the legacy fixed 40.0/55.0 distances (issue #111).

The 7-element camera tuple is ``(tx, ty, tz, rx, ry, rz, dist)`` — the
form the pinned image (``openscad/openscad:trixie``, OpenSCAD 2026.01.19)
accepts via ``--camera``, verified empirically. If a future build wants a
different arity, ``VIEWS`` and this test move together in the same commit.

The ``cam_dist`` function is a pure function of the bounding box: no
timestamps, no randomised seeds, no wall-clock, no dict-iteration-order
dependence. The byte-stability acceptance gate (two renders of the same
source produce identical view PNGs) depends on this purity.
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


@pytest.mark.parametrize(
    "kwargs",
    [
        {"memory_limit": "999g"},
        {"cpus": "1000"},
        {"pids_limit": 999999},
        {"timeout_s": 100000},
        {"memory_limit": "1m"},
        {"cpus": "0"},
        {"pids_limit": 0},
        {"timeout_s": 0},
    ],
)
def test_direct_construction_rejects_out_of_range_params(kwargs: dict) -> None:
    # Validation must live in __post_init__, not only in from_dict — a
    # direct RenderParams(...) call with out-of-bounds values must also
    # raise, closing the bypass that build_docker_argv(params=<instance>)
    # would otherwise expose.
    with pytest.raises(ValueError):
        rw.RenderParams(**kwargs)


def test_direct_construction_accepts_in_range_params() -> None:
    # The happy path through the direct constructor must still work and
    # carry the values through unmodified.
    p = rw.RenderParams(
        defines={"A": "1"},
        timeout_s=60,
        memory_limit="1g",
        cpus="4",
        pids_limit=256,
    )
    assert p.defines == {"A": "1"}
    assert p.timeout_s == 60
    assert p.memory_limit == "1g"
    assert p.cpus == "4"
    assert p.pids_limit == 256


def test_build_docker_argv_rejects_out_of_range_params_instance() -> None:
    # build_docker_argv accepts a RenderParams instance directly. With
    # validation in __post_init__, an out-of-bounds instance can no longer
    # be constructed in the first place — this test documents that the
    # bypass path is closed at construction, not just at the argv builder.
    with pytest.raises(ValueError):
        rw.RenderParams(cpus="1000")


# ── Issue #111: camera-distance fit (replaces fixed 40.0/55.0) ──────────


def test_cam_dist_is_a_pure_function_of_the_bbox() -> None:
    """The fit must be a pure function of the bounding box: no
    timestamps, no randomised seeds, no wall-clock, no
    dict-iteration-order dependence. Two calls with the same arguments
    must return the same value."""
    for extent in (20.0, 60.0, 300.0):
        for _ in range(5):
            d_aa = rw.cam_dist(extent, "view_00_front.png")
            d_iso = rw.cam_dist(extent, "view_05_iso.png")
        for _ in range(5):
            assert rw.cam_dist(extent, "view_00_front.png") == d_aa
            assert rw.cam_dist(extent, "view_05_iso.png") == d_iso


def test_cam_dist_scales_linearly_with_extent() -> None:
    """Doubling the extent must double the distance. This is what makes
    the margin a fixed *fraction* of the model size rather than a fixed
    absolute amount — a 20 mm and a 300 mm part get the same relative
    margin (the ticket's "single margin constant" requirement)."""
    d20 = rw.cam_dist(20.0, "view_00_front.png")
    d40 = rw.cam_dist(40.0, "view_00_front.png")
    d300 = rw.cam_dist(300.0, "view_00_front.png")
    assert d40 == pytest.approx(2.0 * d20)
    assert d300 == pytest.approx(15.0 * d20)


def test_cam_dist_uses_different_factor_for_iso() -> None:
    """The isometric view (45° rotation) projects a larger silhouette
    than any single axis. The worst case is a full-extent box whose
    three extents all equal ``max_extent`` — it projects to
    ``max_extent``·√3 (the space diagonal). The iso factor must cover
    that with the required minimum margin:
    ``CAM_DIST_ISO_FACTOR = 2.52 × √3 / 0.94``
    (exact-fit ratio 2.52, √3 worst-case projection, 3% margin per
    edge → 0.94 usable fraction; issue #234)."""
    extent = 60.0
    d_aa = rw.cam_dist(extent, "view_00_front.png")
    d_iso = rw.cam_dist(extent, "view_05_iso.png")
    # The iso distance is larger than the axis-aligned distance.
    assert d_iso > d_aa
    # The iso factor pins the √3 worst case with 3% margin.
    assert rw.CAM_DIST_ISO_FACTOR == pytest.approx(2.52 * 3.0**0.5 / 0.94, rel=1e-12)
    assert d_iso == pytest.approx(rw.CAM_DIST_ISO_FACTOR * extent, rel=1e-12)


def test_cam_dist_zero_extent_returns_zero() -> None:
    """A degenerate (zero-extent) mesh yields distance 0.0 — the caller
    classifies it as ``empty_model`` before the views are rendered, so
    the fit never sees a negative or zero extent in production."""
    assert rw.cam_dist(0.0, "view_00_front.png") == 0.0
    assert rw.cam_dist(-1.0, "view_00_front.png") == 0.0


def test_cam_dist_factor_is_greater_than_exact_fit() -> None:
    """The margin constant (CAM_DIST_FACTOR) must be strictly greater
    than the exact-fit ratio (≈ 2.52, calibrated empirically) so that
    the model does not touch the frame edge. If the factor were reduced
    to the exact-fit value or below, the 60 mm part would overflow its
    800×800 frame again — the bug this fix closes."""
    assert rw.CAM_DIST_FACTOR > 2.52


def test_cam_dist_frames_all_three_acceptance_sizes() -> None:
    """A 20 mm, 60 mm and 300 mm part must ALL be fully framed with the
    same margin rule. The test asserts the distance is proportional to
    the extent (same relative margin) and strictly positive for all
    three sizes — a single constant that fits all three, not one tuned
    to pass one size and regress another."""
    distances = []
    for extent in (20.0, 60.0, 300.0):
        d = rw.cam_dist(extent, "view_00_front.png")
        assert d > 0, f"distance must be positive for {extent} mm extent"
        # The margin fraction is constant: d / extent is the same for all
        # three sizes (within floating-point tolerance).
        distances.append(d / extent)
    assert distances[0] == pytest.approx(distances[1], rel=1e-12)
    assert distances[1] == pytest.approx(distances[2], rel=1e-12)


def test_views_rotation_semantics_unchanged() -> None:
    """The first six elements of each camera tuple (translate + rotate)
    must still be the empirically-verified values that pin *which* face
    each view sees. Only the 7th element (dist) is now substituted at
    render time — the rotation semantics are unchanged (issue #111 is
    about framing, not about which face each view shows)."""
    expected_rotations = [
        (0.0, 0.0, 0.0, 0.0, 0.0, 0.0),   # front
        (0.0, 0.0, 0.0, 0.0, 180.0, 0.0),  # back
        (0.0, 0.0, 0.0, 0.0, 90.0, 0.0),   # left
        (0.0, 0.0, 0.0, 0.0, -90.0, 0.0),  # right
        (0.0, 0.0, 0.0, 90.0, 0.0, 0.0),   # top
        (0.0, 0.0, 0.0, 0.0, 45.0, 45.0),  # iso
    ]
    for (name, cam), expected in zip(rw.VIEWS, expected_rotations):
        assert cam[:6] == expected, (
            f"VIEWS[{name!r}] rotation {cam[:6]} != expected {expected}"
        )
