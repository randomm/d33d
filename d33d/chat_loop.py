"""The design-loop setup half of the chat route (issue #54's tail).

Extracted from ``d33d.projects.post_chat`` so the router file stays under
the 500-line split threshold (AGENTS.md): the pre-route chain (missing
source, unsettled-part + fill-recut, offer-acceptance, question-answer)
lives in ``d33d.projects.post_chat``; THIS module owns everything AFTER
the last pre-route — resolving the per-axis stated evidence (ticket #91 /
issue #247/#246/#261/#312), capturing the photo, and registering the
design-loop event source.

The module's ONE entry point is :func:`run_design_loop`, which
``d33d.projects.post_chat`` calls once every pre-route has fallen through.
It returns ``{"status": "accepted"}`` on success and ``None`` when it
registered an answer/terminal event source itself (the question pre-route
or the model-unconfigured path) — the caller returns the body verbatim.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from d33d.chat_frames import answered_frames as _answered_frames
from d33d.chat_frames import model_unconfigured_frames as _model_unconfigured_frames
from d33d.design_loop_events import (
    axes_to_gate_triple,
    photo_data_uri,
    run_design_loop_with_events,
)
from d33d.stated_carry import (
    carried_stated_set,
    effective_stated_dims,
    resolve_stated_cues,
)

logger = logging.getLogger(__name__)


async def run_design_loop(
    app: Any,
    project_id: int,
    row: dict[str, Any],
    message: str,
    stated_dims: list[float] | None,
    chat_history: tuple[str, ...],
    fill_recut_instruction: str | None,
) -> dict[str, Any] | None:
    """The design-loop setup for ONE chat turn (issue #54's tail).

    Runs AFTER every pre-route in ``d33d.projects.post_chat`` has fallen
    through (the missing-source / unsettled-part / fill-recut /
    offer-acceptance / question-answer routes are all upstream). Resolves
    the per-axis stated evidence, captures the photo, and registers the
    design-loop event source.

    Returns ``{"status": "accepted"}`` when the design-loop event source
    is registered (the 202 body), or ``None`` when an earlier terminal
    event source was already registered (the question pre-route's
    ``kind: "answer"`` done frame, or the model-unconfigured terminal
    error frame) — the caller returns the registered body verbatim.

    The in-flight flag contract: the flag is claimed BEFORE this call
    (in ``post_chat``); this function releases it ONLY on the pre-route
    setup-failure path (re-raised to the caller) and on the
    ``stated_dims``/loop-setup failure — every other path keeps the flag
    (the SSE endpoint's ``finally`` clears it when the stream drains).

    Issue #332 fix round: the caller's accepted fill-recut offer
    (``fill_recut_instruction is not None``) is NOT cleared until this
    function registers the design-loop event source — if the setup
    below raises, the caller restores the offer (the acceptance must
    not be lost to a setup failure).
    """
    inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
    if inflight is None:
        inflight = set()
        app.state.design_loop_inflight = inflight

    # Resolve the loop's stated dimensions (ticket #91; issue #247's
    # per-axis decision) — the SPA never sends ``stated_dims`` (it posts
    # only ``message`` + ``chat_history``): the loop receives the CURRENT
    # run's per-axis confirmed set (the body's explicit ``stated_dims``
    # axes when a client sends one, else the protocol's per-axis
    # extraction of the message). NO persisted fallback: a follow-up
    # message with no explicit dimension cue confirms nothing and the
    # gate ABSTAINS (``Score.bbox_abstained``) — it must not enforce an
    # axis confirmed on an earlier turn against a candidate the user just
    # asked to change. A PARTIAL confirmed set is a zero-filled (W, D, H)
    # triple — unconfirmed axes render as ``not specified`` in the prompt
    # and abstain per-axis in the bbox gate; ``None`` (abstain entirely)
    # when the current turn confirmed no axis. This route never reads
    # W/D/H param keys and never reads a persisted version row for the
    # gate.

    # Issue #249 — the pre-route (BEFORE the design loop): if the message
    # is a question AND the project's design state can answer it, the
    # answer is emitted on the chat stream as a single terminal done
    # frame (``kind: "answer"`` — the additive discriminator; no token
    # frames, no version-created frame, no version). Everything else —
    # including anything ambiguous — goes to the design loop EXACTLY as
    # today. The route is narrow on purpose (one stage-1 question
    # detector, one cheap stage-2 LLM call with a deterministic number
    # guard, a per-LLM-call hard timeout): the common case ("make it
    # taller") costs nothing.
    from d33d.question_answer import (
        ModelUnconfiguredError,
        route_chat_message,
    )

    latest = app.state.versions.latest_version(project_id)
    # The imported part's measured holes in MM (issue #396) — the
    # ``part_report["holes"]`` list ``[{center, axis, diameter_mm}]``
    # is stored at import in FILE units; :func:`d33d.hole_select.holes_in_mm`
    # applies the part's scale (the ONE conversion — the deterministic
    # stage reports in mm, the user's unit). ``None`` (no part, legacy
    # report, or no holes measured) keeps the pre-route's honest-reply
    # path; a list lets the deterministic stage answer a one-hole size
    # question from the measurement.
    holes = None
    if latest is not None:
        from d33d.hole_select import holes_in_mm
        from d33d.part_http import part_public

        public = part_public(row)
        if public is not None:
            report = public["report"]
            if isinstance(report, dict):
                mm_holes = holes_in_mm(report, public.get("scale"))
                if mm_holes:
                    holes = mm_holes
    try:
        answer_route = await route_chat_message(
            message,
            latest,
            answer_edge=getattr(app.state, "answer_question", None),
            project_id=str(project_id),
            part_unit_status=row.get("part_unit_status"),
            holes=holes,
        )
    except ModelUnconfiguredError as e:
        # The model pre-flight (issue #303) found the model cannot be
        # called: the question path emits the SAME structured terminal
        # error frame the design loop emits (reason
        # ``model_unconfigured``) — never the COULD_NOT_ANSWER text,
        # never a silent degrade into an LLM call. No version is
        # created (the design loop never runs).
        app.state.event_sources[project_id] = _model_unconfigured_frames(e.env_var)
        return {"status": "accepted"}
    except Exception:
        # The pre-route is best-effort but its failure is fatal to THIS
        # request (re-raised below): release the claim so the next
        # attempt can start clean, and log the failure (the request
        # errors — there is no design-loop fallback for a pre-route
        # crash). The warning carries lengths only (no message text — no
        # PII in logs).
        logger.warning(
            "question-answer pre-route failed; the request errors "
            "(inflight flag released, len(message)=%d)",
            len(message),
            exc_info=True,
        )
        inflight.discard(project_id)
        raise
    if answer_route is not None:
        # The event source is registered — the flag stays set
        # (streaming.py's ``finally`` clears it when the stream is
        # drained; no event source means no stream to drain it).
        app.state.event_sources[project_id] = _answered_frames(
            answer_route["answer"]
        )
        return {"status": "accepted"}

    # The per-axis stated evidence (issue #246/#261/#369) — the SINGLE
    # value the carry-forward merge helper (``effective_stated_dims``)
    # feeds BOTH the gate and the new version row. Cue precedence and
    # release semantics are documented in ``effective_stated_dims``.
    # A statement that yields no axis is ``{}`` → the version row persists
    # NULL (abstain, never a fabricated axis row).
    explicit_body: dict[str, float] | None = None
    if stated_dims is not None:
        _w, _d, _h = stated_dims
        # ``> 0`` (never truthiness): 0 is the unconfirmed marker
        explicit_body = {
            axis: float(value)
            for axis, value in zip(("W", "D", "H"), (_w, _d, _h))
            if value > 0
        } or None
    _carried = carried_stated_set(app.state.conn, app.state.versions, project_id)
    if explicit_body is not None:
        per_axis_stated = effective_stated_dims(_carried, explicit_body)
    else:
        # Issue #369: the message's protocol cues are extracted with an
        # EMPTY history (``[]``) — deliberate: the carried set already
        # holds earlier turns, and with the echoed history a relative
        # message would restate the carried value the user is releasing,
        # and the merge treats any extracted axis as an absolute override,
        # masking the release. The finalize route (``versions_routes``)
        # passes full history to the same pipeline; the two call sites
        # differ by design, not by pipeline. An EMPTY extraction falls
        # back to the lexicon classification, which carries the
        # RELATIVE/RELEASE semantics the dict shape cannot express.
        per_axis_stated = resolve_stated_cues(
            _carried, message, label="chat", project_id=project_id
        )

    stated = axes_to_gate_triple(per_axis_stated)

    # The project-level carried set (issue #312): written at the end of
    # every chat turn (pass or fail — the loop runs asynchronously as an
    # SSE stream; the write is here because the effective set is final
    # once cues are resolved, regardless of the loop's outcome). A
    # failed turn creates no version row, but the user's stated axes
    # must survive into the next successful version's gate input.
    try:
        app.state.conn.raw.execute(
            "UPDATE projects SET carried_stated_dims = ? WHERE id = ?",
            (json.dumps(per_axis_stated) if per_axis_stated else None, project_id),
        )
        app.state.conn.commit()
    except Exception:  # the write must never fail the 202
        logger.debug("carried_stated_dims write failed for project %s", project_id, exc_info=True)

    # Photo: read the project's stored photo NOW (synchronously, before
    # the 202 response) — the background task runs via asyncio and the
    # DB may be closed by the time the loop starts (a deleted project
    # or a closed connection). The photo is captured here as a data URI
    # (MIME from the extension; missing file → the fixed 1x1
    # transparent-PNG constant).
    photo = photo_data_uri(row.get("source_photo_path"))

    # Register the event source synchronously BEFORE the 202 response
    # (else the client stream terminates on "no active stream" — see
    # d33d/streaming.py's contract). The adapter is a plain async
    # generator (not a coroutine): ``event_sources`` maps
    # project_id -> AsyncIterator of (event, data) tuples.
    #
    # The SSE endpoint (GET /api/stream/{project_id}) is the SOLE
    # driver of this generator — a single async generator cannot be
    # driven by two concurrent ``async for`` consumers (CPython raises
    # ``RuntimeError: anext(): asynchronous generator is already
    # running`` on the second consumer's first ``__anext__``). The
    # inflight flag is set here (synchronously, before the 202
    # response) and cleared in the SSE endpoint's ``finally`` when the
    # generator is exhausted (or an SSE client disconnects).
    # Issue #332 (sub-issue 3) — the accepted fill-and-recut offer's
    # explicit instruction rides the request text (the ``request``
    # kwarg the adapter's prompt renders as the "Request:" line — the
    # loop's own import-aware prompt (``_design_system`` with
    # ``part_scale``) already teaches the fill-then-cut move from the
    # settled import; the instruction makes the accepted turn's intent
    # explicit to the model). ``user_message`` stays the user's own
    # words (the transcript field) — only the request text gains the
    # appended instruction.
    _request_text = message
    if fill_recut_instruction is not None:
        _request_text = f"{message}\n{fill_recut_instruction}"
    try:
        events = run_design_loop_with_events(
            app,
            project_id,
            user_message=message,
            stated_dims=stated,
            stated_axes=per_axis_stated,
            chat_history=chat_history,
            photo=photo,
            request_text=_request_text,
        )
    except Exception:
        # Design-loop setup failed BEFORE an event source was
        # registered: release the claim so the project is not stuck
        # (the flag was claimed before the pre-route — see above).
        inflight.discard(project_id)
        raise
    app.state.event_sources[project_id] = events
    # The fill-recut offer (cleared by the caller for the accepted turn
    # BEFORE this call) is now CONSUMED: the event source registered
    # successfully, so the acceptance is no longer at risk.
    if fill_recut_instruction is not None:
        app.state.versions.set_pending_offer(project_id, None)
    # The flag stays set — the SSE endpoint's ``finally`` clears it
    # on ALL exit paths (generator exhausted, client disconnect,
    # exception).

    return {"status": "accepted"}


__all__ = ["run_design_loop"]
