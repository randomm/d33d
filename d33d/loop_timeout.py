"""The design-loop adapter's timeout-archive and per-attempt-timing logic
(issue #417).

Extracted from ``d33d/design_loop_events.py`` to keep that module's net
growth small: the deadline-archive helper, the per-attempt latency
tracker, and the safety-net outcome builder all live here. The adapter
imports :func:`archive_deadline` and :class:`AttemptTracker` and wires
them into its wait loop.

The adapter's safety net is a GENEROUS outer deadline (per-attempt budget
× iteration cap + margin) that fires only for a run that stops yielding
frames before the loop's own per-attempt deadline can. A slow model is
handled by the loop, which returns its best-so-far result through the
ordinary exhaustion path; the safety net never versions a candidate —
it only archives and reports.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def archive_deadline(
    app: Any,
    project_id: int,
    user_message: str,
    model: str,
    _last_scad_source: str,
    attempt_count: int,
    latencies: list[float],
) -> None:
    """Archive the deadline-kill row to ``failures.jsonl`` (issue #417).

    The adapter's safety-net deadline fired: the run stopped yielding
    frames before the loop's own per-attempt deadline could. The
    structured ``design_loop_timed_out`` row is written with the attempt
    count and per-attempt latencies (omit-not-null: a stall with no
    frames carries neither). The sink is read from ``app.state`` — a
    ``None`` sink (no ``failures_jsonl_path`` configured) means no
    archive (the timeout frame still fires).

    The archive is best-effort: an exception here is logged and
    swallowed — the deadline frame is the user-visible guarantee.
    """
    sink = getattr(app.state, "failures_jsonl_path", None)
    if sink is None:
        return
    from d33d.evals.failure_capture import (
        _DeadlinedLoopResult,
        record_production_failure,
    )

    try:
        record_production_failure(
            design_result=_DeadlinedLoopResult(_last_scad_source),
            photo=None,
            region_mark=None,
            request=user_message,
            model=model,
            prompt_version="",
            output_scad=_last_scad_source,
            path=sink,
            attempt_count=attempt_count if attempt_count > 0 else None,
            per_attempt_latencies=list(latencies) if latencies else None,
        )
    except Exception:
        logger.exception(
            "deadline archive failed for project %s — the timeout "
            "frame still fires",
            project_id,
        )


class AttemptTracker:
    """Tracks per-attempt first-frame timestamps and the running attempt
    count (issue #417).

    The loop stamps the 1-based iteration index on its per-view markers
    (issue #121's payload contract), so each attempt is timed from its
    first frame to the next attempt's first frame (or the deadline for
    the in-flight attempt). The timeout copy's "about N s an attempt"
    and the archive row's per-attempt latencies are MEASURED values,
    never fabricated.

    Created with a reference to the event loop (for ``time()``); call
    :meth:`observe` from every frame-pump branch so the three paths can
    never drift.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop, max_iterations: int) -> None:
        self._loop = loop
        self._max_iterations = max_iterations
        self._attempt_started: dict[int, float] = {}
        self._attempt_count = 0
        self._last_scad_source = ""

    def observe(self, frame: tuple[str, dict[str, Any]]) -> None:
        """Record the frame's ``scad_source`` (if any) and the
        per-attempt first-frame timestamp (if the iteration index is
        new)."""
        _scad = frame[1].get("scad_source")
        if isinstance(_scad, str) and _scad:
            self._last_scad_source = _scad
        _iter = frame[1].get("iteration")
        if (
            isinstance(_iter, int)
            and 1 <= _iter <= self._max_iterations
            and _iter not in self._attempt_started
        ):
            self._attempt_started[_iter] = self._loop.time()
        if isinstance(_iter, int) and _iter > self._attempt_count:
            self._attempt_count = _iter

    @property
    def attempt_count(self) -> int:
        return self._attempt_count

    @property
    def last_scad_source(self) -> str:
        return self._last_scad_source

    def latencies(self) -> list[float]:
        """Measured seconds for attempts 1..``attempt_count``.

        Each elapsed value is the gap between that attempt's first frame
        and the next attempt's first frame (or now for the in-flight
        attempt); an attempt with no frames has no established time and
        is omitted (honest absence — the copy then falls back to the
        per-attempt budget).
        """
        latencies: list[float] = []
        _now = self._loop.time()
        for idx in range(1, self._attempt_count + 1):
            start = self._attempt_started.get(idx)
            if start is None:
                continue
            end = self._attempt_started.get(idx + 1)
            latencies.append(max(0.0, (end if end is not None else _now) - start))
        return latencies
