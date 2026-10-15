"""Acknowledgement routing with NO live offer (issue #449).

:func:`acknowledgement_route` is the ONE decision for the #449 precedence.
The chat seam (``d33d.fill_recut_turn``, the #349 guard and the unsettled
gate in ``d33d.projects``) and the region-edit seam
(``d33d.fill_recut_region``) each call it and perform their OWN side
effects on the outcome. Seam-specific ordering that is intentional (the
unsettled-unit asymmetry, the turn-start ``live``, the region's
live-#250 bypass) stays at the seam as an explicit, commented line.

* a resize/move trigger is never an acknowledgement (``None``);
* a LIVE offer at turn start: a bare affirmation on a MEASURED part is
  point-first; nothing else is an acknowledgement (the no-offer notice is
  suppressed by a live offer);
* with no live offer: a LITERAL yes on a MEASURED part is point-first;
  every other bare acknowledgement gets the #349 no-offer notice;
* everything else (``None``) flows to the design loop.
"""

from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict

from d33d.confirm_offer import is_live_param_offer
from d33d.fill_recut import (
    FRILL_POINT_FIRST_REPLY,
    is_bare_affirmation,
    is_literal_yes,
)
from d33d.hole_select import part_has_measured_holes

AcknowledgementRoute = Literal["point_first", "no_offer"]


class PendingOfferStore(Protocol):
    """The one versions-store method the point-first answer writes to."""

    def set_pending_offer(self, project_id: int, offer: dict[str, Any] | None) -> None: ...


class PointFirstAnswer(TypedDict):
    """The point-first answer: one ``answer`` frame, no loop, no offer."""

    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["point_first"]


class NoOfferAnswer(TypedDict):
    """The #349 no-offer notice: one ``answer`` frame, no loop, no offer."""

    kind: Literal["answer"]
    answer: str
    run_loop: bool
    outcome: Literal["no_offer"]


def offer_is_live(pending: dict[str, Any] | None, latest: dict[str, Any] | None) -> bool:
    """True iff ``pending`` is a LIVE offer: a fill-recut row (always live
    for its lifetime), or a #250 param row whose version is the latest."""
    if pending is None:
        return False
    return pending.get("kind") == "fill_recut" or is_live_param_offer(pending, latest)


def acknowledgement_route(
    text: str,
    part: dict[str, Any] | None,
    *,
    live: bool,
    trigger: dict[str, Any] | None,
) -> AcknowledgementRoute | None:
    """The #449 acknowledgement precedence (pure; the seams act on it).

    ``live`` is the offer state at turn start; ``trigger`` is
    ``fill_recut_trigger(text)``. Returns ``"point_first"``,
    ``"no_offer"``, or ``None`` (the turn is not an acknowledgement).
    """
    if trigger is not None:
        return None
    measured = part_has_measured_holes(part)
    if live:
        return "point_first" if is_bare_affirmation(text) and measured else None
    if is_literal_yes(text) and measured:
        return "point_first"
    if is_bare_affirmation(text):
        return "no_offer"
    return None


def clear_and_point_first_answer(
    versions: PendingOfferStore, project_id: int
) -> PointFirstAnswer:
    """Build the point-first answer. SIDE EFFECT: clears the project's pending
    row first (``set_pending_offer(project_id, None)``); both seams do so when
    point-first applies, so the row is never left behind."""
    versions.set_pending_offer(project_id, None)
    return {
        "kind": "answer",
        "answer": FRILL_POINT_FIRST_REPLY,
        "run_loop": False,
        "outcome": "point_first",
    }



__all__ = [
    "AcknowledgementRoute",
    "NoOfferAnswer",
    "PointFirstAnswer",
    "acknowledgement_route",
    "clear_and_point_first_answer",
    "offer_is_live",
]
