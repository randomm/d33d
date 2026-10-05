"""Feature-clause rules (issue #398, extracted from ``axis_lexicon``).

A pure leaf module for the "feature-clause" logic added on issue #398:
a clause introduced by a feature verb ("add a 3 mm wide, 2 mm deep
groove") or a bare measurement ("5 mm deep") is describing a FEATURE's
size, not the part's envelope, when the message has another clause with
a feature noun. Extracted as named functions so ``axis_lexicon`` stays
within its size budget.

Dependency direction: this module is a leaf. It takes its inputs
(absolute axis words, the feature-noun regex, and the number/word
helpers) as PARAMETERS, and it does NOT import ``axis_lexicon`` —
that direction is one-way, so importing ``feature_clause`` before or
after ``axis_lexicon`` works.

Public API (imported by ``axis_lexicon``):

- :func:`feature_clause_suppresses` — the cross-clause suppression test.
- :func:`message_has_feature_noun` — the pre-scan helper.
- ``_FEATURE_VERBS`` — the closed feature-verb set.
- ``_is_bare_measurement`` — the bare-measurement test.
"""

from __future__ import annotations

import re
from collections.abc import Callable

__all__ = [
    "_FEATURE_VERBS",
    "_is_bare_measurement",
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

_BARE_MEASUREMENT_RE = re.compile(
    r"\b(?:a|an|the|it|this|that|these|those|he|she|they|we|you|I|my|your|his|her|its|our|their)\b",
    re.IGNORECASE,
)


def _is_bare_measurement(clause: str) -> bool:
    """True if ``clause`` is a BARE MEASUREMENT — just a number + axis
    word (and optional filler like "mm", "around", "from", "the", etc.)
    with NO pronoun- or article-introduced subject (issue #398).

    A bare measurement is a fragment like "5 mm deep" or "10 mm wide"
    that modifies a feature described in a sibling clause, not a
    standalone part statement. The test: the clause contains no
    pronoun or article ("a", "an", "the", "it", "this", ...) that would
    introduce a subject. Verbs are NOT checked here — a feature-verb
    clause is handled by the feature-verb branch of the suppression
    (see ``axis_lexicon._classify_clause``), not by this test.
    """
    return _BARE_MEASUREMENT_RE.search(clause) is None


def message_has_feature_noun(
    clauses: list[str], feature_noun_re: re.Pattern[str]
) -> bool:
    """Pre-scan: does ANY clause in the message contain a feature noun?

    ``feature_noun_re`` is passed in (the closed feature-noun regex from
    ``axis_lexicon``) so this module stays a leaf. Used by the
    feature-verb cross-clause suppression (issue #398).
    """
    return any(feature_noun_re.search(c) for c in clauses)


def feature_clause_suppresses(
    clause: str,
    cue_words: list[str],
    *,
    feature_noun_in_message: bool,
    absolute_words: dict[str, str],
    numbers_in: Callable[[str], list[float]],
    word_re: Callable[[str], re.Pattern[str]],
    feature_verb_re: Callable[[str], re.Pattern[str]],
) -> bool:
    """Feature-verb cross-clause suppression (issue #398).

    A top-level clause (from ``split_clauses``, not a sub-clause from
    ``_split_on_and``) that contains an axis word + number but NO
    feature noun of its own is describing a feature whose noun lives in
    a SIBLING top-level clause. The suppression fires when:

    (a) the clause is introduced by a feature verb (add, cut, drill,
        bore, engrave, emboss) — "add a 3 mm wide, 2 mm deep groove"
        splits into ["add a 3 mm wide", "2 mm deep groove"]: clause 1
        has the verb + axis word + number, clause 2 has the noun.
        The 3 is the groove's width, not the part's.

    (b) the clause is a BARE MEASUREMENT — just the number + axis word
        (no article-introduced subject noun) — and the message has a
        feature noun in another top-level clause. "add a 10 mm wide
        slot across the top, 5 mm deep" splits into ["add a 10 mm
        wide slot across the top", "5 mm deep"]: clause 1 has the
        feature noun, clause 2 is a bare measurement ("5 mm deep").
        The 5 is the slot's depth, not the part's.

    The gate for BOTH: the message must have a feature noun in another
    top-level clause. This prevents false positives on part-level
    statements: "a 40 mm wide box with a 5 mm deep groove" is ONE
    top-level clause (the "with" split creates sub-clauses, not new
    top-level clauses), so the cross-clause rule does not fire and the
    box's width still states W=40. "make it 40 mm wide with a 5 mm
    hole" — "make" is not a feature verb and "a 5 mm hole" is in the
    same top-level clause, so the feature-noun guard handles it within
    the clause and the cross-clause rule doesn't fire.

    ``absolute_words`` / ``numbers_in`` / ``word_re`` / ``feature_verb_re``
    are passed in (rather than imported) to keep this module a leaf —
    the dependency direction to ``axis_lexicon`` is one-way.
    """
    return (
        feature_noun_in_message
        and set(cue_words) & set(absolute_words)
        and numbers_in(clause)
        and (
            any(feature_verb_re(v).search(clause) for v in _FEATURE_VERBS)
            or _is_bare_measurement(clause)
        )
    )
