"""Issue #432: the terminal exhausted error frame carries the attempt count
for every post-check reason (``attempts`` = the loop's ``iterations_used``),
omitted when unknown."""

from __future__ import annotations

import pytest

from d33d.design_loop import IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.versioning.helpers import create_project, run_async

POST_CHECK = (
    "mesh_unchanged",
    "through_hole_missing",
    "screw_clearance_wrong",
    "stack_height_mismatch",
)


def _exhausted(reason: str, iterations_used: int | None) -> object:
    render = RenderResult(
        ok=False,
        exit_code=1,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=(),
    )
    record = IterationRecord(
        iteration=1,
        scad_source="",
        render=render,
        score=Score(bits=(True,) * 5, rank=0, tiebreak=(False,) * 5),
        params={},
    )

    class _Result:
        status = "exhausted"
        best = record
        failure_reason = reason

    if iterations_used is not None:
        _Result.iterations_used = iterations_used
    return _Result()


def _error_frames(app, result: object) -> list[dict]:
    app.state.run_design_loop = lambda **kw: result

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await client.post(f"/api/projects/{pid}/chat", json={"message": "hi"})
        source = app.state.event_sources[pid]
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return [d for e, d in frames if e == "error"]

    return run_async(app, _call)


@pytest.mark.parametrize("reason", POST_CHECK)
def test_post_check_error_frame_carries_attempts_from_iterations_used(app_with_versions, reason):
    """Issue #432 operator decision: an identical-repair stop at attempt 2
    reports ``attempts == 2`` on the terminal frame (from iterations_used)."""
    frames = _error_frames(app_with_versions, _exhausted(reason, iterations_used=2))
    assert frames, "no error frame"
    assert frames[0]["reason"] == reason
    assert frames[0]["attempts"] == 2


def test_post_check_frame_omits_attempts_when_unknown(app_with_versions):
    """Omit-not-null: a post-check reason with no iterations_used → no key."""
    frames = _error_frames(app_with_versions, _exhausted("screw_clearance_wrong", None))
    assert frames and frames[0]["reason"] == "screw_clearance_wrong"
    assert "attempts" not in frames[0]


def test_non_post_check_frame_has_no_attempts(app_with_versions):
    """Only post-check reasons carry ``attempts``; a gate reason does not."""
    frames = _error_frames(app_with_versions, _exhausted("watertight", iterations_used=3))
    assert frames and frames[0]["reason"] == "watertight"
    assert "attempts" not in frames[0]
