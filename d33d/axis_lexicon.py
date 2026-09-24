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
split on "and" ONLY when every resulting part contains its own number;
otherwise the clause stays whole, and a whole clause with two or more
different axis words and one number maps nothing.

Precedence (caller's responsibility, not the lexicon's):
  explicit stated_dims body field > explicit protocol cues > lexicon.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

__all__ = ["Cues", "axis_for_question_word", "classify"]

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

_RELATIVE: dict[str, str] = {
    "taller": "H",
    "shorter": "H",
    "higher": "H",
    "lower": "H",
    "wider": "W",
    "narrower": "W",
    "deeper": "D",
    "shallower": "D",
}

_GLOBAL: frozenset[str] = frozenset(
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

# All axis words (absolute + relative) for clause-level detection.
_ALL_AXIS_WORDS: frozenset[str] = frozenset(_ABSOLUTE) | frozenset(_RELATIVE)

# ---------------------------------------------------------------------------
# Compiled patterns
# ---------------------------------------------------------------------------

# An explicit-mm number: "12 mm", "12mm", "12.5 mm", "12.5mm".
_MM_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*mm\b")

# A bare number (no mm unit) for adjacency checks.
_BARE_NUMBER_RE = re.compile(r"\b(\d+(?:\.\d+)?)\b")

# An absolute axis word at a word boundary (underscore is a word char,
# so "height" inside "spacer_height" does NOT match).
def _word_re(word: str) -> re.Pattern[str]:
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


def _has_number(fragment: str) -> bool:
    """True if the fragment contains at least one number (with or without
    an mm unit)."""
    return _MM_NUMBER_RE.search(fragment) is not None or _BARE_NUMBER_RE.search(
        fragment
    ) is not None


def _axis_words_in(text: str) -> set[str]:
    """The set of axis words (absolute or relative) present in text."""
    found: set[str] = set()
    low = text.lower()
    for word, axis in _ABSOLUTE.items():
        if _word_re(word).search(text):
            found.add(word)
    for word in _RELATIVE:
        if _word_re(word).search(text):
            found.add(word)
    return found


def _numbers_in(text: str) -> list[float]:
    """All numbers (with or without mm unit) in text."""
    return [float(m) for m in _BARE_NUMBER_RE.findall(text)]


def _mm_numbers_in(text: str) -> list[float]:
    """All explicit-mm numbers in text."""
    return [float(m) for m in _MM_NUMBER_RE.findall(text)]


def _split_on_and(clause: str) -> list[str]:
    """Split a clause on "and" ONLY when every resulting part contains
    its own number. Otherwise the clause stays whole."""
    # Find all "and" positions (word-boundary, case-insensitive).
    and_re = re.compile(r"\band\b", re.IGNORECASE)
    matches = list(and_re.finditer(clause))
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
    absolute: dict[str, float] = {}
    relative: set[str] = set()
    global_: bool = False
    cue_words: list[str] = []

    low = clause.lower()

    # Check global cues (multi-word phrases first, then single words).
    for phrase in sorted(_GLOBAL, key=len, reverse=True):
        if _word_re(phrase).search(clause):
            global_ = True
            cue_words.append(phrase)
            break  # one global cue is enough

    # Check axis words (absolute and relative) in this clause.
    for word, axis in _ABSOLUTE.items():
        if _word_re(word).search(clause):
            absolute[word] = 0.0  # placeholder, filled below
            cue_words.append(word)

    for word, axis in _RELATIVE.items():
        if _word_re(word).search(clause):
            relative.add(axis)
            cue_words.append(word)

    # Find all numbers in the clause.
    all_numbers = _numbers_in(clause)
    mm_numbers = _mm_numbers_in(clause)

    # Determine the axis-word count in this clause.
    # Count distinct axis words (absolute + relative).
    axis_words_found: set[str] = set()
    for word in _ABSOLUTE:
        if _word_re(word).search(clause):
            axis_words_found.add(word)
    for word in _RELATIVE:
        if _word_re(word).search(clause):
            axis_words_found.add(word)
    axis_count = len(axis_words_found)

    # If there are two or more different axis words and one number,
    # the clause maps nothing (the two-axes-one-number rule).
    if axis_count >= 2 and len(all_numbers) <= 1:
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
                for num_match in _BARE_NUMBER_RE.finditer(clause):
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


def axis_for_question_word(word: str) -> str | None:
    """The axis for a question word (absolute words only).

    "how tall is it?" → "H"; "how wide is it?" → "W";
    "how deep is it?" → "D". Relative/global/excluded/unknown → None.
    """
    low = word.strip().lower()
    return _ABSOLUTE.get(low)
