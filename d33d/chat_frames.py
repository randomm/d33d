"""The chat-route's answer / terminal SSE frame builders (issue #332 fix
round — the extraction-cycle removal).

The two small async generators ``d33d.projects.post_chat`` and
``d33d.chat_loop.run_design_loop`` both build (the #249 answer path's
one-terminal ``done`` frame and the #303 model-unconfigured terminal
``error`` frame). They lived in ``d33d.projects`` and ``d33d.chat_loop``
imported them lazily (inside ``run_design_loop``) to dodge the
``projects → chat_loop → projects`` module-load cycle; the fill-recut
extraction made that dance load-bearing. This module is the single
owner: ``projects`` and ``chat_loop`` import the two builders at module
level (``fill_recut``'s answer frames are built by the caller through
the same ``projects._answered_frames`` symbol, which now re-exports
them from here).
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "answered_frames",
    "model_unconfigured_frames",
    "register_event_source",
]


def register_event_source(app: Any, project_id: int, source: Any) -> None:
    """Register ``source`` as the project's SSE event source, logging a
    warning (issue #338) when a STALE source is already registered.

    The design loop, the answer path, and the region-edit pre-route all
    install a source via ``app.state.event_sources[project_id] = ...``.
    An unconditional overwrite silently drops a previous, still-live
    source (e.g. a loop that never drained before a fresh accept). The
    helper centralises that write so the dropped-source case is at least
    LOGGED (the operator decision — the single release point is still the
    SSE endpoint's ``finally``; this only surfaces the overlap).
    """
    event_sources = app.state.event_sources
    previous = event_sources.pop(project_id, None)
    if previous is not None:
        logger.warning(
            "register_event_source for project %s: a previous event "
            "source was already registered — dropping it in favour of the "
            "new source (the prior stream may have been left undrained)",
            project_id,
        )
    event_sources[project_id] = source


async def model_unconfigured_frames(env_var: str | None = None):
    """The model-unconfigured terminal frame (issue #303): ONE ``error``
    frame — the SAME structured shape the design loop's terminal error
    frame carries (``reason: model_unconfigured`` + ``env_var`` when known;
    never the key value). ``error``, not ``done``: a ``done`` frame would
    render as a plain assistant message, not a failure turn.
    """
    from d33d.design_loop import MODEL_UNCONFIGURED

    error_data: dict[str, Any] = {
        "message": f"Design loop exhausted: {MODEL_UNCONFIGURED}",
        "reason": MODEL_UNCONFIGURED,
    }
    if env_var is not None:
        error_data["env_var"] = env_var
    yield ("error", error_data)
    # The in-flight flag is released by ``d33d.streaming._stream_events``
    # (the SSE endpoint's ``finally`` — the single release point for every
    # event source, on every exit path), exactly as for
    # ``answered_frames``.


async def answered_frames(
    answer: str,
    project_id: int | None = None,
    app: Any = None,
    confirm_ack: dict[str, str] | None = None,
    fill_recut_offer: bool = False,
):
    """The answer-path SSE stream (issue #249): ONE terminal ``done``
    frame whose ``message`` is the answer text and which carries the
    additive ``kind: "answer"`` discriminator (design-loop done frames
    carry no ``kind`` at all — existing frames are byte-identical).

    ``confirm_ack`` (issue #250, the accepted-offer flow only) adds the
    acknowledgement's ``label``/``value`` as ADDITIVE ``confirm_ack_*``
    fields on the same done frame — the SPA's ``App.tsx`` renders the
    value in the mono face (a measurement must never hide inside a
    sentence). The question-answer path passes ``None`` (no ``confirm_*``
    keys, byte-identical).

    ``fill_recut_offer`` (issue #338, decision 7): when True, the done
    frame carries the additive ``fill_recut_offer`` field — the SPA's
    ``App.tsx`` renders the boundary sentence with the [Yes, do that] /
    [Leave it] buttons (the offer is stored server-side as the pending
    offer; the buttons send the acceptance/decline through the existing
    chat offer path). Default False (byte-identical for non-offer frames).

    No token frames, no version-created progress frame: the answer text
    is delivered exclusively in the done frame's ``message`` (the
    operator's decision — token frames feed the model-source view, and
    the SPA renders an ``kind: "answer"`` done frame's ``message``
    verbatim as a plain assistant chat message)."""
    done_data: dict[str, Any] = {"message": answer, "kind": "answer"}
    if confirm_ack is not None:
        done_data["confirm_ack"] = True
        done_data["confirm_ack_label"] = confirm_ack["label"]
        done_data["confirm_ack_value"] = confirm_ack["value"]
    if fill_recut_offer:
        done_data["fill_recut_offer"] = True
    yield ("done", done_data)
    # The in-flight flag is released by the STREAM's ``finally``
    # (``d33d.streaming._stream_events`` — the single release point for
    # every event source, on every exit path: the SSE endpoint drains in
    # production; tests that drive the source directly exhaust the same
    # generator via the SSE endpoint's ``_stream_events``). There is
    # deliberately no post-yield discard here: a release would have to
    # happen in generator ``finally`` code (not after the last ``yield`` —
    # that runs only if the consumer exhausts the generator), and
    # ``_stream_events``'s ``finally`` already covers every drain path.
    # A bare ``async for`` over the raw source (bypassing the SSE
    # endpoint) leaves the flag set by design — the contract is that the
    # stream endpoint is the sole driver of event sources (its
    # ``finally`` is the single release point).
