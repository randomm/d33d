"""The fill-and-recut pre-route's turn handler (issue #332 sub-issue 3).

:func:`fill_recut_turn` is the fill-and-recut pre-route's ONE entry point —
the turn-level logic that ``d33d.fill_recut`` re-exports (``fill_recut``
keeps the trigger, the boundary copy, the offer instruction, and the
module-level regexes/word sets; the heavier per-turn handler lives here so
the offer module stays under its size budget). ``d33d.projects.post_chat``
calls it (via the ``d33d.fill_recut.fill_recut_turn`` re-export) after the
missing-source check and BEFORE the #250 offer / question pre-routes.
"""

from __future__ import annotations

from typing import Any

from d33d.fill_recut import (
    FILL_RECUT_DECLINE_REPLY,
    FRILL_POINT_AT_NO_DIM_TEMPLATE,
    FRILL_POINT_AT_TEMPLATE,
    _fmt_size,
    boundary_sentence,
    fill_and_recut_instruction,
    fill_recut_trigger,
    is_clean_no,
    is_clean_yes,
    no_match_hole_reply,
    own_feature_names,
)
from d33d.hole_select import NoMatchHole, holes_in_mm, select_measured_hole
from d33d.part_holes import HOLE_NOUNS, no_hole_reply, part_has_hole_evidence
from d33d.part_units import USABLE_UNIT_STATUSES


def fill_recut_turn(
    app: Any, project_id: int, message: str
) -> dict[str, Any] | None:
    """The fill-and-recut pre-route's ONE entry point for ONE chat turn
    (issue #332's sub-issue 3). Called by ``d33d.projects.post_chat``
    AFTER the missing-source check and BEFORE the #250 offer / question
    pre-routes (the unsettled-part guard runs first, upstream).

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop": bool,
    "outcome": "fresh_offer" | "decline" | "accept" | "no_feature" | "no_match"}``
    when the turn is handled here (the caller registers the sentence as a
    ``kind: "answer"`` done frame — and, when ``run_loop`` is True, runs
    the design loop with the ``instruction`` field appended to the
    request text), else ``None`` (fall-through to the existing routes).

    The ``outcome`` discriminator is the SAME contract the region-edit
    seam's :func:`d33d.fill_recut_region.fill_recut_region_edit` returns:
    the caller keys OFF ``outcome`` — the done frame's ``fill_recut_offer``
    flag (the SPA's [Yes, do that] / [Leave it] buttons) is set ONLY for
    ``fresh_offer``; a clean ``decline`` re-emitting it would re-render
    the buttons for an offer that no longer exists.

    Handled cases: a LIVE fill-recut offer (``kind: "fill_recut"``
    pending, server-side state) — a clean "yes" clears the offer and
    runs the loop with the explicit fill-and-recut instruction; a clean
    "no" clears it and replies quietly; anything else supersedes the
    offer and re-evaluates this message as a fresh turn; and a fresh
    trigger on an assumed/settled part — the boundary sentence plus the
    offer recorded server-side (``kind: "fill_recut"``), or the honest
    no-hole reply for a hole-family noun with explicit
    ``hole_count == 0`` (issue #351).
    """
    from d33d.part_http import part_public

    row = app.state.conn.get_project(project_id)
    if row is None:
        return None
    part = part_public(row) if row.get("part_filename") else None
    if part is None or part.get("unit_status") not in USABLE_UNIT_STATUSES:
        return None

    versions = app.state.versions
    pending = versions.get_pending_offer(project_id)
    if pending is not None and pending.get("kind") == "fill_recut":
        # A LIVE fill-recut offer: "yes" clears the offer and runs the
        # loop with the explicit fill-and-recut instruction (the caller
        # registers the design loop and clears the offer once the event
        # source is up, restoring it on a setup failure — the acceptance
        # is never lost); "no" clears the offer and replies quietly
        # (nothing can fail after this point, so the clear is safe);
        # anything else supersedes the offer and re-evaluates this
        # message as a fresh turn.
        if is_clean_yes(message):
            return {
                "kind": "answer",
                "answer": None,
                "run_loop": True,
                "outcome": "accept",
                "instruction": fill_and_recut_instruction(pending),
                "accepted_offer": pending,
            }
        if is_clean_no(message):
            versions.set_pending_offer(project_id, None)
            return {
                "kind": "answer",
                "answer": FILL_RECUT_DECLINE_REPLY,
                "run_loop": False,
                "outcome": "decline",
            }
        # A new message supersedes the pending offer: clear it and
        # re-evaluate as a fresh turn.
        versions.set_pending_offer(project_id, None)
        pending = None

    if pending is None:
        trigger = fill_recut_trigger(message)
        if trigger is not None:
            own = own_feature_names(versions.latest_version(project_id))
            if trigger["noun"] not in own:
                # Issue #351 (operator decisions 1–4): a hole-family noun
                # is offered ONLY when the import stored hole evidence —
                # ``hole_count == 0`` → the honest no-hole reply (no
                # offer, no loop, no flag); ``None`` (unknown — no
                # report, legacy row, corrupt blob) keeps today's
                # behaviour. Other nouns keep current behaviour.
                if (
                    trigger["noun"] in HOLE_NOUNS
                    and part_has_hole_evidence(part) is False
                ):
                    return {
                        "kind": "answer",
                        "answer": no_hole_reply(trigger["noun"]),
                        "run_loop": False,
                        "outcome": "no_feature",
                    }

                # Issue #396: if the part has measured holes, select the
                # hole the user is referring to and store its geometry in
                # the offer so the instruction can carry the numbers.
                offer_dict: dict[str, Any] = {
                    "kind": "fill_recut",
                    "noun": trigger["noun"],
                    "size": trigger["size"],
                }
                sentence = boundary_sentence(
                    trigger["noun"],
                    trigger["size"],
                    move=trigger["move"],
                    move_distance_mm=trigger.get("move_distance"),
                    move_direction=trigger.get("direction"),
                )
                point_at_fallback = False
                no_match = False

                if trigger["noun"] in HOLE_NOUNS:
                    # Issue #396 (round 2): the stored holes are in FILE
                    # units; convert to mm via the part's scale BEFORE
                    # selection and instruction (the user's numbers are
                    # in mm — comparing file units against an mm trigger
                    # size and an mm bbox would pick the wrong hole).
                    report = part.get("report") if part else None
                    scale = part.get("scale")
                    holes = holes_in_mm(report, scale) if report else []
                    bbox_mm: list[float] | None = None
                    if isinstance(report, dict):
                        bbox_fu = report.get("bbox_file_units")
                        # Guard the scale and bbox arithmetic against
                        # non-numeric stored values (issue #396 lens
                        # fix): treat them as unavailable rather than
                        # letting a corrupt row 500 the chat route.
                        try:
                            scale_f = float(scale) if scale is not None else 0.0
                        except (TypeError, ValueError):
                            scale_f = 0.0
                        if (
                            isinstance(bbox_fu, (list, tuple))
                            and len(bbox_fu) >= 2
                            and scale_f > 0
                        ):
                            try:
                                bbox_mm = [
                                    float(bbox_fu[0]) * scale_f,
                                    float(bbox_fu[1]) * scale_f,
                                ]
                            except (TypeError, ValueError):
                                bbox_mm = None
                    if holes:
                        selected = select_measured_hole(
                            holes, message, bbox_mm,
                            trigger_size=trigger["size"],
                        )
                        if selected is None:
                            # Ambiguous or no qualifier matched: use the
                            # point-at copy (never an instruction without
                            # a location).
                            point_at_fallback = True
                        elif isinstance(selected, NoMatchHole):
                            no_match = True
                        else:
                            offer_dict["center"] = selected.get("center")
                            offer_dict["axis"] = selected.get("axis")
                            offer_dict["diameter_mm"] = selected.get("diameter_mm")

                if point_at_fallback:
                    dim_str = _fmt_size(trigger["size"])
                    if dim_str:
                        sentence = FRILL_POINT_AT_TEMPLATE.format(
                            noun=trigger["noun"], dim=dim_str
                        )
                    else:
                        sentence = FRILL_POINT_AT_NO_DIM_TEMPLATE.format(
                            noun=trigger["noun"]
                        )

                if no_match:
                    # Issue #414: the user named a hole by position but no
                    # measured hole matches — say so and list the measured
                    # holes (diameter + centre). No recut offer is made
                    # (nothing is stored server-side), so the done frame
                    # carries no offer flag (the ``outcome`` is not
                    # ``fresh_offer``) and no design loop runs.
                    sentence = no_match_hole_reply(trigger["noun"], holes)
                    return {
                        "kind": "answer",
                        "answer": sentence,
                        "run_loop": False,
                        "outcome": "no_match",
                    }

                versions.set_pending_offer(project_id, offer_dict)
                return {
                    "kind": "answer",
                    "answer": sentence,
                    "run_loop": False,
                    "outcome": "fresh_offer",
                }
    return None


__all__ = [
    "fill_recut_turn",
]
