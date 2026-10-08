"""Feature-clause rules (issue #398, narrowed on issue #413).

A pure leaf module for the "feature-clause" logic added on issue #398
and clause-scoped on issue #413: a sub-clause introduced by a feature
verb ("add a 3 mm wide, 2 mm deep groove") or a bare measurement ("5 mm
deep") is describing a FEATURE's size, not the part's envelope, when the
feature clause (the span from the first feature noun or feature-verb
sub-clause onward) covers it.

The suppression is CLAUSE-LOCAL: sub-clauses BEFORE the feature clause
(the part's own dimensions, e.g. "a 40 mm wide box" and "12 mm tall"
in "a 40 mm wide box, 12 mm tall, with a 5 mm hole") are never
suppressed, even when the message contains a feature noun later.

Extracted as named functions so ``axis_lexicon`` stays within its size
budget.

Dependency direction: this module is a leaf. It takes its inputs
(absolute axis words, the feature-noun regex, and the number/word
helpers) as PARAMETERS, and it does NOT import ``axis_lexicon`` —
that direction is one-way, so importing ``feature_clause`` before or
after ``axis_lexicon`` works.

Public API (imported by ``axis_lexicon``):

- :func:`feature_clause_suppresses` — the cross-clause suppression test.
- :func:`feature_clause_start` — the clause-local feature-clause boundary.
- :func:`message_has_feature_noun` — the pre-scan helper (kept for
  callers that only need a boolean).
- ``_FEATURE_VERBS`` — the closed feature-verb set.
- ``_is_bare_measurement`` — the bare-measurement test.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = [
    "_FEATURE_VERBS",
    "_is_bare_measurement",
    "feature_clause_start",
    "feature_clause_suppresses",
    "message_has_feature_noun",
]

#: The feature verbs (closed set, issue #398): verbs that create or modify
#: a FEATURE on the part. A clause introduced by one of these that contains
#: an axis word + number is describing the feature's size, not the part's
#: envelope. The cross-clause check (the message must have another clause
#: with a feature noun) prevents false positives on part-level statements
#: like "make it 40 mm wide with a 5 mm hole" ("make" is not a feature
#: verb, and even if it were, "hole" alone in the same clause would be
#: caught by the feature-noun guard). "make" is deliberately NOT in this
#: set: "make a 30 mm wide block" still states W=30.
_FEATURE_VERBS: frozenset[str] = frozenset(
    {"add", "cut", "drill", "bore", "engrave", "emboss"}
)

def _is_bare_measurement(clause: str) -> bool:
    """True if ``clause`` is a BARE MEASUREMENT — just a number + axis
    word (and optional filler like "mm", "around", "from", "the", etc.)
    with NO pronoun- or article-introduced subject (issue #398).

    A bare measurement is a fragment like "5 mm deep" or "10 mm wide"
    that modifies a feature described in a sibling clause, not a
    standalone part statement. The test: the clause contains no
    pronoun or article ("a", "an", "it", "this", ...) that would
    introduce a subject. "the" is NOT a subject introducer in this
    context — "8 mm tall on the top" is a bare measurement (the "the"
    refers to the feature's top, not a new subject).
    """
    return not re.search(
        r"\b(?:a|an|it|this|that|these|those|he|she|they|we|you|I|my|your|his|her|its|our|their)\b",
        clause,
        re.IGNORECASE,
    )


def feature_clause_start(
    clauses: list[str],
    feature_noun_re: re.Pattern[str],
    numbers_in: Callable[[str], list[float]],
) -> int:
    """The 0-based index of the sub-clause where the FEATURE CLAUSE
    begins (issue #413), or ``-1`` when the message has no feature
    clause.

    The feature clause starts at the EARLIEST sub-clause that:

    - contains a feature noun ("with a 5 mm hole", "a 10 mm wide cable
      slot", "a 10 mm drain hole"), OR
    - is introduced by a feature verb ("add a 3 mm wide", "drill an 8
      mm deep", "add a boss 12 mm wide") and carries a number — the
      verb clause describes the feature even when its noun lives in a
      later sub-clause ("add a 10 mm wide slot across the top, 5 mm
      deep").

    Sub-clauses after the returned index are part of the feature clause
    (they continue describing that feature's size or position); earlier
    sub-clauses are the part's own dimensions and are never suppressed.
    """
    for i, c in enumerate(clauses):
        if feature_noun_re.search(c):
            return i
    for i, c in enumerate(clauses):
        if any(
            re.search(rf"(?<!\w){re.escape(v)}(?!\w)", c, re.IGNORECASE)
            for v in _FEATURE_VERBS
        ) and numbers_in(c):
            return i
    return -1


def message_has_feature_noun(
    clauses: list[str], feature_noun_re: re.Pattern[str]
) -> bool:
    """Pre-scan: does ANY clause in the message contain a feature noun?

    ``feature_noun_re`` is passed in (the closed feature-noun regex from
    ``axis_lexicon``) so this module stays a leaf. Used by callers
    that only need a boolean (issue #398); the clause-local version is
    :func:`feature_clause_start` (issue #413).
    """
    return any(feature_noun_re.search(c) for c in clauses)


def _feature_noun_in_feature_context(clause: str, feature_noun_re: re.Pattern[str]) -> bool:
    """True if the clause contains a feature noun in a "with a/an" or
    "in a/an" phrase (issue #413).

    This is used by the in-clause feature-noun guard to suppress the
    clause's absolute cue when the feature noun is a SUBORDINATE FEATURE
    (introduced by "with a/an" or "in a/an"). The cross-clause guard
    handles the case where the feature noun is the HEAD NOUN of the
    clause.
    """
    m = feature_noun_re.search(clause)
    if m is None:
        return False
    prefix = clause[: m.start()]
    return bool(
        re.search(r"\b(?:with|in)\s+(?:a|an)\b", prefix, re.IGNORECASE)
    )


def feature_clause_suppresses(
    clause: str,
    cue_words: list[str],
    *,
    sub_clause_index: int,
    feature_clause_start: int,
    absolute_words: dict[str, str],
    numbers_in: Callable[[str], list[float]],
    word_re: Callable[[str], re.Pattern[str]],
    feature_verb_re: Callable[[str], re.Pattern[str]],
    feature_noun_re: re.Pattern[str] | None = None,
) -> bool:
    """Feature-clause cross-clause suppression (issue #398, narrowed on
    #413).

    A sub-clause that contains an axis word + number but NO feature noun
    of its own is describing a feature when the message has a feature
    clause AND the sub-clause is AT or AFTER the feature clause's start.

    Two shapes (both from issue #398, both still valid):

    (a) the clause is introduced by a feature verb (add, cut, drill,
        bore, engrave, emboss) — "add a 3 mm wide, 2 mm deep groove"
        splits into ["add a 3 mm wide", "2 mm deep groove"]: clause 1
        has the verb + axis word + number, clause 2 has the noun.
        The 3 is the groove's width, not the part's.

    (b) the clause is a BARE MEASUREMENT — just the number + axis word
        (no article-introduced subject noun) — and the feature clause
        starts at or before this sub-clause. "add a 10 mm wide slot
        across the top, 5 mm deep" splits into ["add a 10 mm wide
        slot across the top", "5 mm deep"]: clause 1 has the feature
        noun, clause 2 is a bare measurement ("5 mm deep"). The 5 is
        the slot's depth, not the part's.

    The gate (narrowed on issue #413): the message must have a feature
    clause, AND the clause being evaluated must be at or after the
    feature clause's start index. Sub-clauses BEFORE the feature clause
    (the part's own dimensions) are never suppressed, even when the
    message contains a feature noun later.

    This is the fix for the #413 regression: "a 40 mm wide box, 12 mm
    tall, with a 5 mm hole" splits into ["a 40 mm wide box", "12 mm
    tall", "with a 5 mm hole"]; the feature clause starts at index 2
    ("with a 5 mm hole"), so "12 mm tall" (index 1) is NOT suppressed
    and states H=12. Before #413, the gate was message-wide ("does ANY
    clause have a feature noun?"), so "12 mm tall" was swallowed.

    ``absolute_words`` / ``numbers_in`` / ``word_re`` / ``feature_verb_re``
    are passed in (rather than imported) to keep this module a leaf —
    the dependency direction to ``axis_lexicon`` is one-way.
    ``feature_clause_start`` is passed in from
    ``axis_lexicon._classify_clause`` (the caller knows where the
    feature clause begins in the full message).
    """
    has_feature_verb = any(
        feature_verb_re(v).search(clause) for v in _FEATURE_VERBS
    )
    # A feature verb followed by a part-referencing pronoun ("it", "this",
    # "that") is a part-level statement, not a feature clause:
    # "cut it to 15 mm tall" → cutting the part's height, not a feature.
    if has_feature_verb:
        for v in _FEATURE_VERBS:
            m = feature_verb_re(v).search(clause)
            if m:
                after_verb = clause[m.end():].strip()
                if after_verb:
                    first_word_after = after_verb.split()[0].lower()
                    if first_word_after in ("it", "this", "that"):
                        return False  # part-level statement, not a feature
    has_feature_noun = feature_noun_re is not None and feature_noun_re.search(clause)
    return (
        feature_clause_start >= 0
        and (
            sub_clause_index >= feature_clause_start
            or has_feature_verb
        )
        and set(cue_words) & set(absolute_words)
        and numbers_in(clause)
        and (
            has_feature_verb
            or _is_bare_measurement(clause)
        )
        and (has_feature_verb or not has_feature_noun)
    )
