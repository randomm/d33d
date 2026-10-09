"""Issue #417 lens-round-3 fixes.

Two defects from the lens review:

1. ``design_loop_events`` raised ``NameError`` on the NON-awaitable
   (sync loop) path when the timeout result carried no
   ``attempt_latencies``: ``_attempt_tracker`` is only assigned inside
   the ``inspect.isawaitable`` branch, but the timeout-version fallback
   referenced it unconditionally.

2. The adapter safety-net deadline was sized from the per-attempt LLM
   budget only (120 s × 3 + 60 s = 420 s), but each attempt ALSO
   includes the render (the render worker's own subprocess timeout,
   120 s) and post-checks after the budgeted LLM call. A legitimately
   slow, progressing run (LLM ~90 s + render ~60 s, × 3 = ~450 s) was
   killed by the net with NO version kept — the exact QA failure #417
   exists to fix. The net is now sized from the true per-attempt
   worst case: (LLM budget + render worker timeout) × MAX_ITERATIONS +
   margin.
"""

from __future__ import annotations

import asyncio

from d33d.design_loop import IterationRecord, Score
from d33d.design_loop_events import (
    ADAPTER_DEADLINE_MARGIN_SECONDS,
    ADAPTER_RENDER_ALLOWANCE_SECONDS,
    DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS,
    DESIGN_LOOP_TIMED_OUT_REASON,
    MAX_ITERATIONS,
    run_design_loop_with_events,
)
from d33d.render_worker import DEFAULT_TIMEOUT_S, RenderResult
from tests.versioning.helpers import create_project, run_async


def _default_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v",) * 6,
    )


class _TimeoutStubResult:
    """A sync (non-awaitable) loop result: exhausted with the structured
    ``design_loop_timed_out`` reason, a RENDERED best candidate, and NO
    ``attempt_latencies`` (the stub's own deadline never cut — it is not
    the real loop's deadline path), which is exactly the shape that drove
    the NameError fallback."""

    def __init__(self) -> None:
        self.status = "exhausted"
        self.failure_reason = DESIGN_LOOP_TIMED_OUT_REASON
        self.attempts_started = 0
        self.attempt_latencies = None
        self.best = IterationRecord(
            iteration=1,
            scad_source="W = 20;\ncube([W, 20, 20]);\n",
            render=_default_render(),
            score=Score(bits=(True,) * 5, rank=5, tiebreak=(True,) * 5),
            params={"W": 20.0},
        )


# ---------------------------------------------------------------------------
# (1) NameError: sync loop, exhausted timed-out result, no attempt_latencies
# ---------------------------------------------------------------------------


def test_sync_loop_timeout_without_latencies_emits_terminal_frame(
    app_with_versions, monkeypatch
):
    """A sync stub loop returning an exhausted ``design_loop_timed_out``
    result with NO ``attempt_latencies`` must produce a terminal error
    frame (the slow-model copy, no fabricated numbers) WITHOUT crashing
    — the fallback referenced ``_attempt_tracker`` which only exists on
    the awaitable path (NameError before the fix).
    """
    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 2.0 / 3
    )
    monkeypatch.setattr(
        "d33d.design_loop_events.ADAPTER_DEADLINE_MARGIN_SECONDS", 1.0
    )
    monkeypatch.setattr(
        "d33d.design_loop_events.ADAPTER_RENDER_ALLOWANCE_SECONDS", 1.0
    )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # SYNC loop (not a coroutine factory) — the non-awaitable path.
        app_with_versions.state.run_design_loop = (
            lambda **kw: _TimeoutStubResult()
        )
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="hi",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="hi",
        )
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return frames

    frames = run_async(app_with_versions, _call)
    events = [f[0] for f in frames]
    assert "error" in events, f"no terminal error frame: {frames}"
    assert frames[-1][0] == "error", f"error is not last: {frames}"
    error_data = frames[-1][1]
    assert error_data.get("reason") == DESIGN_LOOP_TIMED_OUT_REASON, (
        f"wrong reason: {error_data}"
    )
    # The timeout-version path renders the kept candidate — the stub's
    # best candidate rendered, so a version is created and announced
    # BEFORE the terminal frame (same frame order as the adapter path).
    version_frames = [
        f
        for f in frames
        if f[0] == "progress" and f[1].get("step") == "version-created"
    ]
    assert version_frames, (
        f"kept candidate must be versioned before the terminal frame: {frames}"
    )
    # No fabricated numbers: no loop-sourced latencies and no
    # frame-derived tracker exist on this path — both fields omitted
    # (the SPA never renders a number it has not established).
    assert "attempt_latency_seconds" not in error_data, error_data
    assert "attempt_count" not in error_data, error_data


# ---------------------------------------------------------------------------
# (2) Adapter safety net sized from the true per-attempt worst case
# ---------------------------------------------------------------------------


def test_adapter_net_includes_render_time():
    """The net must cover (LLM budget + render allowance) × MAX_ITERATIONS,
    not just the LLM budget × MAX_ITERATIONS. The render allowance is the
    render worker's own subprocess timeout constant (120 s)."""
    assert ADAPTER_RENDER_ALLOWANCE_SECONDS == DEFAULT_TIMEOUT_S
    net = (
        (DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS + ADAPTER_RENDER_ALLOWANCE_SECONDS)
        * MAX_ITERATIONS
        + ADAPTER_DEADLINE_MARGIN_SECONDS
    )
    assert net == 780.0, f"unexpected net: {net}"


def test_slow_progressing_run_not_cut_off_by_net(app_with_versions, monkeypatch):
    """A legitimately slow, PROGRESSING run whose attempts each take LLM
    plus render time — summing PAST the old 420-s-equivalent net but
    WITHIN the new net — must finish all attempts and emit its ordinary
    terminal frame, NOT be cut off by the adapter safety net.

    Scaled-down constants (monkeypatched): old net = (1.0 + 0.0) × 3 +
    0.1 = 3.1 s; new net = (1.0 + 1.0) × 3 + 0.1 = 6.1 s. The stub takes
    ~1.4 s per attempt × 3 = ~4.2 s: past the old 3.1 s net (where it
    would have been killed with no version), within the new 6.1 s net.
    """
    import time as _time

    monkeypatch.setattr(
        "d33d.design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS", 1.0
    )
    monkeypatch.setattr(
        "d33d.design_loop_events.ADAPTER_DEADLINE_MARGIN_SECONDS", 0.1
    )
    monkeypatch.setattr(
        "d33d.design_loop_events.ADAPTER_RENDER_ALLOWANCE_SECONDS", 1.0
    )

    _per_attempt = 1.4  # 3 × 1.4 = 4.2 s > old net 3.1 s, < new net 6.1 s

    class _SlowResult:
        """An ORDINARY exhausted result (no timeout) so the run finishes
        through the ordinary exhaustion path — the point is that the net
        must not fire first."""

        def __init__(self) -> None:
            self.status = "exhausted"
            self.failure_reason = "bbox_out_of_tolerance"
            self.attempts_started = 0
            self.attempt_latencies = None
            self.best = IterationRecord(
                iteration=3,
                scad_source="W = 20;\ncube([W, 20, 20]);\n",
                render=_default_render(),
                score=Score(bits=(True, True, True, True, False), rank=4, tiebreak=(True, True, True, True, False)),
                params={"W": 20.0},
            )

    class _SlowLoopFinal:
        def __call__(self, app=None, **kwargs):
            async def _slow():
                on_progress = kwargs.get("on_progress")
                for i in range(1, 4):
                    await asyncio.sleep(_per_attempt)
                    if on_progress is not None:
                        on_progress(
                            "view-done", {"view": "front", "iteration": i}
                        )
                return _SlowResult()

            return _slow()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _SlowLoopFinal()
        source = run_design_loop_with_events(
            app_with_versions,
            pid,
            user_message="hi",
            stated_dims=None,
            chat_history=(),
            photo="data:image/png;base64,x",
            request_text="hi",
        )
        t0 = _time.monotonic()
        frames = []
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        elapsed = _time.monotonic() - t0
        return frames, elapsed

    frames, elapsed = run_async(app_with_versions, _call)
    # The stub ran all three attempts (~4.2 s) — past the old 3.1 s net.
    assert elapsed >= 4.0, (
        f"run was cut short ({elapsed:.2f}s) — the net fired early"
    )
    # No timeout kill: the terminal frame is the ordinary exhaustion
    # error (or a pass/none-timeout shape) — never the safety net's
    # ``design_loop_timed_out`` frame.
    assert not any(
        f[1].get("reason") == DESIGN_LOOP_TIMED_OUT_REASON for f in frames
    ), f"adapter net killed a progressing run: {frames}"
    # The run finished within the new net (6.1 s) plus scheduling headroom.
    assert elapsed < 7.5, f"run exceeded the new net: {elapsed:.2f}s"
    assert frames, "no frames emitted"
    assert frames[-1][0] in ("done", "error"), f"no terminal frame: {frames}"