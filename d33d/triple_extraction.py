"""W×D×H triple/pair extraction (issue #275 task-a; restructured #275
round-3, items 2–7; issue #305 by-joiner + part-noun conditional).

The self-contained, pure parsing half of the dimension protocol (extracted
from ``d33d.dimension_protocol`` so that module keeps its cohesion around
the gate / clarification / fit-type domain logic): the ``W×D×H`` /
``W×D``-shaped triple and the per-message mm-cue values it consumes.
"""

from __future__ import annotations

import re

from d33d.axis_lexicon import (
    _FEATURE_NOUNS,
    _FOREIGN_UNIT_RE,
    _PART_NOUNS,
    MM_UNIT_ALTERNATION,
    _mating_connector_at,
)

__all__ = [
    "_extract_triple",
    "_in_mating_zone",
    "_mm_cue_values",
    "_triple_suppressed_by_feature_noun",
    "user_quoted_unmapped_mm",
]

#: The tier-2 history scan bound (issue #261 fix batch):
#: ``user_quoted_unmapped_mm`` scans at most the LAST
#: ``QUOTED_UNMAPPED_MAX_MESSAGES`` user messages — the offer cares about
#: recent user intent, and an unbounded scan of a very long transcript is
#: wasted work on a hot path. Messages older than the window do not make
#: a number eligible.
QUOTED_UNMAPPED_MAX_MESSAGES = 50

#: A no-unit double ("60x45", "5x5") whose numbers exceed this value is
#: a resolution or a count pair ("1920x1080" pixels, "2x4" pieces),
#: not a millimetre envelope, and states nothing: an explicit mm unit
#: ("60 × 45 mm") or the triple's 3-number form is the explicit cue the
#: unit-less reading requires (issue #275 round-1 false-positive fix).
_NO_UNIT_DOUBLE_MAX_MM = 100.0

#: The W×D / W×D×H triple pattern, ONE compiled definition shared by the
#: main pass and the earlier-match candidate scan (issue #305: the "same
#: joiners" invariant is enforced structurally — one pattern string, no
#: drift). Joiners: "×" (U+00D7), "x", "X" with optional spaces, plus
#: "by" as a whole word (issue #305 — "60 by 45 by 80 mm" states W/D/H
#: like the x-form; each joiner is independent, so "60 x 45 by 80 mm"
#: is a valid mixed triple). The unit may be "mm" or a spelled-out
#: "millimetre(s)"/"millimeter(s)" (the shared ``MM_UNIT_ALTERNATION``
#: — one definition with the lexicon), after the last number or after
#: each number.
_TRIPLE_RE = re.compile(
    r"(?<!\d)\b(\d+(?:\.\d+)?)(?:" + MM_UNIT_ALTERNATION + r")?"
    r"\s*(?:[×xX]|by|By|BY)"
    r"\s*(\d+(?:\.\d+)?)(?:" + MM_UNIT_ALTERNATION + r")?"
    r"(?:\s*(?:[×xX]|by|By|BY)\s*(\d+(?:\.\d+)?)(?:" + MM_UNIT_ALTERNATION + r")?)?"
)

#: The explicit-mm unit test over a match span (the shared
#: ``MM_UNIT_ALTERNATION`` forms — "mm" glued as "45mm" or spaced as
#: "45 mm", spelled-out "millimetre(s)"/"millimeter(s)"; the glued "45mm"
#: has no word boundary between the digit and the "m", so ``\bmm\b``
#: alone misses it).
_MM_UNIT_RE = re.compile(r"mm|millimetres?|millimeters?", re.IGNORECASE)


def _triple_suppressed_by_feature_noun(
    message: str,
    m: re.Match[str],
    feature_nouns: frozenset[str] = _FEATURE_NOUNS,
) -> bool:
    """True if a feature noun sits in the triple's before- or after-window
    (issue #275 round-1 feature-noun suppression; issue #275 round-3, item
    2, moved into a helper so EVERY match runs the same guard).

    A triple is suppressed iff a feature noun occurs in EITHER of these
    windows: (a) the up-to-3 words immediately AFTER the triple's last
    number/unit, stopping early at a comma, ";", sentence punctuation, or
    the words "with", "and", "in", "on", "for"; (b) the up-to-3 words
    immediately BEFORE the triple's first number, stopping early at the
    same separators. Words are whitespace tokens.

    ``feature_nouns`` defaults to the full ``_FEATURE_NOUNS`` (the
    unconditional suppression path); ``_extract_triple`` passes the
    ``_PART_NOUNS`` subset for its conditional part-noun check (issue
    #305) so "lid" can state when it is the message's primary object.
    """
    separators = {",", ";", ".", "!", "?", "with", "and", "in", "on", "for"}

    # After-window: up to 3 words after the triple's end.
    after_words = message[m.end():].split()
    after_window: list[str] = []
    for w in after_words[:3]:
        # Issue #305 round-1: a word with trailing punctuation ("lid,") is
        # a clause boundary. It is NOT added to the window — the comma
        # means the word belongs to a different clause and should not
        # suppress the current triple. Break immediately.
        if w != w.rstrip(",;.!?:()[]{}\"'"):
            break
        clean_w = w.strip(",;.!?:()[]{}\"'")
        if clean_w.lower() in separators:
            break
        after_window.append(clean_w.lower())

    # Before-window: up to 3 words before the triple's start (issue #275
    # round-3, item 7: was 2 — "a 10 × 10 mm square hole" needs 3: "square"
    # stops the walk as a non-noun, then "hole" suppresses; "in" still
    # cuts "hole in a 60 × 45 × 20 mm tray"'s before-window early).
    before_words = message[: m.start()].split()
    before_window: list[str] = []
    for w in reversed(before_words[-3:]):
        if w != w.rstrip(",;.!?:()[]{}\"'"):
            break
        clean_w = w.strip(",;.!?:()[]{}\"'")
        if clean_w.lower() in separators:
            break
        before_window.append(clean_w.lower())

    return any(w in feature_nouns for w in after_window) or any(
        w in feature_nouns for w in before_window
    )


def _in_mating_zone(message: str, pos: int) -> bool:
    """True if ``pos`` (a match's start) sits in the mating part's zone
    (issue #314): after a mating connector (``_MATING_CONNECTORS`` —
    "fits", "fit", "fitting", "to fit", "for", "over", "onto", "on top
    of", "that goes on") with no later mating connector before it, so the
    size belongs to the MATING PART, never the part being made.

    A size after a mating connector is the mating part's, not the part's:
    "a 55 × 40 mm lid that fits a 60 × 45 mm box" → the lid (55 × 40) is
    the part; the box's 60 × 45 must never state the lid's axes. The rule
    is head-noun-independent ("a 60 mm wide stand for a 100 × 70 mm
    phone" → the phone's pair is the mating part's), and clause-local:
    a later "with"/"and" clause after the mating phrase resumes normal
    parsing, but a connector's zone extends to the NEXT mating connector
    ("a lid that fits a 60 × 45 mm box, 5 mm thick" → the 5 mm is still
    in the box's zone — "thick" is not an axis word, so nothing states
    either way).

    Multi-word connectors ("to fit", "on top of", "that goes on") match
    as whole phrases; the shared ``_MATING_CONNECTOR_RE`` (the
    axis_lexicon's single definition, longest-first) never matches "fit"
    inside another token, and ``_mating_connector_at``'s fit-type filter
    ("slip fit"/"press fit") is inherited by this helper — a fit-TYPE
    word is not a mating connector. This is the single helper the
    triple/pair path uses; the lexicon's clause classification uses the
    same ``_mating_connector_at`` from its own side.
    """
    return _mating_connector_at(message, pos)


def _match_states(message: str, m: re.Match[str]) -> bool:
    """True if the triple/pair match ``m`` passes ALL the unconditional
    per-match guards (the shared guard body — ONE evaluation, used by BOTH
    the main pass in ``_extract_triple`` and the earlier-match candidate
    scan, so a future guard change applies in exactly one place).

    Guards, in order (any failure → the match states nothing):
    1. two or more numbers (a lone number is not a triple/pair);
    2. letter-glued prefix: a letter immediately before the first digit
       ("M3 x 10 mm" — thread spec; "v2 is 60x45x20mm" — a version
       label);
    3. feature-noun window (``_triple_suppressed_by_feature_noun`` over
       the FULL ``_FEATURE_NOUNS`` set — the unconditional path);
    4. foreign unit (only when the match has NO explicit mm unit — an
       explicit mm unit wins, so the "in" in "60 x 45 mm in the drawer"
       is the preposition, not the inch unit): a cm/in/inches/inch/m
       anywhere in the match span, or as a WHOLE WORD in the next
       whitespace-delimited token ("2 × 2 inch"/"6 x 4 cm" → nothing);
    5. magnitude: an explicit mm unit anywhere on the match NEVER
       magnitude-suppresses ("a 150x45mm tray" → W150 D45); the >100
       bound applies only to a unit-less pair ("1920x1080",
       "a 150x45 tray" → nothing);
    6. mating connector (issue #314): the match starts in the mating
       part's zone (``_in_mating_zone``) — a size after a mating
       connector belongs to the mating part and never states the part's
       axes; its numbers stay unmapped/offerable.

    The part-noun conditional (issue #305) is deliberately NOT part of
    this predicate — it lives in ``_extract_triple`` as the single
    additional branch that relaxes guard 3 for a two-number pair whose
    window holds a ``_PART_NOUNS`` word.
    """
    numbers = [float(g) for g in m.groups() if g]
    if len(numbers) < 2:
        return False
    # Guard 1: letter-glued prefix — the character IMMEDIATELY before the
    # match's first digit (the ``(?<!\\d)`` lookbehind already keeps
    # digit-adjacent starts out — "123x45x67" — so this check only needs
    # the letter case).
    before_char = message[m.start() - 1] if m.start() > 0 else ""
    if before_char.isalpha():
        return False
    # Guard 2: feature-noun window (the full set — unconditional).
    if _triple_suppressed_by_feature_noun(message, m):
        return False
    # Guard 3: foreign unit — only when the match has NO explicit mm
    # unit of its own (an explicit mm unit wins). The unit is foreign
    # when it sits anywhere in the match span, or as a whole word as the
    # WHOLE NEXT whitespace-delimited token (the unit may follow the
    # triple's end: "6 x 4 cm"). The next token must equal a unit word
    # exactly: a 5-char prefix match would fire on "10 inch" via "10 in"
    # (the \b between "in" and "c" is a boundary), which would suppress a
    # legitimate "2 × 2 inch"-shaped statement — "2 × 2 inch" is itself
    # foreign (the "inch" token) but must be rejected for the right
    # reason.
    has_mm_unit = _MM_UNIT_RE.search(message[m.start(): m.end()]) is not None
    if not has_mm_unit:
        if _FOREIGN_UNIT_RE.search(message[m.start(): m.end()]):
            return False
        next_token = message[m.end():].lstrip().split()[:1]
        if next_token and next_token[0].lower() in {
            "cm", "in", "inches", "inch", "m",
        }:
            return False
    # Guard 4: magnitude — the bound applies only to a unit-less pair.
    if len(numbers) == 2 and not has_mm_unit and any(
        v > _NO_UNIT_DOUBLE_MAX_MM for v in numbers
    ):
        return False
    # Guard 5: mating connector (issue #314) — a size after a mating
    # connector ("fits", "for", "over", …) belongs to the mating part and
    # never states the part's axes; its numbers stay unmapped/offerable.
    return not _in_mating_zone(message, m.start())


def _any_other_stating_match(message: str, exclude: re.Match[str]) -> bool:
    """True if any number-bearing triple or pair in ``message`` OTHER THAN
    ``exclude`` states its axes (issue #305: the part-noun primary-object
    rule's suppression trigger).

    A candidate states when it passes the SAME unconditional guard stack
    as the main pass — :func:`_match_states` (letter-glued, full
    feature-noun window, foreign-unit, magnitude). The part-noun
    conditional is intentionally NOT applied to candidates: the
    operator's rule is "another number-bearing triple or pair already
    stated the envelope" — the candidate must pass the UNCONDITIONAL
    feature-noun guard to be a stating envelope, and the part-noun
    conditional only relaxes suppression for the primary-object pair
    itself, never for a secondary pair.

    The scan covers the WHOLE message (not only earlier matches): a
    part-noun pair states W/D only when it is the message's SOLE
    number-bearing triple/pair. Any other stating triple/pair — earlier
    ("a box 60 × 45 × 80 mm with a 55 × 40 mm lid" → the box states)
    OR LATER ("make a 40x60 lid, the box is 60x45x80mm" → the box
    states) — takes precedence over the pair's W/D reading, which
    would otherwise fabricate a wrong fit-critical envelope (the
    exact anti-pattern the bbox gate exists to prevent). ``exclude``
    is the pair under evaluation (its own span is skipped, so a
    message whose only stating match IS the pair states W/D).
    """
    for cm in _TRIPLE_RE.finditer(message):
        if cm.start() == exclude.start():
            continue
        # Issue #314: a candidate in the mating part's zone (after a
        # mating connector) never states, so it can never count as the
        # "other stating match" that would suppress a part-noun pair
        # (e.g. the box pair in "a 55 × 40 mm lid that fits a 60 × 45 mm
        # box" must not suppress the lid pair's W/D reading). The
        # part-noun conditional is intentionally NOT applied here
        # (same as before #314): the candidate must pass the UNCONDITIONAL
        # guard stack — and the mating guard is unconditional.
        if _match_states(message, cm):
            return True
    return False


def _part_noun_in_window(message: str, m: re.Match[str]) -> bool:
    """True if a ``_PART_NOUNS`` word sits in the up-to-3-word window
    immediately after or before the match ``m`` (issue #305 round-1:
    the main-pass skip for part-noun pairs).

    Uses a BROADER window than ``_triple_suppressed_by_feature_noun``:
    words with trailing punctuation ("lid,") are still checked as part
    nouns (the comma is punctuation, not a different word), and the walk
    continues past them (up to 3 words). This is needed because the
    comma after "lid" makes it a clause boundary for the feature-noun
    window (which stops the walk), but the part-noun check needs to see
    "lid" to skip the pair in the main pass.

    The walk stops at words that are clause separators (",", ";", ".",
    "!", "?", "with", "and", "in", "on", "for") — but a word like "lid,"
    is NOT a separator; it's a part noun with trailing punctuation.
    """
    separators = {",", ";", ".", "!", "?", "with", "and", "in", "on", "for"}

    # After-window: up to 3 words after the match.
    after_words = message[m.end():].split()
    for w in after_words[:3]:
        clean_w = w.strip(",;.!?:()[]{}\"'")
        if clean_w.lower() in separators:
            break
        if clean_w.lower() in _PART_NOUNS:
            return True

    # Before-window: up to 3 words before the match.
    before_words = message[: m.start()].split()
    for w in reversed(before_words[-3:]):
        clean_w = w.strip(",;.!?:()[]{}\"'")
        if clean_w.lower() in separators:
            break
        if clean_w.lower() in _PART_NOUNS:
            return True

    return False


def _extract_triple(message: str) -> tuple[dict[str, float], set[float]]:
    """Extract the W×D×H part-envelope triple from a raw message
    (issue #275 task-a; restructured issue #275 round-3, items 2–7).

    Returns ``(axes, consumed_numbers)`` where ``axes`` maps W/D/H to mm
    values (empty dict when nothing is stated) and ``consumed_numbers``
    is the set of numbers CONSUMED by the triple (for exclusion from
    ``unmapped_mm_numbers`` and ``_mm_cue_values``).

    Per-match evaluation (issue #275 round-3, item 2): EVERY match is
    evaluated against the per-match guards; the part's envelope is the
    FIRST match that passes ALL guards (not the last); a match that fails
    a guard states nothing and its numbers stay UNCONSUMED (still
    unmapped/offerable, including feature-noun- and letter-glued
    suppression) — consumption is uniform across all suppression paths:
    a number is consumed only when a match states.

    Joiners: "×" (U+00D7), "x", "X" with optional spaces, plus "by" as a
    whole word (issue #305 — "60 by 45 by 80 mm" states W/D/H like the
    x-form; each joiner is independent, so "60 x 45 by 80 mm" is a valid
    mixed triple). "by" is whole-word and only fires between two numbers,
    so "stand by", "by the edge", "made by 3 mm walls", "made by Alice"
    never match (no digit on both sides of the joiner). The unit may be
    "mm" or a spelled-out "millimetre(s)"/"millimeter(s)" (the shared
    ``MM_UNIT_ALTERNATION`` — one definition with the lexicon), after the
    last number or after each number. Two numbers state W and D only.

    Per-match guards, in order:
    1. starts on the first digit of its number (``(?<!\\d)`` — no match
       inside a longer number, "123x45x67" → nothing, not 23x45x67);
    2. the unconditional guard stack of :func:`_match_states`
       (letter-glued prefix, feature-noun window, foreign unit,
       magnitude) — ONE shared body, never re-implemented;
    3. part-noun conditional (issue #305, the single branch that
       diverges from ``_match_states``): a two-number pair whose
       window holds a ``_PART_NOUNS`` word (e.g. "lid") passes the
       full feature-noun guard only when the pair is the message's
       PRIMARY object — ``_any_stating_match`` over the WHOLE message
       is False (no other number-bearing triple or pair, earlier OR
       later, states the envelope). Any other stating candidate takes
       precedence: "a box 60 × 45 × 80 mm with a 55 × 40 mm lid" and
       "make a 40x60 lid, the box is 60x45x80mm" both → the box
       states; the pair stays suppressed ("a 60 × 45 mm lid",
       "make a 60 x 45 mm lid" → the pair states W/D). The conditional
       applies ONLY to two-number pairs (a 3-number triple with "lid"
       in the window is a feature-size spec, not a primary object, and
       stays suppressed).
    """
    for m in _TRIPLE_RE.finditer(message):
        # First try the shared unconditional guard stack.
        if _match_states(message, m):
            # Issue #305 round-1: a pair whose window holds a part noun
            # (e.g. "lid") is NOT a stating envelope in the main pass —
            # it's only the part's own dimensions, not the box envelope.
            # Without this check, "a 55 x 40 mm lid, box is 60x45x80mm"
            # would state the lid pair (55/40) instead of the box (60/45/80)
            # because the comma makes "lid" a clause boundary outside the
            # feature-noun window. The part-noun conditional below handles
            # the primary-object case; here we skip the pair and let the
            # box triple state.
            if m.group(3) is None and _part_noun_in_window(message, m):
                continue
            numbers = [float(g) for g in m.groups() if g]
            axes: dict[str, float] = {}
            for i, axis in enumerate(("W", "D", "H")):
                if i < len(numbers):
                    axes[axis] = numbers[i]
            return axes, set(numbers)
        # Fallback: part-noun conditional (issue #305) — a two-number
        # pair whose window holds a part noun states W/D when it is the
        # message's PRIMARY object (no OTHER number-bearing triple/pair,
        # earlier OR later, states the envelope). The pair passes the
        # rest of the guard stack (letter-glued, foreign-unit, magnitude
        # already verified in _match_states) but bypasses the feature-noun
        # window guard (which would suppress it for "lid").
        if m.group(3) is None and _triple_suppressed_by_feature_noun(
            message, m, _PART_NOUNS
        ) and not _any_other_stating_match(message, m):
            # The pair is the primary object — state W/D.
            numbers = [float(g) for g in m.groups() if g]
            axes: dict[str, float] = {}
            for i, axis in enumerate(("W", "D")):
                if i < len(numbers):
                    axes[axis] = numbers[i]
            return axes, set(numbers)
    return {}, set()


def _mm_cue_values(message: str) -> set[float]:
    """The mm values an explicit protocol cue in ONE message consumed
    (issue #261, task-b): the axis-prefixed forms (``W: 42`` / "D is
    30mm" / "H = 20 mm" — the ``_extract_stated`` axis pass) and the
    equal-axis shorthand ("a 20 mm cube" — the ONLY form that fills
    three axes from one number). A number consumed here is MAPPED and
    never eligible for the tier-2 offer.

    The axis-letter forms REQUIRE the ``:``/``=`` marker or an explicit
    ``mm`` unit: "H is the axis you want" + "12 mm somewhere else" must
    NOT mark the 12 as consumed (issue #261 round 2 — the old optional
    ``[:]?``/bare-number shape let a stray letter followed by any number
    suppress a tier-2 offer)."""
    values: set[float] = set()
    for axis in ("W", "D", "H"):
        # Two explicit shapes: "H = 20" (the ``:``/``=`` marker — any
        # unit, the user meant the marker form) and "H 20 mm" (the
        # explicit mm unit, no marker — the ``_extract_stated`` pass
        # still reads this shape, so the offer helper must match it
        # for per-message consumption consistency). Anything else —
        # "H is the axis you want" + "12 mm somewhere" — does NOT
        # consume the 12 (issue #261 round 2).
        m = re.search(
            rf"\b{axis}\b\s*[:=]\s*(\d+(?:\.\d+)?)"
            r"|\b{axis}\b\s+(\d+(?:\.\d+)?)\s*mm\b",
            message,
            re.IGNORECASE,
        )
        if m:
            raw = m.group(1) if m.group(1) else m.group(2)
            f = float(raw)
            if f > 0:
                values.add(f)
    m = re.search(
        r"\b(?:a|an)\s+(\d+(?:\.\d+)?)"
        r"\s*mm\b"
        r"\s+(?:cube|box|sphere|ball)\b",
        message,
        re.IGNORECASE,
    )
    if m:
        f = float(m.group(1))
        if f > 0:
            values.add(f)
    # W×D×H triple-consumed numbers (issue #275 task-a): a triple's
    # numbers are MAPPED (consumed by the triple) and never eligible
    # for the tier-2 offer.
    _, triple_numbers = _extract_triple(message)
    values |= triple_numbers
    return values


def user_quoted_unmapped_mm(messages: list[str] | tuple[str, ...]) -> set[float]:
    """The user-quoted explicit-mm numbers no axis was ever assigned to
    (issue #261's tier-2 offer scan — the "user-quoted number the lexicon
    did not map is offered first" rule) — the tier-2 helper.

    Scans the project's recent user messages (the LAST
    ``QUOTED_UNMAPPED_MAX_MESSAGES`` — 50 — not just this turn). Only
    numbers written with an explicit ``mm`` unit count ("12 mm", "12mm",
    "12.5 mm" — no cm/in conversion, no bare numbers). A number is MAPPED
    — and excluded — when the closed axis lexicon
    (``d33d.axis_lexicon.classify``) or an explicit protocol cue in the
    SAME message assigned it to an axis; every other mm number is
    unmapped and eligible ("a 20 mm wide thing, lift it 12 mm" → {12.0} —
    the 20 is mapped to W, the 12 is not, and per-number evaluation is
    what makes the 12 eligible even though its clause held no axis word).
    """
    from d33d.axis_lexicon import classify

    unmapped: set[float] = set()
    for msg in list(messages)[-QUOTED_UNMAPPED_MAX_MESSAGES:]:
        text = str(msg)
        # Mapped by the lexicon: the mm numbers it assigned to an axis
        # (``classify(text).absolute`` — an axis-word clause with its
        # number). An mm number the lexicon saw but did NOT assign ("a
        # 15 mm hole") is unmapped either way.
        lexicon_absolute = classify(text).absolute
        lexicon_mapped: set[float] = set(lexicon_absolute.values())
        # Mapped by an explicit protocol cue in the SAME message
        # ("W: 42" / "a 20 mm cube" — the ``_extract_stated`` axis pass
        # and the equal-axis shorthand).
        mapped_by_protocol = _mm_cue_values(text)
        # Triple override: when a triple assigns an axis, the lexicon's
        # value for that axis (if different) is OVERRIDDEN and becomes
        # unmapped ("a 60 × 45 × 20 mm tray 40 mm wide" → W=60 from the
        # triple, so the lexicon's W=40 is unmapped and eligible for the
        # tier-2 offer).
        triple_axes, _ = _extract_triple(text)
        if triple_axes:
            for axis, tv in triple_axes.items():
                lv = lexicon_absolute.get(axis)
                if lv is not None and abs(lv - tv) > 1e-6:
                    lexicon_mapped.discard(lv)
        for n in re.findall(r"\b(\d+(?:\.\d+)?)" r"\s*mm\b", text):
            value = float(n)
            if value in lexicon_mapped:
                continue
            if any(abs(value - pv) < 1e-6 for pv in mapped_by_protocol):
                continue
            unmapped.add(value)
    return unmapped
