"""Fast-layer test: ``result.json`` shape, parsing, ``ok`` derivation.

No Docker required — synthetic payloads only. Asserts the exact shape
the caller writes, the closed ``error_class`` enum, and that
``artifacts.views`` holds the six bare filenames, never on-volume
paths (the work volume is internal to the caller and its path must never
leak).
"""

from __future__ import annotations

import json

import d33d.render_worker as rw

SIX_VIEWS = [
    "view_00_front.png",
    "view_01_back.png",
    "view_02_left.png",
    "view_03_right.png",
    "view_04_top.png",
    "view_05_iso.png",
]


def _valid_result_dict() -> dict:
    return {
        "ok": True,
        "exit_code": 0,
        "duration_ms": 1234,
        "error_class": "ok",
        "stderr": "",
        "artifacts": {
            "stl": "model.stl",
            "csg": "model.csg",
            "views": list(SIX_VIEWS),
        },
    }


def test_result_round_trips_through_json() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    out = rw.result_to_json(result)
    parsed = json.loads(out)
    assert parsed == d


def test_error_class_is_a_closed_enum() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    assert result.error_class in rw.ERROR_CLASSES


def test_views_are_bare_filenames_not_on_volume_paths() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    for v in result.views:
        assert "/" not in v, f"host path leaked into result.json: {v!r}"
        assert not v.startswith("/")


def test_ok_is_derived_correctly() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    assert result.ok is True
    assert result.error_class == "ok"


def test_views_has_exactly_six_entries() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    assert len(result.views) == 6


def test_result_does_not_leak_host_paths() -> None:
    d = _valid_result_dict()
    result = rw.RenderResult.from_dict(d)
    for key in ("stl", "csg"):
        val = getattr(result, key)
        if val is not None:
            assert "/" not in val, f"host path leaked into {key}: {val!r}"


def test_ok_derivation_requires_all_conditions() -> None:
    """ok=true only when exit 0 AND all 8 artifacts present AND 6 PNGs
    valid AND STL vertex count > 0."""
    assert (
        rw.derive_ok(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=list(SIX_VIEWS),
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        is True
    )


def test_ok_derivation_fails_on_nonzero_exit() -> None:
    assert (
        rw.derive_ok(
            exit_code=1,
            stl_path="model.stl",
            csg_path="model.csg",
            views=list(SIX_VIEWS),
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        is False
    )


def test_ok_derivation_fails_on_zero_vertex_stl() -> None:
    assert (
        rw.derive_ok(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=list(SIX_VIEWS),
            vertex_count=0,
            watertight=True,
            volume=1.0,
        )
        is False
    )


def test_ok_derivation_fails_on_missing_csg() -> None:
    assert (
        rw.derive_ok(
            exit_code=0,
            stl_path="model.stl",
            csg_path=None,
            views=list(SIX_VIEWS),
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        is False
    )


def test_ok_derivation_fails_on_missing_view() -> None:
    assert (
        rw.derive_ok(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS[:5],  # only 5 views
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        is False
    )


def test_artifact_error_when_a_view_is_missing() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=SIX_VIEWS[:5],
        )
        == "artifact_error"
    )


def test_artifact_error_when_csg_missing() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path=None,
            views=list(SIX_VIEWS),
        )
        == "artifact_error"
    )


def test_ok_requires_all_conditions_classify() -> None:
    assert (
        rw.classify(
            exit_code=0,
            stl_path="model.stl",
            csg_path="model.csg",
            views=list(SIX_VIEWS),
            vertex_count=100,
            watertight=True,
            volume=1.0,
        )
        == "ok"
    )


def test_result_json_never_contains_a_host_path_in_views() -> None:
    result = rw.RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=tuple(SIX_VIEWS),
    )
    out = rw.result_to_json(result)
    # No absolute path anywhere in the serialized result.
    assert "/work/" not in out
    assert "/tmp/" not in out
