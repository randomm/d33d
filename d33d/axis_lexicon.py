"""Closed axis lexicon (issue #261, task-a).

A pure, deterministic, no-LLM leaf module that classifies each clause of a
user message into absolute / relative / global / no axis cues for W/D/H.

Word sets (closed):

- Absolute axis words: H = tall, high, height; W = wide, width;
  D = deep, depth.
- Relative cues: H = taller, shorter, higher, lower; W = wider, narrower;
  D = deeper, shallower.
- Global cues: bigger, smaller, scale, scaled, resize, resized,
  "half the size", "twice the size".
- EXCLUDED (pinned by tests): long/length, thick/thickness,
  diameter/Ø/bore, and verbs such as lift, sit, reach, clear.

Absolute cue: a number with an mm unit, or a bare number directly
adjacent to the axis word, in the same clause as exactly one axis word.
Clauses split on sentence punctuation, commas, and ";". Within a clause,
split on "and" (or "with", under the same rule) ONLY when every resulting
part contains its own number; otherwise the clause stays whole, and a
whole clause with two or more different axis words and one number maps
nothing.

Feature nouns: a clause that contains a FEATURE NOUN (the closed
``_FEATURE_NOUNS`` set — hole, groove, foot, …) never produces an
ABSOLUTE axis cue: its number is a feature size ("a 10 mm deep hole" is
a hole, not a 10 mm part), so it goes to ``unmapped_mm_numbers`` (tier-2
offer territory) instead of setting an axis. Relative and global cues
are NOT affected by feature nouns ("make the hole deeper" still
releases D — a release only stops enforcement, so this is the
conservative choice).

Precedence (caller's responsibility, not the lexicon's):
  explicit stated_dims body field > explicit protocol cues > lexicon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = [
    "GLOBAL_WORDS",
    "RELATIVE_WORDS",
    "Cues",
    "axis_for_question_word",
    "classify",
]

# ---------------------------------------------------------------------------
# Closed word sets
# ---------------------------------------------------------------------------

_ABSOLUTE: dict[str, str] = {
    "tall": "H",
    "high": "H",
    "height": "H",
    "wide": "W",
    "width": "W",
    "deep": "D",
    "depth": "D",
}

RELATIVE_WORDS: dict[str, str] = {
    "taller": "H",
    "shorter": "H",
    "higher": "H",
    "lower": "H",
    "wider": "W",
    "narrower": "W",
    "deeper": "D",
    "shallower": "D",
}

GLOBAL_WORDS: frozenset[str] = frozenset(
    {
        "bigger",
        "smaller",
        "scale",
        "scaled",
        "resize",
        "resized",
        "half the size",
        "twice the size",
    }
)

_RELATIVE = RELATIVE_WORDS
_GLOBAL = GLOBAL_WORDS

# Words that must NOT trigger any axis (pinned by tests).
_EXCLUDED: frozenset[str] = frozenset(
    {
        "long",
        "length",
        "thick",
        "thickness",
        "diameter",
        "bore",
        "lift",
        "sit",
        "reach",
        "clear",
    }
)

# Feature nouns (closed set, issue #261 fix batch): the parts of a design
# that are NOT the part's envelope. A clause containing any of these words
# (whole-word, case-insensitive; singular and plural as listed) never
# produces an ABSOLUTE axis cue — "a 5 mm deep groove" states nothing
# about the part's depth; the 5 is a feature size and belongs in
# ``unmapped_mm_numbers`` (the tier-2 offer). Relative and global cues
# still work in feature clauses ("make the hole deeper" releases D —
# a release only stops enforcement, never sets a wrong value).
_FEATURE_NOUNS: frozenset[str] = frozenset(
    {
        "hole",
        "holes",
        "groove",
        "slot",
        "pocket",
        "bore",
        "recess",
        "notch",
        "channel",
        "cutout",
        "cut-out",
        "counterbore",
        "countersink",
        "foot",
        "feet",
        "leg",
        "legs",
        "post",
        "tab",
        "lip",
        "rim",
        "rib",
        "boss",
        "peg",
        "pin",
        "screw",
        "bolts",
        "magnet",
        "magnets",
        "spacer",
        "spacers",
        "grid",
        "grids",
        "lid",
        "wall",
        "walls",
        "chamfer",
        "fillet",
        "text",
        "label",
        "logo",
    }
)

_FEATURE_NOUN_RE = re.compile(
    r"(?<!\w)(?:" + "|".join(_FEATURE_NOUNS) + r")(?!\w)", re.IGNORECASE
)

# All axis words (absolute + relative) for clause-level detection.
_ALL_AXIS_WORDS: frozenset[str] = frozenset(_ABSOLUTE) | frozenset(RELATIVE_WORDS)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# An explicit-mm number: "12 mm", "12mm", "12.5 mm", "12.5mm".
_MM_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm\b")

# The SHARED number token (issue #275 task-a): one number followed by an
# OPTIONAL millimetre unit — the bare form ("20", "40.5") or an mm unit
# ("40mm", "40 mm", "40.5mm", "40 millimetres", "40 millimeter").
# Written as an explicit alternative (the unit part cannot start with a
# digit, so the bare form never misfires on a unit-leading string):
#   - "40mm wide" matches as (40, "mm") — the no-space mm bug that made
#     the clause look number-less and tripped the release fallback;
#   - a spelled-out unit ("42 millimeter thing") matches as
#     (42, " millimeter") so it counts as a number AND as an explicit-mm
#     number (the #91 guard: single number, no axis word → unmapped).
#
# Backs EVERY number-presence / number-extraction site in this module —
# ``_has_number``, ``_numbers_in``, ``classify``'s ``has_any_number``
# release guard, and the adjacency scan in ``_classify_clause`` — leaving
# any site on the old bare pattern re-creates the no-space mm bug at that
# site (a clause that looks number-less releases its axis instead of
# stating it).
# Note: the spelled-out unit deliberately does NOT touch ``_FOREIGN_UNIT_RE``
# — its "m" alternative requires the number to be directly adjacent
# ("5 m"), and "5 millimetres" has a word boundary between "m" and the
# "m" of "millimetres", so the foreign check never fires on the new unit.
_NUMBER_TOKEN_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)"
    r"((?:\s*mm|\s+millimetres?|\s+millimeters?|\s*millimetre)?)"
)

# A bare number (no mm unit) for adjacency checks.
# Kept for reference; the shared token above subsumes it (see
# ``_NUMBER_TOKEN_RE``).
_BARE_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")


def _word_re(word: str) -> re.Pattern[str]:
    """An absolute axis word at a word boundary (underscore is a word
    char, so "height" inside "spacer_height" does NOT match)."""
    return re.compile(rf"(?<!\w){re.escape(word)}(?!\w)", re.IGNORECASE)


# ---------------------------------------------------------------------------
# Public data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Cues:
    """The result of classifying one message.

    - ``absolute``: axes stated by an absolute cue, with the value in mm.
    - ``relative``: axes released by a relative cue.
    - ``global_``: True if a global cue is present (releases all three).
    - ``cue_words``: the lexicon tokens found in the message, in order of
      first appearance (for tier-1 offer cue extraction).
    - ``unmapped_mm_numbers``: explicit-mm numbers not assigned to any
      axis by the lexicon (eligible for tier-2 offer).
    """

    absolute: dict[str, float] = field(default_factory=dict)
    relative: set[str] = field(default_factory=set)
    global_: bool = False
    cue_words: list[str] = field(default_factory=list)
    unmapped_mm_numbers: list[float] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Clause splitting
# ---------------------------------------------------------------------------

_CLAUSE_SPLIT_RE = re.compile(r"[,;]|[.!?]\s")


def _split_clauses(message: str) -> list[str]:
    """Split a message into clauses on sentence punctuation, commas,
    and semicolons."""
    parts = _CLAUSE_SPLIT_RE.split(message)
    return [p.strip() for p in parts if p.strip()]


#: A foreign length unit (cm, inch/in, m) attached to a number (with a
#: word boundary on both sides so "cmm"/"imm" does not match): a clause
#: holding one does not assign an absolute axis (the operator decision
#: "no cm/in conversion" applies to STATEMENTS, not to feature sizes —
#: a clause with "5 cm" neither maps an axis nor leaks its number into
#: tier-2, which is mm-only). Issue #261 round 2: without this, a user
#: writing "make it 5 cm tall" was assigned H=5 (a 10×-wrong statement)
#: because the bare 5 was adjacent to the axis word.
_FOREIGN_UNIT_RE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:cm|in|inches|inch|m)\b")


def _has_number(fragment: str) -> bool:
    """True if the fragment contains at least one number (with or without
    an mm unit). Uses the shared number token (``_NUMBER_TOKEN_RE``) so a
    no-space mm number like "40mm" counts as a number — the old bare
    pattern missed "40mm" (no word boundary between digit and m) and the
    clause looked number-less, tripping the release fallback.

    A foreign-unit number ("5 cm") is NOT a shared-token number: the
    operator decision for the release guard says only shared-token
    numbers count, so "make it 5 cm wider" still releases W."""
    for m in _NUMBER_TOKEN_RE.finditer(fragment):
        if m.group(2):
            return True  # mm unit → shared-token number
        # No mm unit: check if this number is part of a foreign-unit
        # match. If the number starts a foreign-unit match, skip it.
        # _FOREIGN_UNIT_RE has no capture group, so use group(0) and
        # check if the foreign match starts with the number text.
        foreign_m = _FOREIGN_UNIT_RE.match(fragment[m.start():])
        if foreign_m:
            number_text = fragment[m.start(1):m.end(1)]
            if foreign_m.group(0).startswith(number_text):
                continue  # foreign-unit number, not a shared-token number
        return True
    return False


def _numbers_in(text: str) -> list[float]:
    """All numbers (with or without mm unit) in text, using the shared
    number token."""
    return [float(m.group(1)) for m in _NUMBER_TOKEN_RE.finditer(text)]


def _mm_numbers_in(text: str) -> list[float]:
    """All explicit-mm numbers in text (the shared number token with a
    spelled-out mm unit — "40mm", "40 mm", "40 millimetres")."""
    out: list[float] = []
    for m in _NUMBER_TOKEN_RE.finditer(text):
        if m.group(2):
            out.append(float(m.group(1)))
    return out


# The "and"/"with" joiners: split ONLY when every resulting part contains
# its own number (the same rule for both — "a 40 mm wide box with a
# 5 mm deep groove" splits into two single-number clauses; "20 mm wide
# and tall" does not split because "tall" carries no number).
_CLAUSE_JOINER_RE = re.compile(r"\b(?:and|with)\b", re.IGNORECASE)


def _split_on_and(clause: str) -> list[str]:
    """Split a clause on "and"/"with" ONLY when every resulting part
    contains its own number. Otherwise the clause stays whole."""
    matches = list(_CLAUSE_JOINER_RE.finditer(clause))
    if not matches:
        return [clause]

    # Build candidate parts by splitting on each "and".
    parts: list[str] = []
    prev_end = 0
    for m in matches:
        parts.append(clause[prev_end : m.start()])
        prev_end = m.end()
    parts.append(clause[prev_end:])

    # Check: every part must contain its own number for the split to apply.
    if len(parts) > 1 and all(_has_number(p.strip()) for p in parts):
        return [p.strip() for p in parts if p.strip()]

    return [clause]


# ---------------------------------------------------------------------------
# Clause-level classification
# ---------------------------------------------------------------------------


def _classify_clause(clause: str) -> tuple[dict[str, float], set[str], bool, list[str]]:
    """Classify one clause. Returns (absolute, relative, global_, cue_words)."""
    relative: set[str] = set()
    global_: bool = False
    cue_words: list[str] = []

    # Check global cues (multi-word phrases first, then single words).
    for phrase in sorted(GLOBAL_WORDS, key=len, reverse=True):
        if _word_re(phrase).search(clause):
            global_ = True
            cue_words.append(phrase)
            break  # one global cue is enough

    # Check axis words (absolute and relative) in this clause.
    for word in _ABSOLUTE:
        if _word_re(word).search(clause):
            cue_words.append(word)

    for word, axis in RELATIVE_WORDS.items():
        if _word_re(word).search(clause):
            relative.add(axis)
            cue_words.append(word)

    # Feature-noun clauses (issue #261): "a 5 mm deep groove" / "a hole
    # 10 mm deep" / "12 mm high feet" state nothing about the part's
    # axes — the number is the feature's size, not the part's dimension.
    # The clause still contributes its relative/global cues and cue words
    # ("make the hole deeper" releases D — a release only stops
    # enforcement, so keeping releases in feature clauses is the
    # conservative choice); the mm number falls out into
    # unmapped_mm_numbers via the normal unmapped scan below.
    if _FEATURE_NOUN_RE.search(clause):
        return ({}, relative, global_, cue_words)

    # Find all numbers in the clause.
    all_numbers = _numbers_in(clause)

    # Determine the axis-word count in this clause.
    # Count distinct axis words (absolute or relative).
    axis_words_found = _ALL_AXIS_WORDS & set(cue_words)

    # If there are two or more different axis words and one number,
    # the clause maps nothing (the two-axes-one-number rule).
    if len(axis_words_found) >= 2 and len(all_numbers) <= 1:
        return ({}, set(), global_, cue_words)

    # A clause with a foreign-unit number (cm/in/m) does not assign an
    # absolute axis: no unit conversion is the operator decision, and
    # assigning the raw number would fabricate a 10×-wrong statement
    # ("5 cm tall" → H=5). Relative/global cues in the same clause are
    # kept (a "make it 5 cm taller"-style message still releases H).
    if axis_words_found & set(_ABSOLUTE) and _FOREIGN_UNIT_RE.search(clause):
        # A foreign-unit number (cm/in/m) does not assign an absolute axis
        # AND does not RELEASE the axis either (the operator decision: the
        # release guard only fires when no shared-token number is present.
        # "make it 5 cm taller" abstains — it does not set H=5, and it
        # does not release H either; the carried H is simply carried
        # forward, which is the honest outcome for a statement the system
        # cannot measure). "make it 5 cm taller" (relative cue) is the
        # exception: the relative cue still releases H, because the user
        # asked to change H. But "make it 5 cm tall" (absolute cue) does
        # not release H — the release fallback is gated on has_any_number,
        # and the foreign-unit number does NOT count as a shared-token
        # number, so the fallback WOULD fire and release H. To prevent
        # this, we clear the relative set when a foreign-unit number is
        # present and there are absolute axis words: the abstention is
        # total (no set, no release). This preserves the #261 round-2
        # foreign-unit semantics: "5 cm tall" → H neither set nor released.
        return ({}, set(), global_, cue_words)

    # Assign numbers to absolute axes.
    # An absolute axis word with an mm number or a bare number adjacent
    # to it becomes an absolute cue.
    absolute_result: dict[str, float] = {}
    for word in axis_words_found:
        if word not in _ABSOLUTE:
            continue  # relative word, handled separately
        axis = _ABSOLUTE[word]
        if len(all_numbers) == 1:
            # Single number: assign to this absolute axis word.
            absolute_result[axis] = all_numbers[0]
        else:
            # Multiple numbers: look for the number closest to the word.
            word_match = _word_re(word).search(clause)
            if word_match is not None:
                word_pos = word_match.start()
                best_num: float | None = None
                best_dist: float = float("inf")
                for num_match in _NUMBER_TOKEN_RE.finditer(clause):
                    num_pos = num_match.start()
                    dist = abs(num_pos - word_pos)
                    if dist < best_dist:
                        best_dist = dist
                        best_num = float(num_match.group(1))
                if best_num is not None and best_dist <= 10:
                    absolute_result[axis] = best_num

    return (absolute_result, relative, global_, cue_words)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def classify(message: str) -> Cues:
    """Classify a user message into axis cues.

    Returns a :class:`Cues` with the absolute axes, relative axes,
    global flag, cue words, and unmapped mm numbers.

    A relative/global cue is also emitted when the message carries NO
    absolute axis word at all and holds no number (a pure direction
    request like "increase the height" or "make it 20% taller" — the
    operator decision "no cm/in conversion" left such phrasings outside
    the closed word sets, which left the gate enforcing the carried old
    axis against a user who asked to change it — issue #261 round 2).
    The emitted cue releases the axis in ``effective_stated_dims`` and
    feeds the tier-1 offer, without setting any axis value. A message
    that DOES carry an absolute axis word (or a number, which the
    number-mapping needs) keeps the strict behavior: "increase the
    height to 30 mm" sets H=30 without releasing it.
    """
    clauses = _split_clauses(message)

    all_absolute: dict[str, float] = {}
    all_relative: set[str] = set()
    all_global: bool = False
    all_cue_words: list[str] = []
    all_mapped_numbers: set[float] = set()

    for clause in clauses:
        # Try splitting on "and" within this clause.
        sub_clauses = _split_on_and(clause)
        for sub in sub_clauses:
            abs_c, rel_c, glob_c, words_c = _classify_clause(sub)
            for axis, val in abs_c.items():
                all_absolute[axis] = val
                all_mapped_numbers.add(val)
            all_relative |= rel_c
            all_global |= glob_c
            for w in words_c:
                if w not in all_cue_words:
                    all_cue_words.append(w)

    has_any_number = _has_number(message)
    has_percent: bool = False
    # Scan for a bare "%" (the number is already filtered out — a
    # percentage is not an axis measurement, "20% taller" must not
    # set H=20).
    if not has_any_number:
        for clause in clauses:
            if "%" in clause:
                has_percent = True
                break

    # The #261 round-2 release: a pure direction request (no absolute
    # value assigned, no number present, or only a percentage number)
    # still releases the carried axes via a named cue ("increase the
    # height", "20% taller"). The fallback only fires when the message
    # did NOT state an absolute value: "increase the height to 30 mm"
    # sets H=30 and carries the rest — it does not ALSO release H.
    # A foreign-unit number (cm/in/m) does NOT count as a shared-token
    # number, so _has_number is False — but a foreign-unit message must
    # NOT release its axes either ("make it 5 cm tall" abstains: H is
    # neither set nor released, the carried value carries forward). The
    # foreign-unit check suppresses the fallback for foreign-unit messages
    # that carry an absolute axis word (the release guard: only shared-
    # token numbers count; a foreign-unit number does not).
    has_foreign_unit = _FOREIGN_UNIT_RE.search(message) is not None
    if not all_absolute and not (has_any_number and not has_percent) and not has_foreign_unit:
        all_global = all_global or _apply_release_fallback(message, all_relative)
    elif not all_absolute and not (has_any_number and not has_percent) and has_foreign_unit:
        # Foreign-unit message: the fallback would fire (no shared-token
        # number present) but must NOT release axes. The relative set is
        # left as-is (empty for absolute-word messages, populated for
        # relative-word messages like "make it 5 cm taller").
        pass

    # Collect all explicit-mm numbers in the message.
    all_mm_numbers = _mm_numbers_in(message)

    # Unmapped mm numbers: explicit-mm numbers not in the mapped set.
    unmapped: list[float] = []
    seen: set[float] = set()
    for n in all_mm_numbers:
        if n not in seen:
            seen.add(n)
            if not any(abs(n - m) < 1e-9 for m in all_mapped_numbers):
                unmapped.append(n)

    return Cues(
        absolute=all_absolute,
        relative=all_relative,
        global_=all_global,
        cue_words=all_cue_words,
        unmapped_mm_numbers=unmapped,
    )


def _global_release(message: str) -> bool:
    """True if the message carries a global cue (any of ``GLOBAL_WORDS``)."""
    for phrase in sorted(GLOBAL_WORDS, key=len, reverse=True):
        if _word_re(phrase).search(message):
            return True
    return False


def _apply_release_fallback(message: str, released: set[str]) -> bool:
    """The #261 round-2 pure-direction-request release (extracted from
    :func:`classify` so it can be tested directly).

    When a message states NO absolute value, its relative and absolute
    axis words still RELEASE their axes ("increase the height" releases
    H — the gate must not enforce the carried H against a user who asked
    to change it), and a global word releases all three. Returns True if
    the global flag should be set. ``unmapped_mm_numbers`` is message-level
    by design (issue #261's "per number" decision): an explicit-mm number
    is eligible for the tier-2 offer if no clause in the message mapped it,
    regardless of which clause it sits in.
    """
    for word, axis in RELATIVE_WORDS.items():
        if _word_re(word).search(message):
            released.add(axis)
    for word, axis in _ABSOLUTE.items():
        if _word_re(word).search(message):
            released.add(axis)
    return _global_release(message)


def axis_for_question_word(word: str) -> str | None:
    """The axis for a question word (absolute words only).

    "how tall is it?" → "H"; "how wide is it?" → "W";
    "how deep is it?" → "D". Relative/global/excluded/unknown → None.
    """
    low = word.strip().lower()
    return _ABSOLUTE.get(low)
