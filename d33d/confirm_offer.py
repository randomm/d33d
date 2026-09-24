"""The assumed-value offer (issue #250): pick, sentence, accept.

After a passing design pass, the assistant offers to confirm ONE assumed
value — the one that most affects fit — as a single plain sentence in the
conversation ("I assumed 3 mm walls. … Want it thinner?"), never a form.
A short affirmative reply records the value as user-confirmed (``stated``
in the design state, rule (b) of issue #250's operator decision) without
a new design run.

This module owns the pure decision logic; the wiring (the version
row's ``confirmed_params`` write, the project row's ``pending_offer``
state, the done-frame field, the ``post_chat`` acceptance pre-route)
lives in ``d33d.design_loop_events`` / ``d33d.projects``.

The offer is a PARAMETER (a model-emitted value the user never set), not
an axis: DECISIONS.md's own example offer is a wall thickness, which has
no axis. Selection, in strict precedence (AT MOST ONE param per turn):

1. an assumed numeric param with a model-DECLARED axis (issue #248's
   ``param_meta`` — the param most obviously tied to overall fit), in
   declaration order (first wins);
2. else the param the model flagged as most fit-critical: the reply's
   ``confirm_first`` field, validated to be an assumed numeric param of
   THIS version (a name that is not, or a name already confirmed or
   changed, falls through to no offer — never a re-offer);
3. else no offer.

Never re-offered: a param in the version's ``confirmed_params`` (already
confirmed) or a param whose value the user changed via the design loop
(the previous version carried it with a different value, or the param is
new to this version) is never offered for that project — the offer is an
invite to confirm what the model picked, and a value the user already
drove has been answered.

The model MAY supply the sentence (``confirm_sentence``); it is accepted
only if it names the chosen param's value AND every number in it appears
in the design-state block (the same deterministic number guard as
issue #249 — ``d33d.question_answer.guard_answer_numbers``), else the
deterministic template — ``copy.confirmOffer.offer`` in the SPA's
``copy.ts`` — renders the sentence: "I assumed {value} for {label}. Want
it different?" (the label per #248, identifier fallback). The
acknowledgement on acceptance: "Got it — {label} stays {value}."
(``copy.confirmOffer.acknowledged``).

Import graph (acyclic by construction): this module is a LEAF of the
design-state chain — ``design_state`` imports
``CONFIRMED_VALUE_TOLERANCE`` from here (rule (b) promotion), and this
module lazily imports ``d33d.design_state`` / ``d33d.question_answer`` /
``d33d.dimension_protocol`` inside functions. Do NOT add a module-level
import of ``d33d.design_state`` or ``d33d.question_answer`` here.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

#: The value-equality tolerance for rule (b) promotion and the offer's
#: "still current" checks (issue #250's operator decision: 1e-6 — a
#: confirmed value that later differs from the param's current value
#: does not count as evidence). Defined here (a leaf module of the
#: ``design_state`` ← ``confirm_offer`` import chain) so
#: ``d33d.design_state`` can import it without a cycle.
CONFIRMED_VALUE_TOLERANCE = 1e-6

__all__ = [
    "CONFIRMED_VALUE_TOLERANCE",
    "ack_sentence",
    "format_param_value",
    "is_pending_offer_acceptance",
    "offer_entry",
    "offer_sentence",
    "select_offer_candidate",
    "validate_confirm_first",
]

def _is_number(value: Any) -> bool:
    """True for int/float (bool is excluded — it is not a measurement)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def format_param_value(value: Any) -> str:
    """A param value rendered for copy (``12.0`` → ``"12"``, non-numeric
    verbatim — never a fabricated unit).

    The SHARED value formatter (public — ``d33d.projects``'s offer-ack
    field uses it instead of its own copy of the same bool/number/other
    ternary), used by :func:`offer_sentence` and :func:`ack_sentence`.
    """
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:g}"
    return str(value)


def _assumed_numeric_params(
    params: dict[str, Any],
    param_meta: dict[str, Any] | None,
    confirmed: dict[str, Any] | None,
    excluded: set[str],
) -> list[dict[str, Any]]:
    """The version's assumed numeric params eligible for an offer: a
    numeric (non-bool, non-zero) value, NOT in ``confirmed_params`` (rule
    (b) evidence already recorded), NOT in ``excluded`` (the user's own
    change set), with the entry's design-state provenance ``assumed`` (a
    param already ``stated`` via the axis rule or ``measured`` is not an
    offer — it is not an assumption anymore).

    ``excluded`` is the set of param names the user changed via the
    design loop (previous version's snapshot: values that differ, plus
    params newly introduced this version — those are not "assumed in
    place" values the model is inviting confirmation of; they are the
    user's own moves)."""
    from d33d.design_state import state_block_from_params

    confirmed = confirmed or {}
    changed = {name for name, value in params.items() if name in excluded}
    changed.update(excluded)
    entries = state_block_from_params(params, param_meta)
    by_name = {e["name"]: e for e in entries}
    out: list[dict[str, Any]] = []
    for name, value in params.items():
        if name in changed or name in confirmed:
            continue
        if not _is_number(value) or value == 0:
            continue
        entry = by_name.get(name)
        if entry is None or entry.get("provenance") != "assumed":
            continue
        out.append(entry)
    return out


def select_offer_candidate(
    params: dict[str, Any],
    param_meta: dict[str, Any] | None,
    confirmed: dict[str, Any] | None,
    changed: set[str] | tuple[str, ...],
    confirm_first: str | None,
) -> str | None:
    """The ONE assumed param to offer for this version, or ``None``.

    Precedence (module docstring): a model-declared-axis assumed numeric
    param first (declaration order), else the validated ``confirm_first``
    (the model's fit-critical flag — rejected when it names a
    non-assumed, non-numeric, already-confirmed, or user-changed param,
    in which case the offer is ABSENT rather than a silent second
    fallback — the model flagged a value that is no longer an open
    question, and a wrong second guess would be worse than none), else
    none.
    """
    changed_set = set(changed or ())
    eligible = _assumed_numeric_params(params, param_meta, confirmed, changed_set)
    if not eligible:
        return None
    # Rule 1: a declared-axis assumed param (declaration order — the
    # first in the params snapshot).
    for entry in eligible:
        if entry.get("axis"):
            return entry["name"]
    # Rule 2: the model's own flag, validated.
    eligible_names = {e["name"] for e in eligible}
    if confirm_first in eligible_names:
        return str(confirm_first)
    return None


def validate_confirm_first(
    confirm_first: str | None,
    params: dict[str, Any],
    param_meta: dict[str, Any] | None,
    confirmed: dict[str, Any] | None,
    changed: set[str] | tuple[str, ...],
) -> bool:
    """``confirm_first`` is valid iff it names an assumed numeric param of
    THIS version (in the eligible set — declared, non-zero numeric
    value, not confirmed, not user-changed)."""
    if not isinstance(confirm_first, str) or not confirm_first:
        return False
    changed_set = set(changed or ())
    eligible = _assumed_numeric_params(params, param_meta, confirmed, changed_set)
    return any(e["name"] == confirm_first for e in eligible)


def offer_entry(
    params: dict[str, Any],
    param_meta: dict[str, Any] | None,
    name: str,
) -> dict[str, Any] | None:
    """The chosen param's design-state entry (label + value from the
    shared block builder), or ``None`` when the name is not a declared
    param (a guard — the caller should never reach here with a bad name)."""
    from d33d.design_state import state_block_from_params

    for entry in state_block_from_params(params, param_meta):
        if entry["name"] == name:
            return entry
    return None


def offer_sentence(
    entry: dict[str, Any],
    sentence: str | None,
    block_entries: list[dict[str, Any]],
) -> str:
    """The offer sentence: the model's ``confirm_sentence`` when it names
    the chosen param's value AND every number in it appears in the
    design-state block (``guard_answer_numbers`` — issue #249's guard),
    else the deterministic template — ``copy.confirmOffer.offer`` in the
    SPA's ``copy.ts`` (one string, two consumers — the deck is the
    wording home, this function is the wire's).

    A sentence that names a DIFFERENT value ("I assumed 4 mm" when the
    param is 3) fails the value-name check; a sentence with an invented
    number fails the guard; both fall to the template. A sentence with
    no number at all cannot name the value either → template (the
    sentence must carry the value, not just a mood)."""
    from d33d.question_answer import guard_answer_numbers

    if isinstance(sentence, str) and sentence.strip():
        value_str = _format_value(entry.get("value"))
        label = entry.get("label") or entry.get("name") or ""
        if value_str in sentence and label and label in sentence and guard_answer_numbers(
            sentence, block_entries
        ):
            return sentence.strip()
    value_str = _format_value(entry.get("value"))
    label = entry.get("label") or entry.get("name") or entry["name"]
    return f"I assumed {value_str} for {label}. Want it different?"


def _format_value(value: Any) -> str:  # alias — the private name predates the public one
    return format_param_value(value)


def ack_sentence(entry: dict[str, Any]) -> str:
    """The acknowledgement the accepted-offer flow replies with (the
    SPA's ``confirmOffer.acknowledged`` in ``copy.ts``): "Got it —
    {label} stays {value}." (label per #248, identifier fallback — the
    template is deterministic; the model never writes it)."""
    value_str = _format_value(entry.get("value"))
    label = entry.get("label") or entry.get("name") or entry["name"]
    return f"Got it — {label} stays {value_str}."


def is_pending_offer_acceptance(
    message: str,
    offer: dict[str, Any] | None,
    latest: dict[str, Any] | None,
) -> bool:
    """True iff the user's message is a CLEAN AFFIRMATION (the issue
    #249 heuristic, ``_is_clean_affirmation`` — "yes" yes; "yes but make
    it 2 mm" no — a change request, a question, or a hedge is NOT an
    acceptance) AND a pending offer is LIVE: the offer's version is the
    project's CURRENT LATEST version (a newer version supersedes the
    offer — the stale offer lapses and the message routes normally).

    The value-unchanged check (the param must still carry the offered
    value) happens in the caller (it needs the design-state block, not
    the raw row)."""
    from d33d.dimension_protocol import _is_clean_affirmation

    if offer is None or latest is None:
        return False
    if offer.get("version_id") != latest.get("id"):
        return False
    return _is_clean_affirmation(message)


