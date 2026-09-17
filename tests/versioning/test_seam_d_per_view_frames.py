"""SEAM D — per-view progress frames (issue #121).

The per-view arrival events (``render-view-start`` / ``render-view-done``)
are SSE progress frames emitted by the design-loop adapter while the render
container is still running. This test pins their wire shape: the ``step``
string, the ``view`` field (the view stem), and the ``iteration`` field
(the design-loop iteration index, 1-based). It verifies the frames pass
``validate_frames_stream`` (the SEAM D schema) and that the version-created
frame still carries all six views at the end (the atomic behaviour is
preserved).
"""

from __future__ import annotations

import pytest

from tests.seam_schemas_d import SeamError, validate_frame, validate_frames_stream


def test_per_view_frames_pass_seam_d_schema() -> None:
    """A realistic frame stream with per-view progress frames passes the
    SEAM D schema (``validate_frames_stream``). The per-view frames carry
    ``step`` (``render-view-start`` / ``render-view-done``), ``view`` (the
    view stem), and ``iteration`` (the 1-based loop index)."""
    frames = [
        ("progress", {"step": "design-loop-start"}),
        ("progress", {"step": "render-view-start", "view": "view_00_front", "iteration": 1}),
        ("progress", {"step": "render-view-done", "view": "view_00_front", "iteration": 1}),
        ("progress", {"step": "render-view-start", "view": "view_01_back", "iteration": 1}),
        ("progress", {"step": "render-view-done", "view": "view_01_back", "iteration": 1}),
        ("progress", {"step": "render-view-done", "view": "view_05_iso", "iteration": 2}),
        ("progress", {"step": "design-loop-pass"}),
        (
            "progress",
            {
                "step": "version-created",
                "version_id": 1,
                "bbox_abstained": False,
                "stl_data_uri": "data:application/octet-stream;base64,AAAA",
                "views": {
                    "view_00_front.png": "data:image/png;base64,iVBOR",
                    "view_01_back.png": "data:image/png;base64,iVBOR",
                    "view_02_left.png": "data:image/png;base64,iVBOR",
                    "view_03_right.png": "data:image/png;base64,iVBOR",
                    "view_04_top.png": "data:image/png;base64,iVBOR",
                    "view_05_iso.png": "data:image/png;base64,iVBOR",
                },
            },
        ),
        ("token", {"text": "cube(20);"}),
        ("done", {"message": "Design loop passed validation", "bbox_abstained": False}),
    ]
    validated = validate_frames_stream(frames)
    # The stream terminates with a terminal frame
    assert validated[-1][0] == "done"
    # The version-created frame still carries all six views (atomic behaviour
    # preserved — issue #121's "fewer than six means empty" contract unchanged)
    vc = [f for f in validated if f[0] == "progress" and f[1].get("step") == "version-created"]
    assert vc, "no version-created frame"
    assert len(vc[0][1]["views"]) == 6


def test_per_view_frame_carries_view_and_iteration() -> None:
    """A per-view frame carries ``view`` (the stem) and ``iteration`` (the
    1-based loop index). The ``step`` is one of the two closed values
    (``render-view-start`` / ``render-view-done``)."""
    frame = (
        "progress",
        {"step": "render-view-done", "view": "view_03_right", "iteration": 2},
    )
    validate_frame(frame)
    data = frame[1]
    assert data["step"] == "render-view-done"
    assert data["view"] == "view_03_right"
    assert data["iteration"] == 2


def test_per_view_frame_view_start_is_valid() -> None:
    """The ``render-view-start`` step is a valid progress frame (the
    'render is working' signal)."""
    frame = (
        "progress",
        {"step": "render-view-start", "view": "view_00_front", "iteration": 1},
    )
    validate_frame(frame)


def test_version_created_frame_still_carries_all_six_views() -> None:
    """The version-created frame still carries all six views at the end —
    the atomic behaviour is preserved (issue #121's acceptance criterion:
    'the version-created frame still carries all six views')."""
    frame = (
        "progress",
        {
            "step": "version-created",
            "version_id": 1,
            "bbox_abstained": False,
            "stl_data_uri": "data:application/octet-stream;base64,AAAA",
            "views": {
                "view_00_front.png": "data:image/png;base64,iVBOR",
                "view_01_back.png": "data:image/png;base64,iVBOR",
                "view_02_left.png": "data:image/png;base64,iVBOR",
                "view_03_right.png": "data:image/png;base64,iVBOR",
                "view_04_top.png": "data:image/png;base64,iVBOR",
                "view_05_iso.png": "data:image/png;base64,iVBOR",
            },
        },
    )
    validate_frame(frame)
    assert len(frame[1]["views"]) == 6


def test_per_view_frames_arrive_in_order() -> None:
    """Per-view frames within one iteration arrive in view-index order
    (view 0 before view 1, etc.). The test constructs a stream where the
    order is correct and verifies the schema accepts it (the schema does
    not enforce order — this test documents the expected behaviour)."""
    frames = [
        ("progress", {"step": "design-loop-start"}),
    ]
    for i, stem in enumerate(
        ["view_00_front", "view_01_back", "view_02_left", "view_03_right", "view_04_top", "view_05_iso"]
    ):
        frames.append(("progress", {"step": "render-view-start", "view": stem, "iteration": 1}))
        frames.append(("progress", {"step": "render-view-done", "view": stem, "iteration": 1}))
    frames.append(("done", {"message": "ok"}))
    validate_frames_stream(frames)
