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
answer-frame plumbing — event-source registration and the 202 body —
stays in the router). The #351 hole-evidence half (the stored-fact
reader, the gated noun set, the no-hole reply) lives in
:mod:`d33d.part_holes`; this module imports the pieces it calls
(``HOLE_NOUNS``, ``part_has_hole_evidence``, ``no_hole_reply``) directly
from there — the region-edit seam and the tests import from
``d33d.part_holes`` themselves.
"""

from __future__ import annotations

import re
from typing import Any

from d33d.part_holes import HOLE_NOUNS, no_hole_reply, part_has_hole_evidence
from d33d.versions import valid_axis

import math

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
#: distance + direction — the number and direction are kept, never
#: dropped).
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
#: ambiguous (two equally-near candidates, or no qualifier matched) —
#: the offer asks the user to point at the hole on the part. The ``{dim}``
#: slot is the user's stated target diameter (mono-formatted); when the
#: user didn't state a size, the template has no Ø clause.
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

#: The closed feature-noun set a resize/move request can target (issue
#: #332's operator decision (b)+(c)): an imported feature is phrased from
#: the USER'S WORDS, and only these nouns are recognised feature names on
#: a part the user brought. Defined ONCE here — the pre-route, the copy
#: templates, and the tests all read this single constant.
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
#: the <noun>", "move the <noun> …". The dimension, when present, is a
#: full decimal token (``38`` or ``38.5``) followed by "mm" — captured
#: whole so the user's own number is never truncated by trailing
#: punctuation — substituted into the boundary copy, never invented.
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
    """The lower-cased param names, labels, and SINGLE-WORD label tokens
    of the CURRENT version's own params (issue #332's operator decision
    (d): a resize of a feature the user added — a param in the design's
    own SCAD — behaves as today and never triggers the fill-and-recut
    offer).

    Token match: a param named ``hole_diameter`` (label "Hole
    diameter") contributes the token ``hole``, so "make the hole 38 mm"
    does NOT trigger when the design's own SCAD already owns a hole
    param. ``None`` row → empty set (no design of its own yet)."""
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
    """The fill-and-recut offer's acceptance predicate (issue #332's
    operator decision: "yes" / "Yes, do that" run the loop). A clean
    affirmation via the shared heuristic (``_is_clean_affirmation`` —
    short, no question mark, no negation, no hedge)."""
    from d33d.dimension_protocol import _is_clean_affirmation

    return _is_clean_affirmation(message)


def is_clean_no(message: str) -> bool:
    """The fill-and-recut offer's decline predicate (issue #332's operator
    decision: "no" / "Leave it" clear the offer). A short turn carrying a
    negation token ("no"/"not"/...) or the "leave it" phrase, with no
    question mark and no affirmative token."""
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


def _select_measured_hole(
    holes: list[dict[str, Any]],
    message: str,
    bbox_mm: list[float] | None,
    trigger_size: float | None = None,
) -> dict[str, Any] | None:
    """Select the hole the user is referring to from the measured list.

    Selection rules (issue #396):
    1. A single measured hole is picked trivially.
    2. "the center hole" (or similar) picks the hole nearest the part's
       XY bbox centre.
    3. If no qualifier is present and the trigger size matches exactly
       one hole's existing diameter (within 5%), that hole is picked.
    4. If the choice is ambiguous (two candidates equally near, or no
       qualifier and no unique diameter match), return ``None`` (the
       caller falls back to the point-at copy).

    Returns the hole dict or ``None``.
    """
    if not holes:
        return None
    if len(holes) == 1:
        return holes[0]

    msg_lower = message.lower()
    has_center_qualifier = any(
        w in msg_lower for w in ("center", "centre", "middle")
    )

    # "center hole" qualifier: nearest to XY bbox centre.
    if has_center_qualifier and bbox_mm and len(bbox_mm) >= 2:
        cx = bbox_mm[0] / 2.0
        cy = bbox_mm[1] / 2.0
        dists = []
        for h in holes:
            c = h.get("center")
            if not c or len(c) < 2:
                continue
            d = math.hypot(c[0] - cx, c[1] - cy)
            dists.append((d, h))
        if dists:
            dists.sort(key=lambda x: x[0])
            # If two are within 1% of each other, it's ambiguous.
            if len(dists) > 1:
                d_min = dists[0][0]
                d_max = dists[1][0]
                if d_min > 0 and d_max / d_min < 1.01:
                    return None
            return dists[0][1]

    # No center qualifier: try diameter matching (the trigger size must
    # match exactly one hole's existing diameter within 5%).
    if trigger_size is not None and trigger_size > 0:
        size_matches = []
        for h in holes:
            d = h.get("diameter_mm")
            if d and abs(trigger_size - d) / d <= 0.05:
                size_matches.append(h)
        if len(size_matches) == 1:
            return size_matches[0]
        # 0 or 2+ matches: ambiguous (or no match) → point-at fallback.

    # No qualifier matched — ambiguous, return None (point-at fallback).
    return None


def fill_and_recut_instruction(offer: dict[str, Any]) -> str:
    """The explicit design-loop instruction an ACCEPTED fill-recut offer
    appends to the request text (the loop's own import-aware prompt
    already teaches the fill-then-cut move — this makes the accepted
    turn's intent explicit). The noun and size come from the
    SERVER-SIDE offer (never parsed from the client's chat); the size is
    mono-formatted, ``None`` → no size clause.

    Issue #396: when the offer carries a measured hole (``center``,
    ``axis``, ``diameter_mm``), the instruction names the specific hole
    with its numbers: "fill the Ø30.0 mm hole at (60.0, 40.0), axis Z,
    then cut a Ø38.0 mm hole on the same axis". Without a measured hole
    (legacy/region-route offers with no location), the instruction is
    byte-identical to the pre-#396 form."""
    from d33d.part_http import PART_FILENAME

    noun = str(offer.get("noun") or "feature")
    size = offer.get("size")
    size_str = (
        f" at {size:g} mm"
        if isinstance(size, (int, float)) and size > 0
        else ""
    )

    # Issue #396: measured hole location (centre + axis + existing diameter).
    hole_center = offer.get("center")
    hole_diameter = offer.get("diameter_mm")
    hole_axis = offer.get("axis")

    if (
        isinstance(hole_center, (list, tuple))
        and len(hole_center) >= 2
        and all(isinstance(v, (int, float)) and math.isfinite(v) for v in hole_center[:2])
    ):
        # The instruction carries the measured hole's numbers.
        cx, cy = float(hole_center[0]), float(hole_center[1])
        # Diameter clause for the hole being filled.
        dia_str = (
            f"Ø{hole_diameter:g} mm "
            if isinstance(hole_diameter, (int, float)) and hole_diameter > 0
            else ""
        )
        # Axis clause (validated).
        axis_clause = ""
        if hole_axis and valid_axis(hole_axis):
            ax = [float(v) for v in hole_axis]
            # Find the dominant axis name.
            if abs(ax[0]) > 0.9:
                axis_name = "X"
            elif abs(ax[1]) > 0.9:
                axis_name = "Y"
            elif abs(ax[2]) > 0.9:
                axis_name = "Z"
            else:
                axis_name = f"({ax[0]:g}, {ax[1]:g}, {ax[2]:g})"
            axis_clause = f", axis {axis_name}"
        return (
            f"Fill-and-recut: fill the {dia_str}{noun} at ({cx:g}, {cy:g})"
            f"{axis_clause}, then cut a {dia_str}{noun}{size_str} "
            f"on the same axis. Never resize the imported mesh itself — "
            f"import(\"{PART_FILENAME}\") stays as brought."
        )

    # Legacy / region-route: no measured centre — the pre-#396 form.
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


def fill_recut_turn(
    app: Any, project_id: int, message: str
) -> dict[str, Any] | None:
    """The fill-and-recut pre-route's ONE entry point for ONE chat turn
    (issue #332's sub-issue 3). Called by ``d33d.projects.post_chat``
    AFTER the missing-source check and BEFORE the #250 offer / question
    pre-routes (the unsettled-part guard runs first, upstream).

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop": bool,
    "outcome": "fresh_offer" | "decline" | "accept" | "no_feature"}``
    when the turn is handled here (the caller registers the sentence as
    a ``kind: "answer"`` done frame — and, when ``run_loop`` is True,
    runs the design loop with the ``instruction`` field appended to the
    request text), else ``None`` (fall-through to the existing routes).

    The ``outcome`` discriminator is the SAME contract the region-edit
    seam's :func:`d33d.fill_recut_region.fill_recut_region_edit`
    returns: the caller keys OFF ``outcome`` — the done frame's
    ``fill_recut_offer`` flag (the SPA's [Yes, do that] / [Leave it]
    buttons) is set ONLY for ``fresh_offer``; a clean ``decline``
    re-emitting it would re-render the buttons for an offer that no
    longer exists.

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
    if part is None or part.get("unit_status") not in ("assumed", "settled"):
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

                if trigger["noun"] in HOLE_NOUNS:
                    report = part.get("report") if part else None
                    holes: list[dict[str, Any]] = []
                    bbox_mm: list[float] | None = None
                    if isinstance(report, dict):
                        holes = report.get("holes") or []
                        # bbox_mm from the report (file units × scale).
                        bbox_fu = report.get("bbox_file_units")
                        scale = part.get("scale")
                        if (
                            isinstance(bbox_fu, (list, tuple))
                            and len(bbox_fu) >= 2
                            and scale is not None
                            and scale > 0
                        ):
                            bbox_mm = [float(v) * scale for v in bbox_fu]
                    if holes:
                        selected = _select_measured_hole(
                            holes, message, bbox_mm,
                            trigger_size=trigger["size"],
                        )
                        if selected is not None:
                            offer_dict["center"] = selected.get("center")
                            offer_dict["axis"] = selected.get("axis")
                            offer_dict["diameter_mm"] = selected.get("diameter_mm")
                        else:
                            # Ambiguous or no qualifier matched: use the
                            # point-at copy (never an instruction without
                            # a location).
                            point_at_fallback = True

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

                versions.set_pending_offer(project_id, offer_dict)
                return {
                    "kind": "answer",
                    "answer": sentence,
                    "run_loop": False,
                    "outcome": "fresh_offer",
                }
    return None


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
    "_select_measured_hole",
    "boundary_sentence",
    "fill_and_recut_instruction",
    "fill_recut_trigger",
    "fill_recut_turn",
    "is_clean_no",
    "is_clean_yes",
    "own_feature_names",
]
