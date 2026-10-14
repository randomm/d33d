"""Acknowledgement routing with NO live offer (issue #449).

The shared predicates the chat seam (``d33d.fill_recut_turn``, the #349
guard in ``d33d.projects``) and the region-edit seam
(``d33d.fill_recut_region``) both read, so the two routes partition the
same way:

* a LIVE offer at turn start (a fill-recut row, or a #250 row whose
  version is still the latest) keeps main's routing unchanged;
* with no live offer, a LITERAL yes on a MEASURED part gets point-first;
  every other bare acknowledgement gets the #349 no-offer notice;
* everything else flows to the design loop.
"""

from __future__ import annotations

from typing import Any

from d33d.confirm_offer import is_live_param_offer
from d33d.fill_recut import (
    FRILL_POINT_FIRST_REPLY,
    fill_recut_trigger,
    is_bare_affirmation,
    is_literal_yes,
)
from d33d.hole_select import part_has_measured_holes


def offer_is_live(pending: dict[str, Any] | None, latest: dict[str, Any] | None) -> bool:
    """True iff ``pending`` is a LIVE offer: a fill-recut row (always live
    for its lifetime), or a #250 param row whose version is the latest."""
    if pending is None:
        return False
    return pending.get("kind") == "fill_recut" or is_live_param_offer(pending, latest)


def point_first_applies(
    text: str, part: dict[str, Any] | None, *, live: bool
) -> bool:
    """The point-first word-and-state test (the caller checks the trigger).

    With a live offer at turn start the word test stays the bare-affirmation
    set (main's routing, unchanged); with none, only a literal yes counts.
    Either way the part must carry a measured hole list."""
    word = is_bare_affirmation(text) if live else is_literal_yes(text)
    return word and part_has_measured_holes(part)


def point_first_reply(
    versions: Any,
    project_id: int,
    text: str,
    part: dict[str, Any] | None,
    *,
    live: bool,
    trigger: dict[str, Any] | None,
) -> dict[str, Any] | None:
    """The point-first answer (no loop, no offer) when it applies; it clears
    a stale non-live pending row on both routes. ``None`` otherwise."""
    if trigger is not None or not point_first_applies(text, part, live=live):
        return None
    versions.set_pending_offer(project_id, None)
    return {
        "kind": "answer",
        "answer": FRILL_POINT_FIRST_REPLY,
        "run_loop": False,
        "outcome": "point_first",
    }


def no_offer_notice_applies(
    text: str,
    part: dict[str, Any] | None,
    pending: dict[str, Any] | None,
    latest: dict[str, Any] | None,
) -> bool:
    """The #349 no-offer notice: a bare affirmation with NO live offer, and
    NOT a literal yes on a measured part (that one is point-first)."""
    if offer_is_live(pending, latest):
        return False
    return is_bare_affirmation(text) and not (
        is_literal_yes(text) and part_has_measured_holes(part)
    )


def literal_yes_pre_unit_gate(
    app: Any, project_id: int, text: str, part: dict[str, Any] | None
) -> bool:
    """True iff a literal yes on a measured part with NO live offer must get
    point-first BEFORE the unsettled-unit gate (measured wins over unit
    status). The chat route checks this before its settle-first notice."""
    versions = app.state.versions
    if offer_is_live(
        versions.get_pending_offer(project_id), versions.latest_version(project_id)
    ):
        return False
    return (
        fill_recut_trigger(text) is None
        and is_literal_yes(text)
        and part_has_measured_holes(part)
    )


__all__ = [
    "literal_yes_pre_unit_gate",
    "no_offer_notice_applies",
    "offer_is_live",
    "point_first_applies",
    "point_first_reply",
]
