"""SEAM D — per-view frames arrive LIVE, not batched at render completion.

Issue #121 shipped the per-view frames (``render-view-start`` /
``render-view-done``) with the right SHAPE, but the adapter enqueued them
on ``_frame_queue`` and only yielded them AFTER the ``to_thread`` await
completed — i.e. after the render container had already exited. The
consumer's counter therefore filled 0→6 in a single instant at the end of
a 15-30 second render instead of across it.

The decisive test: inject a render function that blocks on an event, have
it emit a view marker, assert the consumer OBSERVES that frame while the
render is still blocked, and only then release the block and let the pass
finish. If the frame cannot be observed before release, the fix has not
worked.

The test uses a 3-second timeout on the drive to prevent a broken state
from hanging the suite — a batched adapter would release the render after
3 seconds and the assertion on ``observed_before_release`` would fail
loudly.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from d33d.design_loop import IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.versioning.helpers import create_project, run_async

#: How long the drive may take before the test reports FAILURE instead of
#: hanging. The render is NOT released by a timer — it is released only
#: when the consumer observes the frame (fixed) or when the drive times
#: out (broken → assertion failure).
DRIVE_TIMEOUT_S = 5.0


def _ok_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=1,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
    )


def _passing_result() -> Any:
    record = IterationRecord(
        iteration=1,
        scad_source="W = 20;\ncube([W, W, W]);",
        render=_ok_render(),
        score=Score(
            bits=(True, True, True, True),
            rank=4,
            tiebreak=(True, True, True, True),
        ),
        params={"W": 20.0, "D": 20.0, "H": 20.0},
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    return _Result()


def test_per_view_frame_delivered_while_render_still_running(
    app_with_versions: Any,
) -> None:
    """A per-view frame is observable by the consumer BEFORE the render
    finishes.

    Shape: the injected design-loop stub blocks on a ``threading.Event``
    (the "render still running" stand-in — the real render blocks in
    Docker ``subprocess.run`` on a worker thread, exactly like this
    stub), and its ``on_progress`` hook (the one the adapter BAKES IN and
    forwards into the loop — the same seam the real drain thread uses)
    fires a ``render-view-start`` marker while blocked. The consumer
    (driving ``run_design_loop_with_events``) must OBSERVE that frame
    while the render is still blocked. Only then is the block released
    and the pass allowed to finish.

    The frame is delivered via ``_on_progress`` → ``call_soon_threadsafe``
    → the queue the SSE generator drains, so this exercises the real
    delivery path end to end (adapter hook → live queue → yielded frame),
    with only the container's stderr-drain thread replaced by the stub
    thread.

    RED-CHECK: with the adapter yielding frames only after the ``to_thread``
    await completes, the consumer NEVER sees a per-view frame while the
    render is blocked — the drive times out after ``DRIVE_TIMEOUT_S``
    seconds and the assertion on ``observed_before_release`` fails loudly
    (the render is released after the timeout so the test cannot hang).
    """
    import threading

    from d33d.design_loop_events import run_design_loop_with_events

    release = threading.Event()
    # Populated by the consumer if/when it sees a per-view frame while
    # the render is still blocked.
    observed_before_release: list[tuple[str, dict[str, Any]]] = []

    class _BlockedRenderLoop:
        """Production-seam-shaped stub: returns a coroutine that blocks on
        ``release`` (the render running) and, while blocked, fires the
        adapter's per-view hook once from a worker thread (the drain-thread
        stand-in)."""

        def __call__(self, app: Any = None, **kwargs: Any) -> Any:
            on_progress = kwargs["on_progress"]  # the adapter's live hook

            async def _run() -> Any:
                # Stand in for the render's stderr drain thread: fire the
                # marker hook from a real worker thread while the render
                # is still blocked below.
                def _emit_marker() -> None:
                    on_progress(
                        "view-start",
                        {"view": "view_00_front", "iteration": 1},
                    )

                t = threading.Thread(target=_emit_marker, daemon=True)
                t.start()
                # The render is running: block until the test releases it.
                # If the frame never arrives (broken adapter), the drive
                # times out and the test releases here as cleanup.
                release.wait(timeout=DRIVE_TIMEOUT_S)
                t.join(timeout=1.0)
                return _passing_result()

            return _run()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _BlockedRenderLoop()
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="make a 20mm cube",
            stated_dims=(20.0, 20.0, 20.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a 20mm cube",
        )
        frames: list[tuple[str, dict[str, Any]]] = []

        async def _drive() -> None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
                if (
                    event == "progress"
                    and data.get("step", "").startswith("render-view-")
                ):
                    observed_before_release.append((event, data))
                    # The frame arrived live: release the render and let
                    # the pass finish.
                    release.set()

        try:
            await asyncio.wait_for(_drive(), timeout=DRIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            # The render was still blocked (no frame arrived live).
            # Release it so the test can finish and the assertion below
            # can report the failure.
            release.set()
        return frames, observed_before_release

    frames, observed = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert "done" in event_names, f"no done frame: {frames}"
    assert observed, (
        "the per-view frame was NOT delivered while the render was still "
        f"blocked — it arrived only after completion (batched, the old "
        f"behaviour). All frames: {frames}"
    )
    assert observed[0] == (
        "progress",
        {"step": "render-view-start", "view": "view_00_front", "iteration": 1},
    ), f"unexpected frame observed before release: {observed[0]}"


def test_live_delivery_preserves_frame_order(app_with_versions: Any) -> None:
    """While streaming live, per-view frames arrive in enqueue order and a
    ``render-view-done`` never precedes its own ``render-view-start``: the
    stub fires start→done→start→done for two views while the render
    blocks, and the consumer must see them in that order before release."""
    import threading

    from d33d.design_loop_events import run_design_loop_with_events

    release = threading.Event()
    observed_before_release: list[tuple[str, dict[str, Any]]] = []
    expected = [
        ("progress", {"step": "render-view-start", "view": "view_00_front", "iteration": 1}),
        ("progress", {"step": "render-view-done", "view": "view_00_front", "iteration": 1}),
        ("progress", {"step": "render-view-start", "view": "view_01_back", "iteration": 1}),
        ("progress", {"step": "render-view-done", "view": "view_01_back", "iteration": 1}),
    ]

    class _BlockedRenderLoop:
        """Same stub as the first test, but fires four markers
        (start→done→start→done for two views)."""

        def __call__(self, app: Any = None, **kwargs: Any) -> Any:
            on_progress = kwargs["on_progress"]

            async def _run() -> Any:
                def _emit_markers() -> None:
                    on_progress("view-start", {"view": "view_00_front", "iteration": 1})
                    on_progress("view-done", {"view": "view_00_front", "iteration": 1})
                    on_progress("view-start", {"view": "view_01_back", "iteration": 1})
                    on_progress("view-done", {"view": "view_01_back", "iteration": 1})

                t = threading.Thread(target=_emit_markers, daemon=True)
                t.start()
                release.wait(timeout=DRIVE_TIMEOUT_S)
                t.join(timeout=1.0)
                return _passing_result()

            return _run()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _BlockedRenderLoop()
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="make a 20mm cube",
            stated_dims=(20.0, 20.0, 20.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a 20mm cube",
        )
        frames: list[tuple[str, dict[str, Any]]] = []

        async def _drive() -> None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
                if (
                    event == "progress"
                    and data.get("step", "").startswith("render-view-")
                ):
                    observed_before_release.append((event, data))
                    if len(observed_before_release) == len(expected):
                        release.set()

        try:
            await asyncio.wait_for(_drive(), timeout=DRIVE_TIMEOUT_S)
        except asyncio.TimeoutError:
            release.set()
        return frames, observed_before_release

    frames, observed = run_async(app_with_versions, _call)
    event_names = [f[0] for f in frames]
    assert "done" in event_names, f"no done frame: {frames}"
    assert observed == expected, (
        "per-view frames were reordered or lost while streaming live: "
        f"got {observed}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-x", "-q"])
