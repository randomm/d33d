"""The fill-and-recut boundary pre-route (issue #332, sub-issue 3).

When a project has a part whose units ARE assumed/settled and the user
asks to RESIZE or MOVE one of the part's own features (a noun from the
closed feature-noun set that is NOT a param name or label of the
design's own current version — operator decision (b)–(e)), the reply is
the spec's templated boundary sentence (copy.ts) as a ``kind: "answer"``
done frame, and a fill-recut offer is recorded server-side (the
pending-offer field, ``kind: "fill_recut"``, which the #250 param-offer
route never reads back). A clean "yes" on the pending offer runs the
design loop with an explicit fill-and-recut instruction; a clean "no"
clears the offer.

The module's ONE entry point is :func:`fill_recut_turn`, which
``d33d.projects.post_chat`` calls after the missing-source check (the
stays in the router). The #351 hole-evidence half lives in
:mod:`d33d.part_holes`; the measured-hole selection and instruction live
in :mod:`d33d.hole_select` (this module imports both from there)."""

from __future__ import annotations

import math
import re
from typing import Any

from d33d.hole_select import fill_recut_instruction_with_hole
from d33d.versions import valid_axis

# Issue #332 (sub-issue 3) — the unsettled-part chat reply (verbatim copy
# of the copy.ts sentence — the parity test in
# ``tests/test_projects.py`` pins the two-way agreement against
# ``web/src/copy.ts``). The units are not settled; the design loop must
# not run until the user settles them.
UNSETTLED_PART_REPLY = (
    "The part's units aren't settled yet, so I can't work on it. "
    "Settle the units first — pick mm, cm, or inch, or give one measured "
    "axis — and then I can add and cut on it."
)

# Issue #332 (sub-issue 3) — the fill-and-recut boundary copy (verbatim
# copies of the copy.ts sentences — the parity test in
# ``tests/test_projects.py`` pins each against ``web/src/copy.ts``; the
# ``{noun}``/``{dim}``/``{distance}``/``{direction}`` slots are the
# template substitutions, substituted from the USER'S OWN words and
# number, never invented).
#: The point-at-the-spot move reply (a move request with no distance).
FRILL_MOVE_REPLY = (
    "That {noun} came with your file, so I can't move it directly — "
    "the file has no parameters for me to change. What I can do: "
    "fill it, then cut a new one where you want it. Point at the spot, "
    "or tell me where."
)
#: The move-with-distance reply (a move request carrying the user's own
#: distance + direction — the number and direction are kept).
FRILL_MOVE_DISTANCE_REPLY = (
    "That {noun} came with your file, so I can't move it directly — "
    "the file has no parameters for me to change. What I can do: "
    "fill it, then cut a new one {distance} mm {direction} of where "
    "it is now. It'll look the same, and you'll see it as a change in "
    "the history."
)
#: The hole/bore diameter resize reply (the UX spec's sentence).
FRILL_HOLE_DIAMETER_REPLY = (
    "That {noun} came with your file, so I can't resize it directly — "
    "the file has no parameters for me to change. What I can do: "
    "fill it, then cut a Ø{dim} mm one on the same axis. It'll look "
    "the same, and you'll see it as a change in the history."
)
#: The other-noun resize reply (a dimension the user stated).
FRILL_NOUN_DIMENSION_REPLY = (
    "That {noun} came with your file, so I can't resize it directly — "
    "the file has no parameters for me to change. What I can do: "
    "fill it, then cut a new {noun} at {dim} mm in the same place. "
    "It'll look the same, and you'll see it as a change in the history."
)
#: The no-dimension ask (the user named a feature but no size).
FRILL_NO_DIMENSION_REPLY = (
    "How big should the {noun} be? It came with your file, so I'll "
    "fill it and cut a new one at that size."
)
#: The quiet decline acknowledgement (a clean "no" on the pending offer).
FILL_RECUT_DECLINE_REPLY = "Understood — leaving the part as it is."

#: The point-at fallback (issue #396): the measured-hole selection is
#: ambiguous (two equally-near candidates, or no qualifier matched).
FRILL_POINT_AT_TEMPLATE = (
    "Point at the {noun} on the part and I'll fill it and cut a Ø{dim} mm one there."
)
FRILL_POINT_AT_NO_DIM_TEMPLATE = (
    "Point at the {noun} on the part and I'll fill it and cut a new one there."
)

#: The input bound for :func:`fill_recut_trigger`: an instruction longer
#: than this many characters is NOT a resize/move request — it bails out
#: before the regexes run.
TRIGGER_MAX_INSTRUCTION_CHARS = 500

FEATURE_NOUNS = frozenset(
    {
        "hole",
        "holes",
        "slot",
        "slots",
        "boss",
        "post",
        "tab",
        "recess",
        "pocket",
        "cutout",
        "notch",
        "groove",
        "bore",
        "counterbore",
    }
)

#: The add/create verbs that turn a feature message into an ADD (an add
#: goes to the design loop — rule 4 forbids only RESIZING the imported
#: mesh, not adding onto it; issue #332's operator decision (e)).
_ADD_VERB_RE = re.compile(
    r"\b(?:add|drill|cut\s+a\s+new|make\s+a\s+new|put\s+a)\b", re.IGNORECASE
)

#: The resize/move phrasings the trigger scans for (operator decision (b)):
#: "make the <noun> N mm", "make the <noun> bigger/smaller/wider", "resize
#: the <noun>", "move the <noun> …". The dimension is a full decimal token
#: followed by "mm", captured whole (the user's number is never truncated
#: by trailing punctuation) and substituted into the boundary copy.
_NOUN_ALT = "|".join(sorted(FEATURE_NOUNS))
_FRILL_RESIZE_RE = re.compile(
    r"\b(?:make|resize)\b[^.,;!?]{0,40}?(?:the\s+|a\s+)?"
    r"(?P<noun>" + _NOUN_ALT + r")\b[^.!?]*?\b(?P<size>\d+(?:\.\d+)?)\s*mm\b",
    re.IGNORECASE,
)
_FRILL_BIGGER_RE = re.compile(
    r"\b(?:make|resize)\b[^.,;!?]{0,40}?(?:the\s+|a\s+)?"
    r"(?P<noun>" + _NOUN_ALT + r")\b[^.!?]*?\b(?:bigger|smaller|wider|narrower)\b",
    re.IGNORECASE,
)
#: A move carrying the user's own distance + direction — the distance is
#: a full decimal token followed by "mm" and the direction is the
#: same closed where-word set as the point-at-the-spot move.
_FRILL_MOVE_DISTANCE_RE = re.compile(
    r"\bmove\b[^.,;!?]{0,40}?(?:the\s+|a\s+)?"
    r"(?P<noun>" + _NOUN_ALT + r")\b[^.!?]*?\b(?P<distance>\d+(?:\.\d+)?)\s*mm\b"
    r"[^.!?]*?\b(?P<where>left|right|up|down|over|out|in|here|there)\b",
    re.IGNORECASE,
)
_FRILL_MOVE_RE = re.compile(
    r"\bmove\b[^.,;!?]{0,40}?(?:the\s+|a\s+)?"
    r"(?P<noun>" + _NOUN_ALT + r")\b[^.!?]*?\b(?P<where>left|right|up|down|over|out|in|here|there)\b",
    re.IGNORECASE,
)

#: The closed set of the where-words the move replies accept (the
#: user's own direction word, substituted — never an invented one).
_MOVE_DIRECTIONS = frozenset(
    {"left", "right", "up", "down", "over", "out", "in", "here", "there"}
)

def _fmt_size(value: float | None) -> str | None:
    """The user's number, mono-formatted the way the deck renders it
    (never invented, never truncated)."""
    if value is None:
        return None
    return f"{value:g}"


def boundary_sentence(
    noun: str,
    size_mm: float | None,
    move: bool = False,
    move_distance_mm: float | None = None,
    move_direction: str | None = None,
) -> str:
    """The spec's boundary sentence, templated (issue #332's operator
    decision: a CLOSED set of templates with the user's noun and
    dimension substituted — mono-formatted mm, never invented).

    The five shapes, in the operator decision's order: a move with a
    distance + direction (the fill-then-cut at the user's stated offset
    — the number and direction are KEPT); a move without a distance
    (the point-at-the-spot ask); a hole/bore with a diameter (the UX
    spec's sentence VERBATIM); another noun with a dimension ("fill it,
    then cut a new {noun} at {dimension} mm in the same place"); and no
    dimension given (the ask, "How big should the {noun} be?")."""
    if move:
        if move_distance_mm is not None and move_direction in _MOVE_DIRECTIONS:
            return FRILL_MOVE_DISTANCE_REPLY.format(
                noun=noun,
                distance=_fmt_size(move_distance_mm),
                direction=move_direction,
            )
        return FRILL_MOVE_REPLY.format(noun=noun)
    if size_mm is None:
        return FRILL_NO_DIMENSION_REPLY.format(noun=noun)
    dim = _fmt_size(size_mm)
    if noun in ("hole", "holes", "bore", "counterbore"):
        return FRILL_HOLE_DIAMETER_REPLY.format(noun=noun, dim=dim)
    return FRILL_NOUN_DIMENSION_REPLY.format(noun=noun, dim=dim)

def fill_recut_trigger(message: str) -> dict[str, Any] | None:
    """The deterministic fill-and-recut trigger for ONE chat message
    (issue #332's operator decision): ``None`` when the message does not
    ask to RESIZE or MOVE an imported feature, else
    ``{"noun": <closed-set noun>, "size": <mm float or None>, "move":
    bool, "move_distance": <mm float or None>, "direction": <where-word
    or None>}``.

    Fires only when ALL of the operator decision's (b)–(e) hold: (b) the
    message asks to resize or move an existing feature ("make the <noun>
    N mm", "make the <noun> bigger/smaller", "resize the <noun>", "move
    the <noun> …") — "taller"/"wider the part" is an ADD (no closed-set
    noun) and never triggers; (c) the noun is in :data:`FEATURE_NOUNS`
    (a closed set, never a free-form noun match); (e) the message has no
    add/create verb ("add a 38 mm hole" is an add and goes to the design
    loop). (a) — an assumed/settled part — and (d) — the noun is not a
    param name/label of the CURRENT version — are caller-side checks this
    pure helper does not take.

    An instruction longer than :data:`TRIGGER_MAX_INSTRUCTION_CHARS`
    returns ``None`` before ANY regex runs (a multi-KB unpunctuated blob
    is never a resize/move request, and the unbounded ``[^.!?]`` spans
    must not spin on it)."""
    if len(message) > TRIGGER_MAX_INSTRUCTION_CHARS:
        return None
    if _ADD_VERB_RE.search(message):
        return None
    m = _FRILL_RESIZE_RE.search(message)
    if m is not None:
        return {
            "noun": m.group("noun").lower(),
            "size": float(m.group("size")),
            "move": False,
            "move_distance": None,
            "direction": None,
        }
    m = _FRILL_BIGGER_RE.search(message)
    if m is not None:
        return {
            "noun": m.group("noun").lower(),
            "size": None,
            "move": False,
            "move_distance": None,
            "direction": None,
        }
    m = _FRILL_MOVE_DISTANCE_RE.search(message)
    if m is not None:
        return {
            "noun": m.group("noun").lower(),
            "size": None,
            "move": True,
            "move_distance": float(m.group("distance")),
            "direction": m.group("where").lower(),
        }
    m = _FRILL_MOVE_RE.search(message)
    if m is not None:
        return {
            "noun": m.group("noun").lower(),
            "size": None,
            "move": True,
            "move_distance": None,
            "direction": None,
        }
    return None

def own_feature_names(latest: dict[str, Any] | None) -> set[str]:
    """The lower-cased param names, labels, and single-word label tokens
    of the CURRENT version's own params (issue #332 decision (d): a
    resize of a feature the user added — a param in the design's own
    SCAD — never triggers). ``None`` row → empty set."""
    names: set[str] = set()
    if not latest:
        return names
    params = latest.get("params") or {}
    meta = latest.get("param_meta") or {}
    for key in params:
        names.add(str(key).lower())
        m = meta.get(key) or {}
        label = m.get("label")
        if isinstance(label, str) and label:
            for token in label.lower().split():
                names.add(token)
    return names

def is_clean_yes(message: str) -> bool:
    """The fill-and-recut offer's acceptance predicate (clean
    affirmation via the shared ``_is_clean_affirmation``)."""
    from d33d.dimension_protocol import _is_clean_affirmation

    return _is_clean_affirmation(message)

def is_clean_no(message: str) -> bool:
    """The fill-and-recut offer's decline predicate: a short turn with
    a negation token or "leave it", no question mark, no affirmative."""
    low = message.lower()
    if "?" in low:
        return False
    if re.search(r"\bleave (it|that)\b", low):
        return True
    if re.search(
        r"\b(confirm|confirmed|yes|yep|correct|right|accept|ok|okay)\b", low
    ):
        return False
    return bool(
        re.search(r"\b(no|not|nope|nah|wrong|incorrect|never|drop it|forget it)\b", low)
    )

def fill_and_recut_instruction(offer: dict[str, Any]) -> str:
    """The explicit design-loop instruction an ACCEPTED fill-recut offer
    appends to the request text (the loop's own import-aware prompt
    already teaches the fill-then-cut move — this makes the accepted
    turn's intent explicit). The noun and size come from the SERVER-SIDE
    offer (never parsed from the client's chat).

    Issue #396: when the offer carries a measured hole (``center``,
    ``axis``, ``diameter_mm``), the instruction names the specific hole
    with its numbers (built by
    :func:`d33d.hole_select.fill_recut_instruction_with_hole`). Without a
    measured hole, the instruction is byte-identical to the pre-#396
    form."""
    from d33d.part_http import PART_FILENAME

    noun = str(offer.get("noun") or "feature")
    size = offer.get("size")
    hole_center = offer.get("center")
    if (
        isinstance(hole_center, (list, tuple))
        and len(hole_center) >= 2
        and all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in hole_center[:2])
    ):
        return fill_recut_instruction_with_hole(
            noun=noun,
            center=[float(v) for v in hole_center],
            axis=offer.get("axis"),
            diameter_mm=offer.get("diameter_mm"),
            size=size,
        )

    # Legacy / region-route: no measured centre — the pre-#396 form.
    size_str = (
        f" at {size:g} mm"
        if isinstance(size, (int, float)) and size > 0
        else ""
    )
    axis = offer.get("axis")
    if valid_axis(axis):
        axis = [float(v) for v in axis]
        axis_str = f" (axis {axis[0]:g}, {axis[1]:g}, {axis[2]:g})"
    else:
        axis_str = ""
    return (
        "Fill-and-recut: union a solid over the existing "
        f"{noun} of the imported part, then difference "
        f"the new {noun}{size_str} on the same "
        f"axis/location{axis_str}. Never resize the imported mesh "
        f"itself — import(\"{PART_FILENAME}\") stays as "
        "brought."
    )

__all__ = [
    "FEATURE_NOUNS",
    "FILL_RECUT_DECLINE_REPLY",
    "FRILL_HOLE_DIAMETER_REPLY",
    "FRILL_MOVE_DISTANCE_REPLY",
    "FRILL_MOVE_REPLY",
    "FRILL_NOUN_DIMENSION_REPLY",
    "FRILL_NO_DIMENSION_REPLY",
    "FRILL_POINT_AT_NO_DIM_TEMPLATE",
    "FRILL_POINT_AT_TEMPLATE",
    "TRIGGER_MAX_INSTRUCTION_CHARS",
    "UNSETTLED_PART_REPLY",
    "boundary_sentence",
    "fill_and_recut_instruction",
    "fill_recut_trigger",
    "is_clean_no",
    "is_clean_yes",
    "own_feature_names",
]


def __getattr__(name: str):
    """Lazy re-export of ``fill_recut_turn`` (which lives in
    :mod:`d33d.fill_recut_turn`).

    A top-level ``from d33d.fill_recut_turn import fill_recut_turn`` would
    be a circular import: ``fill_recut_turn`` imports the constants and
    helpers it uses FROM ``d33d.fill_recut``, so the two modules must not
    import each other at module load. PEP 562's module ``__getattr__``
    re-exports ``fill_recut_turn`` lazily — ``d33d.projects.post_chat`` and
    the tests do ``from d33d.fill_recut import fill_recut_turn`` and get the
    same function object (``d33d.fill_recut_turn.fill_recut_turn``) without
    the cycle.
    """
    if name == "fill_recut_turn":
        from d33d.fill_recut_turn import fill_recut_turn

        return fill_recut_turn
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
