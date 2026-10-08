"""The design-loop adapter's timeout-archive and per-attempt-timing logic
(issue #417).

Extracted from ``d33d/design_loop_events.py`` to keep that module's net
growth small: the deadline-archive helper, the per-attempt latency
tracker, and the synthetic deadline-kill result all live here. The
adapter imports :func:`archive_deadline` and :class:`AttemptTracker` and
wires them into its wait loop.

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


class DeadlinedLoopResult:
    """A synthetic exhausted ``DesignResult`` for the deadline-kill
    archive (issue #396).

    A whole-loop timeout (the ``run_design_loop_with_events`` deadline
    cutting off a stalled loop) produces NO real loop result — the
    loop never returned, so the hook the app wires at the loop seam
    (``record_production_failure``) never sees it, and the timeout
    would never reach ``failures.jsonl``. This stub carries just what
    the hook reads (``status``, ``failure_reason``, ``best``) so the
    deadline path can archive the stall with the loop-level
    ``design_loop_timed_out`` class and the best available candidate
    text, never a fabricated full result.

    The reason is an explicit constructor argument (not a module-level
    constant) so this module does not import ``d33d.design_loop_events``
    (the adapter would import it back — a cycle); the adapter passes
    its own ``DESIGN_LOOP_TIMED_OUT_REASON``.
    """

    status = "exhausted"

    def __init__(self, scad: str = "", reason: str = "design_loop_timed_out") -> None:
        self.scad_source = scad
        self.failure_reason = reason

    @property
    def best(self) -> Any:
        # The hook reads ``best.scad_source`` for the archive row; a
        # stall has no candidate of its own, so the row carries the
        # last SCAD the loop's frames surfaced (``""`` when nothing
        # rendered).
        return self


def archive_deadline(
    app: Any,
    project_id: int,
    *,
    kwargs: dict[str, Any],
    photo: str,
    attempt_tracker: Any,
    deadline_reason: str,
    attempt_count: int = 0,
    latencies: list[float] | None = None,
) -> None:
    """Archive the deadline-kill row to ``failures.jsonl`` (issue #417).

    The adapter's safety-net deadline fired: the run stopped yielding
    frames before the loop's own per-attempt deadline could. The
    structured ``design_loop_timed_out`` row is written with the attempt
    count and per-attempt latencies (omit-not-null: a stall with no
    frames carries neither). The sink is read from ``app.state`` — a
    ``None`` sink (no ``failures_jsonl_path`` configured) means no
    archive (the timeout frame still fires).

    The row's fields come from the adapter's own context (passed in —
    the helper never reaches back into the adapter): ``request`` is the
    loop's ``request`` kwarg (a blank degrades to ``user_message``,
    then ``""``), the model id is resolved from the ``model`` kwarg the
    production closure injects for the hook (an object with a ``.model``
    field, or a bare string id) with ``app.state.model_id`` as fallback,
    and ``output_scad`` is the last SCAD the loop's frames surfaced
    (:class:`AttemptTracker.last_scad_source` — ``""`` when nothing
    rendered).

    A row with an unidentifiable model is not foldable — skipped (logged)
    rather than fabricated. The archive is best-effort: an exception
    here is logged and swallowed — the deadline frame is the
    user-visible guarantee.
    """
    sink = getattr(app.state, "failures_jsonl_path", None)
    if sink is None:
        return

    from d33d.evals.failure_capture import record_production_failure

    try:
        # The request text the loop was handed (the same ``request``
        # kwarg the hook archives for result-based failures); a blank
        # request degrades to the user's message.
        request = str(kwargs.get("request") or kwargs.get("user_message") or "")
        # The model id the app's production closure injects for the hook
        # (the ``model`` kwarg it pops before the real loop runs — the
        # hook archives it, the loop never sees it): a bare-string id, an
        # object with a ``.model`` field, or ``None``. Fallback:
        # ``app.state.model_id`` — a plain ``create_app`` without the
        # closure (a test stub) carries no ``model`` kwarg, so the
        # archive reads it from ``app.state`` instead.
        raw_model = kwargs.get("model")
        model_id = getattr(raw_model, "model", None)
        if not isinstance(model_id, str) or not model_id:
            model_id = (
                raw_model if isinstance(raw_model, str) and raw_model else None
            )
        if not model_id:
            model_id = getattr(app.state, "model_id", None)
        if not model_id:
            # A row with an unidentifiable model is not foldable — skip
            # rather than fabricate an id (the deadline frame still
            # fires).
            logger.warning(
                "failures.jsonl deadline archive skipped for project %s: "
                "no model id available",
                project_id,
            )
            return
        # The last SCAD source the loop's frames have surfaced (a token
        # frame, on a pass, or a progress frame carrying ``scad_source``)
        # — the stall has no result, so the best available candidate
        # text is ``""`` when nothing rendered.
        output_scad = attempt_tracker.last_scad_source
        record_production_failure(
            design_result=DeadlinedLoopResult(
                scad=output_scad, reason=deadline_reason
            ),
            photo=photo,
            region_mark=kwargs.get("region_mark"),
            request=request,
            model=model_id,
            prompt_version="",
            output_scad=output_scad,
            path=sink,
            attempt_count=attempt_count if attempt_count > 0 else None,
            per_attempt_latencies=list(latencies) if latencies else None,
        )
    except Exception:
        logger.exception(
            "failures.jsonl deadline archive failed for project %s; "
            "emitting the timeout frame anyway",
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
        # Bound the attempt count by the iteration cap (like the
        # ``_attempt_started`` check above): a frame carrying an index
        # above the cap is a protocol violation, not evidence of more
        # attempts — the count can never exceed what the loop may have
        # started.
        if (
            isinstance(_iter, int)
            and _iter > self._attempt_count
            and _iter <= self._max_iterations
        ):
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
