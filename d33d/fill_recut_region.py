"""The fill-and-recut pre-route for the REGION-EDIT seam (issue #338,
operator decisions 5–6).

``d33d.app``'s ``POST /api/projects/{id}/region-edits`` calls
:func:`region_edit_preroute` BEFORE the loop when the project has an
imported part whose units are assumed or settled — the SAME trigger the
chat route uses (``fill_recut.fill_recut_trigger``), evaluated with the
SAME caller-side rules. The function owns the whole pre-route outcome
lifecycle — the accept / decline / fresh-trigger / no-normal dispatch,
the event-source registration, the offer's restore-on-setup-failure,
and the in-flight flag's pre-registration release (the single release
point after registration is ``d33d.streaming``'s ``finally``) — so the
route body stays a few lines.

The module also hosts the pre-route DECISION
(:func:`fill_recut_region_edit`), the no-normal degradation copy, and
the face-normal length tolerance the wire validator reads. Everything
shared with the chat route stays in :mod:`d33d.fill_recut`.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from typing import Any, Literal, TypedDict

from fastapi.responses import JSONResponse

from d33d.chat_frames import answered_frames, register_event_source
from d33d.fill_recut import (
    FILL_RECUT_DECLINE_REPLY,
    FILL_RECUT_NO_HOLE_REPLY,
    HOLE_NOUNS,
    boundary_sentence,
    fill_and_recut_instruction,
    fill_recut_trigger,
    is_clean_no,
    is_clean_yes,
    own_feature_names,
    part_has_hole_evidence,
)

logger = logging.getLogger(__name__)

#: Issue #338 (operator decision 6) — the region-edit no-normal
#: degradation copy (issue #332's axis-dependent offer is impossible
#: without a face normal; the reply says plainly that the feature came
#: with the file and that the axis cannot be read from the pick). A
#: verbatim copy of the copy.ts sentence — the parity test pins the
#: two-way agreement (the #332 pin's pattern).
FILL_RECUT_NO_NORMAL_REPLY = (
    "That feature came with your file, so I can't resize it directly — "
    "the file has no parameters for me to change. I can't tell that "
    "feature's axis from where you pointed — pin a flat face on it and "
    "I'll offer to fill it and recut it on the same axis."
)

#: The pre-route's "handled the turn" decision shape (see
#: :func:`fill_recut_region_edit`). A discriminated union of the four
#: per-outcome shapes, keyed on ``outcome`` — each member is an honest
#: ``total=True`` TypedDict (every key of that outcome present, no
#: ``total=False`` holes the reader has to narrow around):
#
#: * ``accept`` — the clean-acceptance-of-a-LIVE-offer path: the reply is
#:   ``None`` (no plain reply), ``run_loop`` is ``True``, and
#:   ``instruction`` + ``accepted_offer`` are REQUIRED (the fill-and-recut
#:   instruction the caller prefixes to the request text; the pending offer
#:   the caller clears once the event source is registered, restored on a
#:   setup failure);
#: * ``decline`` / ``fresh_offer`` / ``no_normal`` — ONE ``answer`` frame
#:   each (``run_loop`` is ``False``); ``fresh_offer`` is the ONLY outcome
#:   whose done frame carries the ``fill_recut_offer`` flag (the SPA's
#:   [Yes, do that] / [Leave it] buttons) — a decline / no-normal reply must
#:   not re-render the buttons for an offer that does not (or no longer)
#:   exists.
class _FillAccept(TypedDict):
    kind: Literal["answer"]
    answer: None
    run_loop: bool
    outcome: Literal["accept"]
    instruction: str
    accepted_offer: dict[str, Any]


class _FillDecline(TypedDict):
    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["decline"]


class _FillFreshOffer(TypedDict):
    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["fresh_offer"]


class _FillNoNormal(TypedDict):
    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["no_normal"]


class _FillNoFeature(TypedDict):
    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["no_feature"]


#: The decision union: an ``accept`` member, or any of the four
#: answer-frame members (the caller dispatches on ``outcome``; only
#: ``fresh_offer`` surfaces the SPA's offer buttons — the decline /
#: no-normal / no-feature replies are plain answers, never an offer).
FillResult = (
    _FillAccept | _FillDecline | _FillFreshOffer | _FillNoNormal | _FillNoFeature
)


def fill_recut_region_edit(
    app: Any,
    project_id: int,
    instruction: str,
    face_normal: tuple[float, float, float] | None = None,
    row: dict[str, Any] | None = None,
    part: dict[str, Any] | None = None,
) -> FillResult | None:
    """The fill-and-recut pre-route DECISION for the REGION-EDIT seam
    (issue #338's operator decisions 5–6).

    The route (via :func:`region_edit_preroute`) calls this BEFORE the
    loop when the project has an imported part whose units are assumed
    or settled — the SAME trigger the chat route uses
    (``fill_recut_trigger`` over the instruction text), evaluated with
    the SAME caller-side rules: the part exists and is assumed/settled
    (a) and the noun is not a token of the design's own current params
    or labels (d).

    ``row`` / ``part`` are the route's ALREADY-FETCHED project row and
    ``part_public`` dict — passed in (the route fetched them for the
    404 and the unit-status decision) instead of re-fetched via
    ``get_project``; when ``row`` is ``None`` a defensive fetch runs
    (a route that would have 404'd never calls this).

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop":
    bool, "outcome": "accept" | "decline" | "fresh_offer" | "no_normal",
    ...}`` when the pre-route handles the turn (the caller
    registers the event source and, when ``run_loop`` is true, runs the
    design loop with the ``instruction`` field prefixed to the request
    text), else ``None`` (the caller proceeds to the design loop
    exactly as today).

    The handled cases mirror the chat route's:

    * a LIVE fill-recut offer (``kind: "fill_recut"``): a clean
      acceptance returns the explicit fill-and-recut instruction with
      ``run_loop: True`` and the offer the caller clears once the event
      source is registered (restored on a setup failure); a clean
      decline clears the offer and replies quietly; anything else
      supersedes the offer (cleared) and re-evaluates the instruction
      as a fresh trigger;
    * a fresh trigger with a face normal: the boundary sentence (the
      same ``boundary_sentence`` template) and the offer recorded
      server-side — the offer carries the pick's face normal as its
      ``axis`` so the accepted turn's instruction names the axis
      (operator decision 5: "…on the same axis");
    * a fresh trigger WITHOUT a face normal: the axis-dependent offer
      is impossible — the reply is :data:`FILL_RECUT_NO_NORMAL_REPLY`
      (the #338 operator decision 6 copy) and NO offer is stored, NO
      loop runs (operator decision 6: no offer, no buttons, no loop).
    """
    if row is None:
        row = app.state.conn.get_project(project_id)
        if row is None:
            return None
    if part is None:
        from d33d.part_http import part_public

        part = part_public(row) if row.get("part_filename") else None
    if part is None or part.get("unit_status") not in ("assumed", "settled"):
        return None

    versions = app.state.versions
    pending = versions.get_pending_offer(project_id)
    if pending is not None and pending.get("kind") == "fill_recut":
        return _handle_live_offer(versions, project_id, instruction, pending)

    trigger = fill_recut_trigger(instruction)
    if trigger is None or trigger["noun"] in own_feature_names(
        versions.latest_version(project_id)
    ):
        return None
    # Issue #351 (operator decision 3): the region-edit path reads the
    # SAME stored ``hole_count`` fact the chat route gates on — NO local
    # mesh geometry at the hit point (the pick can land on a plain face
    # too, and the stored import-time fact is the deterministic signal).
    # A hole-family noun with an explicit zero count gets the honest
    # no-hole reply (no offer, no loop — before the face-normal branch,
    # because the pick's normal is not evidence the hole exists); an
    # unknown count keeps today's behaviour (the fresh-offer / no-normal
    # dispatch, unchanged).
    if (
        trigger["noun"] in HOLE_NOUNS
        and part_has_hole_evidence(part) is False
    ):
        return {
            "kind": "answer",
            "answer": FILL_RECUT_NO_HOLE_REPLY,
            "run_loop": False,
            "outcome": "no_feature",
        }
    return _handle_fresh_trigger(versions, project_id, trigger, face_normal)


def _handle_live_offer(
    versions: Any,
    project_id: int,
    instruction: str,
    pending: dict[str, Any],
) -> FillResult | None:
    """A LIVE fill-recut offer (``kind: "fill_recut"``): a clean
    acceptance runs the loop with the fill-and-recut instruction; a
    clean decline clears the offer and replies quietly; anything else
    supersedes the offer (cleared) and falls through to the fresh
    trigger evaluation (this function returns ``None`` so the caller
    runs it). The ``pending`` offer is passed in (the caller already
    fetched it) so the reader is not called a second time.

    Deliberate twin of :func:`d33d.fill_recut.fill_recut_turn`'s matching
    branch — the chat seam's live-offer handling (accept / decline /
    supersede) — kept in lockstep by the parity tests."""
    if is_clean_yes(instruction):
        return {
            "kind": "answer",
            "answer": None,
            "run_loop": True,
            "outcome": "accept",
            "instruction": fill_and_recut_instruction(pending),
            "accepted_offer": pending,
        }
    if is_clean_no(instruction):
        versions.set_pending_offer(project_id, None)
        return {
            "kind": "answer",
            "answer": FILL_RECUT_DECLINE_REPLY,
            "run_loop": False,
            "outcome": "decline",
        }
    versions.set_pending_offer(project_id, None)
    return None


def _handle_fresh_trigger(
    versions: Any,
    project_id: int,
    trigger: dict[str, Any],
    face_normal: tuple[float, float, float] | None,
) -> FillResult:
    """A fresh fill-and-recut trigger: with a face normal the boundary
    sentence is the answer and the offer is recorded server-side (the
    offer carries the pick's normal as its ``axis`` — operator decision
    5); without one the axis-dependent offer is impossible — the
    no-normal degradation copy (operator decision 6), no offer, no
    loop."""
    if face_normal is None:
        return {
            "kind": "answer",
            "answer": FILL_RECUT_NO_NORMAL_REPLY,
            "run_loop": False,
            "outcome": "no_normal",
        }
    axis = tuple(float(v) for v in face_normal)
    versions.set_pending_offer(
        project_id,
        {
            "kind": "fill_recut",
            "noun": trigger["noun"],
            "size": trigger["size"],
            "axis": list(axis),
        },
    )
    sentence = boundary_sentence(
        trigger["noun"],
        trigger["size"],
        move=trigger["move"],
        move_distance_mm=trigger.get("move_distance"),
        move_direction=trigger.get("direction"),
    )
    return {"kind": "answer", "answer": sentence, "run_loop": False, "outcome": "fresh_offer"}


def region_edit_preroute(
    app: Any,
    project_id: int,
    *,
    row: dict[str, Any] | None,
    part: dict[str, Any] | None,
    instruction: str,
    face_normal: tuple[float, float, float] | None,
    start_loop: Callable[..., Any],
    loop_kwargs: dict[str, Any],
    inflight: set[int],
) -> JSONResponse | None:
    """The region-edit route's fill-and-recut pre-route, end to end
    (issue #338, operator decision 5).

    The route has ALREADY claimed the in-flight flag (the chat route's
    contract: claim before the pre-route). This function covers every
    outcome the route used to inline:

    * the unit-status gate lives in :func:`fill_recut_region_edit`
      (the single decision point) — a project without an imported part
      whose units are assumed/settled makes the decision return ``None``
      (no part, or the gate fails) and this function falls through to the
      design loop via that ``None``;
    * :func:`fill_recut_region_edit`'s decision — on ANY pre-route
      exception the flag (claimed by the caller) is released,
      ``logger.exception`` logs it, and the exception re-raises (the
      project is never left 409-blocked);
    * the accept path (:func:`_handle_acceptance`) — the loop runs
      with the fill-and-recut instruction; the offer is cleared once
      the event source is registered and RESTORED on a setup failure
      (the #332 "lost yes" pattern), with the flag released and the
      exception logged + re-raised;
    * the decline / fresh-trigger / no-normal path
      (:func:`_handle_answer`) — ONE ``answered_frames`` source, no
      loop, no version.

    After an event source is registered the stream's
    ``d33d.streaming`` ``finally`` is the single in-flight release
    point (as before); this function only releases on the pre-route's
    own no-source failure exits.
    """
    try:
        fill_result = fill_recut_region_edit(
            app,
            project_id,
            instruction,
            face_normal=face_normal,
            row=row,
            part=part,
        )
    except Exception:
        # Pre-route failure: the event source is NOT registered, so the
        # stream's finally never runs — release the flag (claimed by the
        # caller) and re-raise; without this guard the project would be
        # 409-blocked.
        inflight.discard(project_id)
        logger.exception(
            "region-edit for project %s: fill-and-recut pre-route failed — "
            "the in-flight flag is released",
            project_id,
        )
        raise
    if fill_result is None:
        return None
    if fill_result.get("run_loop"):
        return _handle_acceptance(
            app, project_id, fill_result, loop_kwargs, start_loop, inflight
        )
    return _handle_answer(app, project_id, fill_result)


def _handle_acceptance(
    app: Any,
    project_id: int,
    fill_result: FillResult,
    loop_kwargs: dict[str, Any],
    start_loop: Callable[..., Any],
    inflight: set[int],
) -> JSONResponse:
    """The clean-acceptance-of-a-LIVE-offer path (the #332 "lost yes"
    pattern): the loop runs with the fill-and-recut instruction
    prefixed to the request text; the event source is registered
    synchronously before the 202; the offer is cleared once the source
    is registered and RESTORED on a setup failure — the acceptance is
    never lost to a failed setup. On a setup failure the in-flight
    flag is released (the source was not registered, so the stream's
    ``finally`` never runs) and the exception is logged + re-raised.
    """
    # The outcome discriminator is the honest narrowing key: this path is
    # only reachable with ``outcome == "accept"`` (the caller dispatches on
    # it), which — under the discriminated-union type — makes
    # ``instruction`` and ``accepted_offer`` required members, no
    # dict-view dance and no cast needed.
    if fill_result["outcome"] != "accept":
        raise ValueError(
            f"region-edit acceptance path reached with outcome "
            f"{fill_result['outcome']!r}, not 'accept'"
        )
    accepted_offer = fill_result["accepted_offer"]
    instruction = fill_result["instruction"]
    request_text = f"{instruction} {loop_kwargs['request_text']}"
    loop_kwargs = dict(loop_kwargs, user_message=request_text, request_text=request_text)
    try:
        events = start_loop(app, project_id, **loop_kwargs)
    except Exception:
        # Discard in-flight FIRST (the event source was NOT registered,
        # so the stream's ``finally`` never runs — the flag must be
        # released here, not after the restore).
        inflight.discard(project_id)
        # Then restore the offer inside its OWN try/except: a restore
        # failure must not mask the ORIGINAL setup exception — it is
        # logged and swallowed, and the original re-raises below.
        try:
            app.state.versions.set_pending_offer(project_id, accepted_offer)
        except Exception:
            logger.exception(
                "region-edit for project %s: restoring the accepted "
                "fill-recut offer after a design-loop setup failure "
                "failed — the acceptance may be lost",
                project_id,
            )
        logger.exception(
            "region-edit for project %s: design-loop setup failed after a "
            "fill-recut offer acceptance — the in-flight flag is released "
            "and the offer restored",
            project_id,
        )
        raise
    register_event_source(app, project_id, events)
    # Best-effort: the source is registered, so the stream owns the
    # in-flight flag from here — a clear failure is logged and swallowed,
    # never re-raised (the loop already runs).
    try:
        app.state.versions.set_pending_offer(project_id, None)
    except Exception:
        logger.exception(
            "region-edit for project %s: clearing the accepted "
            "fill-recut offer after registration failed — the loop "
            "runs with the offer still pending",
            project_id,
        )
    # No inflight.discard here — the event source is registered, so the
    # stream's finally is the single release point.
    return JSONResponse(
        status_code=202,
        content={"project_id": project_id, "status": "accepted"},
    )


def _handle_answer(
    app: Any,
    project_id: int,
    fill_result: FillResult,
) -> JSONResponse:
    """The boundary-trigger / no-normal-degradation / clean-decline
    path: ONE answer frame, NO loop, NO version. The event source (an
    ``answered_frames`` generator) is registered synchronously; the
    in-flight flag is released when the stream's ``finally`` runs (once
    the generator is exhausted) — there is no explicit discard here.

    Only a FRESH boundary trigger (``outcome == "fresh_offer"``) surfaces
    the offer — the SPA renders the boundary sentence with the [Yes, do
    that] / [Leave it] buttons (issue #338, decision 7); the decline /
    no-normal replies are plain (no buttons). The outcome discriminator
    is the decision functions' own field — never a string comparison of
    the reply (the no-normal reply is not the decline reply, and it must
    not render offer buttons for an offer that was never stored).
    """
    is_fresh_trigger = fill_result.get("outcome") == "fresh_offer"
    register_event_source(
        app,
        project_id,
        answered_frames(fill_result["answer"], fill_recut_offer=is_fresh_trigger),
    )
    return JSONResponse(
        status_code=202,
        content={"project_id": project_id, "status": "accepted"},
    )


__all__ = [
    "FILL_RECUT_NO_NORMAL_REPLY",
    "fill_recut_region_edit",
    "region_edit_preroute",
]
