"""Per-message stated-dimension extraction (issue #393).

The pure text-extraction half of the dimension protocol: ``_extract_stated``
and its private helpers, the per-message wrappers
(``stated_axes_from_message`` / ``stated_dims_from_message``), and the
confirmation-text helpers (``_is_clean_affirmation`` /
``_last_user_turn`` / ``_confirmed_suggestion_tokens``).

Carry-forward resolution lives in :mod:`d33d.stated_carry`.
Import graph (unidirectional): ``axis_lexicon`` -> ``triple_extraction``
-> this module -> ``stated_carry`` -> ``dimension_protocol`` (never back).
``DIMENSION_AXES`` lives in ``d33d.axis_lexicon`` (the bottom of the
import graph, issue #393).
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from functools import cache
from typing import Any

from d33d.axis_lexicon import (
    ABSOLUTE_WORDS,
    DIMENSION_AXES,
    FEATURE_NOUN_RE,
    MM_UNIT_ALTERNATION,
    RELATIVE_WORDS,
    Cues,
    classify,
    split_clauses,
)
from d33d.triple_extraction import (
    _extract_triple,
    history_window_start,
)

logger = logging.getLogger(__name__)

__all__ = [
    "StatedFeature",
    "stated_axes_from_message",
    "stated_dims_from_message",
    "stated_features_from_messages",
]

#: The role words a stated feature clause names the size with (issue #433):
#: the closed role-token vocabulary that maps a feature size to a
#: parameter. ``_FEATURE_NOUNS`` (``d33d.axis_lexicon``) names the feature;
#: this set names the ROLE of the stated size (a hole's diameter, a
#: groove's depth). Closed and small on purpose: an unmatched clause
#: records ``role=None`` and the matching in :mod:`d33d.design_state`
#: falls through (a stated value the param matcher cannot attribute is
#: kept in state, never flagged).
_FEATURE_ROLE_WORDS: frozenset[str] = frozenset(
    {"diameter", "dia", "wide", "deep", "long", "width", "depth"}
)

_FEATURE_ROLE_RE = re.compile(
    r"(?<!\w)(?:"
    + "|".join(sorted(_FEATURE_ROLE_WORDS, key=len, reverse=True))
    + r")(?!\w)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class StatedFeature:
    """One stated feature size (issue #433): the user said ``a 5 mm hole``
    and the model later picks ``hole_diameter = 8`` — the 5 is the stated
    value, the 8 the made value.

    ``noun`` is the feature noun the clause named (``"hole"``) and ``role``
    the size role the clause used (``"diameter"``), or ``None`` when the
    clause stated a size without a role word (``a 5 mm hole`` alone — the
    noun's default size). ``value`` is the explicit-mm value. The record is
    the dimension protocol's FEATURE equivalent of the per-axis stated
    set: it is extracted from the user's words, carried across turns, and
    NEVER a W/D/H axis (the #398/#413 axis guard is untouched — the
    feature clause's number stays unmapped for the axis extraction).
    """

    noun: str
    role: str | None
    value: float


#: The lexicon's explicit-mm number token, anchored for span matching:
#: one mm number followed by the lexicon's mm unit alternative. The
#: number is captured as the USER'S LITERAL TEXT — every number→clause
#: lookup matches the token text directly and compares
#: ``float(text) == number`` (never the lossy ``f"{number:g}"``
#: re-render, which would miss ``20.50`` / ``1e-05`` / ``10000000``).
_MM_NUMBER_SPAN_RE = re.compile(
    r"(?<![-\d.])" r"(\d+(?:\.\d+)?(?:[eE][+-]?\d+)?)(?:" + MM_UNIT_ALTERNATION + r")\b",
    re.IGNORECASE,
)


def _mm_number_occurrences(clause: str) -> list[re.Match[str]]:
    """The clause's explicit-mm number tokens (``_MM_NUMBER_SPAN_RE``
    matches), as raw matches — the caller compares
    ``float(m.group(1)) == number`` against the parsed value it holds."""
    return list(_MM_NUMBER_SPAN_RE.finditer(clause))


def _delta_marker_at(number: float, clause: str) -> bool:
    """Whether ``clause`` carries a RELATIVE-delta marker ANCHORED to
    ``number``'s own occurrence (issue #369 round 2, literal-text
    matching): the preposition "by" immediately before the number's
    token ("taller by 5 mm") or an axis word immediately after the
    number's unit ("5 mm taller"). A delta RELEASES the axis (the new
    value is unknown — the gate asks), never enforces it as an absolute."""
    words = sorted(set(ABSOLUTE_WORDS) | set(RELATIVE_WORDS), key=len, reverse=True)
    for m in _mm_number_occurrences(clause):
        if float(m.group(1)) != number:
            continue
        before = clause[: m.start()]
        if re.search(r"\bby\s+$", before, re.IGNORECASE):
            return True
        after = clause[m.end() :]
        if re.search(r"\s+(?:" + "|".join(words) + r")\b", after, re.IGNORECASE):
            return True
    return False


@cache
def _axis_letter_pattern(axis: str) -> re.Pattern[str]:
    """The axis-letter cue pattern (``W: 42`` / ``H = 20 mm``) for one
    axis, compiled once (issue #369 round 2)."""
    return re.compile(
        rf"\b{axis}\b\s*[:=]?\s*(\d+(?:\.\d+)?)(?:{MM_UNIT_ALTERNATION})?\b",
        re.IGNORECASE,
    )


def _coerce(value: Any) -> float | None:
    """Coerce a user-stated dimension to a positive float (mm), else None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _extract_stated(
    chat_history: list[str],
    stated_dims: dict[str, Any] | None,
    ai_suggested: dict[str, float] | None,
) -> dict[str, float]:
    """Gather confirmed dimensions from (in priority order) explicit
    ``stated_dims``, then chat text of the form ``W: 42`` / ``42 mm``
    attached to a W/D/H axis, then a USER-confirmed AI suggestion.

    An AI-suggested value is ONLY accepted here if the user has confirmed
    it in the chat (the "confirmable suggestion, never ground truth" rule)
    — a bare pre-fill without user confirmation is NOT a stated dimension
    and will leave the gate closed.
    """
    out: dict[str, float] = {}
    stated_at: dict[str, int] = {}
    turn_cues: dict[int, Cues] = {}

    # 1. Explicit stated_dims (highest priority — the caller parsed these).
    # Deliberate (issue #369 round 3): the caller's explicit set is NOT
    # history and is never touched by the release pass (step 4) — a
    # relative word in the CHAT may release a value the user typed in
    # chat text, but it can never release a value the caller passed in
    # explicitly (an explicit statement is ground truth for the merge).
    # The release pass only pops axes whose ``stated_at`` was recorded by
    # step 2 (chat text); this step writes no ``stated_at`` entry.
    if stated_dims:
        for axis in DIMENSION_AXES:
            v = _coerce(stated_dims.get(axis))
            if v is not None:
                out[axis] = v

    # 2. Chat-text dimensions: axis-letter cues ("W: 42"), W×D×H triples,
    # equal-axis shape shorthand ("a 20 mm cube"), and lexicon cues.
    # Precedence per turn: axis-letter > triple > lexicon > shorthand.
    # Across turns: newest wins.
    history = list(chat_history or [])
    window_start = history_window_start(len(history))
    window = range(window_start, len(history))

    if not all(a in out for a in DIMENSION_AXES):
        for idx in window:
            turn = history[idx]
            text = str(turn)
            turn_axes: dict[str, float] = {}
            for axis in DIMENSION_AXES:
                m = _axis_letter_pattern(axis).search(text)
                if m:
                    v = _coerce(m.group(1))
                    if v is not None:
                        turn_axes[axis] = v
            # W×D×H triple: only fills axes the axis-letter pass left empty.
            triple_axes, _ = _extract_triple(text)
            for axis, value in triple_axes.items():
                if axis not in turn_axes:
                    turn_axes[axis] = value
            # ONE guarded classify per turn: serves the lexicon-absolute
            # extraction below AND is stored for the release pass.
            try:
                cues = classify(text)
            except Exception:
                logger.warning(
                    "stated-extraction classify failed for turn %d (len=%d); "
                    "the turn contributes no lexicon cues and no release",
                    idx,
                    len(turn),
                    exc_info=True,
                )
                cues = None
            if cues is not None:
                lexicon_axes = dict(cues.absolute)
                for axis, value in lexicon_axes.items():
                    if axis not in turn_axes:
                        turn_axes[axis] = value
                turn_cues[idx] = cues
            # Equal-axis size shorthand (only if other passes left axes empty).
            if not turn_axes:
                m = re.search(
                    r"\b(?:a|an)\s+(\d+(?:\.\d+)?)\s*mm\b"
                    r"\s+(?:cube|box|sphere|ball)\b",
                    text,
                    re.IGNORECASE,
                )
                if m:
                    v = _coerce(m.group(1))
                    if v is not None:
                        turn_axes = {axis: v for axis in DIMENSION_AXES}
            for axis in turn_axes:
                stated_at[axis] = idx
            out.update(turn_axes)

    # 3. AI-suggested dimensions, ONLY if the user confirmed them.
    if ai_suggested:
        confirmed_tokens = _confirmed_suggestion_tokens(history)
        for axis, v in ai_suggested.items():
            if axis in out:
                continue
            key = f"suggested:{axis}"
            if key in confirmed_tokens or "suggested" in confirmed_tokens:
                cv = _coerce(v)
                if cv is not None:
                    out[axis] = cv

    # 4. Issue #369 release pass: a RELATIVE word for an axis in a newer
    #    message releases that axis when the axis's LATEST explicit
    #    statement (``stated_at``, step 2) is OLDER than the releasing
    #    turn. An explicit value in the releasing turn itself beats the
    #    relative word and sets instead of releases.
    for idx in window:
        turn = history[idx]
        cues = turn_cues.get(idx)
        if cues is None:
            continue
        for axis in cues.relative:
            if axis in cues.absolute:
                continue
            if axis not in stated_at or stated_at[axis] >= idx:
                continue
            clauses = split_clauses(str(turn))
            if any(FEATURE_NOUN_RE.search(clause) for clause in clauses):
                out.pop(axis, None)
                continue
            v = _unmapped_value_for_axis(cues, clauses)
            if v is not None:
                out[axis] = v
            else:
                out.pop(axis, None)
    return out


def _unmapped_value_for_axis(cues: Cues, clauses: list[str]) -> float | None:
    """The unmapped mm number a releasing turn assigns to its axis.

    An unmapped number ("make it taller, 20 mm") restates the released
    axis ONLY when the message carries EXACTLY ONE unmapped mm number
    that is NOT an anchored delta, the clause containing it has no
    feature noun, and the clause carries no RELATIVE-delta marker
    anchored to THIS number. ``None`` (release) when any condition fails.
    """
    all_numbers = cues.unmapped_mm_numbers
    number_to_clause: dict[float, str] = {}
    non_delta_numbers: list[float] = []
    for number in all_numbers:
        for clause in clauses:
            for m in _mm_number_occurrences(clause):
                if float(m.group(1)) == number:
                    if not _delta_marker_at(number, clause):
                        non_delta_numbers.append(number)
                        number_to_clause[number] = clause
                    break
            else:
                continue
            break
    if len(non_delta_numbers) != 1:
        return None
    number = non_delta_numbers[0]
    clause = number_to_clause[number]
    if FEATURE_NOUN_RE.search(clause):
        return None
    return _coerce(number)


def stated_features_from_messages(
    messages: list[str] | tuple[str, ...],
) -> list[StatedFeature]:
    """The stated feature sizes in a run of user messages (issue #433).

    The FEATURE-clause half of the dimension protocol, mirroring
    ``stated_axes_from_message`` (which owns the W/D/H half): scan each
    message's feature clauses (the clauses :mod:`d33d.feature_clause`
    identifies — a feature noun or a feature-verb sub-clause) and record
    each explicit-mm number the clause states as a :class:`StatedFeature`
    (noun, role, value). The clause's number is the FEATURE's size — the
    axis lexicon suppresses it from W/D/H by design (``feature_clause
    .feature_clause_suppresses``), and this is the record that keeps the
    value honest instead of dropping it.

    Supersession (the #416 pattern, the operator decision): a LATER
    message that restates the SAME feature (same noun + role — a
    ``role=None`` restatement supersedes a ``role=None`` earlier value,
    and an explicit role never matches a ``role=None`` one) REPLACES the
    earlier value in the returned list; a message that names a DIFFERENT
    feature or no feature clause leaves the earlier value in place. The
    same (noun, role, value) stated more than once — two holes of the
    same size, or a restatement — is ONE record, never a duplicate.

    Only explicit-mm numbers count (``12 mm`` / ``12mm`` / ``12
    millimetres``) — cm/in/unitless feature sizes are out of scope
    (issue #433 operator decision), exactly as in
    ``user_quoted_unmapped_mm``'s tier-2 scan.
    """
    from d33d import feature_clause
    from d33d.axis_lexicon import _split_on_and, _numbers_in, _word_re

    #: The current (message index, records) per (noun, role) key — a later
    #: message restating the same key replaces the earlier records; the
    #: within-message key set keeps one record per (noun, role, value).
    current: dict[tuple[str, str | None], tuple[int, list[StatedFeature]]] = {}

    def _record(idx: int, feat: StatedFeature) -> None:
        key = (feat.noun, feat.role)
        existing = current.get(key)
        if existing is not None and existing[0] == idx:
            # Same message: keep one record per (noun, role, value).
            if not any(r.value == feat.value for r in existing[1]):
                existing[1].append(feat)
            return
        current[key] = (idx, [feat])  # a later message supersedes

    #: A single message arrives as a bare ``str`` (the per-message seam);
    #: a list/tuple is the multi-message carry (one element per user
    #: message, in order).
    if isinstance(messages, str):
        messages: list[str] | tuple[str, ...] = [messages]
    for idx, msg in enumerate(messages):
        text = str(msg)
        clauses = split_clauses(text)
        all_sub_clauses: list[str] = []
        for clause in clauses:
            all_sub_clauses.extend(_split_on_and(clause))
        fc_start = feature_clause.feature_clause_start(
            all_sub_clauses, FEATURE_NOUN_RE, _numbers_in, _word_re
        )
        if fc_start < 0:
            continue  # no feature clause in this message — nothing stated
        for clause in all_sub_clauses[fc_start:]:
            m = FEATURE_NOUN_RE.search(clause)
            if m is None:
                continue  # a feature-verb sub-clause without a noun of its
                # own: the noun (and its value) are recorded from the
                # clause that names the feature.
            noun = m.group(0).lower()
            rm = _FEATURE_ROLE_RE.search(clause)
            role = rm.group(0).lower() if rm else None
            for nm in _mm_number_occurrences(clause):
                value = float(nm.group(1))
                if value <= 0:
                    continue
                _record(idx, StatedFeature(noun=noun, role=role, value=value))
    return [r for key in current for r in current[key][1]]


def stated_axes_from_message(
    message: str,
    chat_history: list[str] | tuple[str, ...] | None = (),
    explicit: dict[str, float] | None = None,
) -> dict[str, float]:
    """The PER-AXIS set of axes the user stated (issue #246) — a PARTIAL
    statement counts for the axes it states. The per-axis variant of
    :func:`stated_dims_from_message`: the SAME ``_extract_stated``
    pipeline, returning whatever of W/D/H was stated — an empty dict when
    nothing was stated — instead of ``None`` for a partial statement."""
    turns = [str(t) for t in (chat_history or ())] + [str(message)]
    stated = _extract_stated(turns, explicit, None)
    return {
        axis: float(stated[axis])
        for axis in DIMENSION_AXES
        if axis in stated
    }


def _classify_axis_cues(text: str) -> dict[str, float]:
    """The closed axis lexicon's absolute assignment for ONE message —
    the per-axis seam's floor (issue #314). The triple path wins on
    conflicts; this only fills axes the triple left empty."""
    return dict(classify(text).absolute)


def stated_dims_from_message(
    message: str,
    chat_history: list[str] | tuple[str, ...] | None = (),
    explicit: dict[str, float] | None = None,
) -> tuple[float, float, float] | None:
    """The complete (W, D, H) triple stated in a chat turn, or ``None``.
    Ticket #91's dimension source for the ``/chat`` caller path: the
    user's own words, via the existing :func:`_extract_stated` pipeline.
    Returns ``None`` — NOT a zero triple — when fewer than all three axes
    are stated (the bbox gate is unmeasurable; never fabricate targets)."""
    turns = [str(t) for t in (chat_history or ())] + [str(message)]
    stated = _extract_stated(turns, explicit, None)
    if all(axis in stated for axis in DIMENSION_AXES):
        return (
            float(stated["W"]),
            float(stated["D"]),
            float(stated["H"]),
        )
    return None


def _is_clean_affirmation(
    text: str, *, fit_keyword_is_affirmative: bool = False
) -> bool:
    """True if ``text`` is a SHORT, unambiguous affirmative response.

    A deliberately conservative heuristic soft-gate, NOT a
    confirmation-intent classifier: accepts only turns that are short
    enough to be a plain acceptance (under ~20 words) and carry none of
    the disqualifying signals (question mark, negation, hedging marker).
    ``fit_keyword_is_affirmative=True`` additionally admits a turn naming
    the fit-type keyword itself (a clean "snap fit" IS the answer to the
    fit-type question) and the tight "no fit" / "no-fit" / "nofit"
    phrase (the ``no_fit`` enum value, spoken).
    """
    tokens = re.findall(r"[a-z']+", text.lower())
    if not tokens or len(tokens) > 19:
        return False
    if "?" in text:
        return False
    if fit_keyword_is_affirmative:
        phrase = re.sub(r"\s+", " ", text.strip().lower())
        phrase = phrase.strip(".?! ")
        phrase_nohyphen = phrase.replace("-", "")
        if phrase_nohyphen in {"no fit", "nofit"} and not (
            set(tokens) & {"don't", "dont", "nope", "nah", "wrong", "incorrect"}
        ):
            return True
    if set(tokens) & {
        "no",
        "not",
        "nope",
        "nah",
        "wrong",
        "incorrect",
        "reconsider",
    }:
        return False
    if set(tokens) & {"but", "why", "because", "instead", "although", "however"}:
        return False
    if re.search(
        r"\b(confirm|confirmed|yes|yep|correct|right|accept|ok|okay)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if fit_keyword_is_affirmative:
        return bool(
            re.search(r"\b(slip|press|interference|snap)\b", text, re.IGNORECASE)
        )
    return False


def _last_user_turn(chat_history: list[str]) -> str:
    """The most recent turn in the history ('' for an empty history).

    Only the LAST turn is scanned, so a stray "yes" or "snap" from
    earlier in the conversation can never silently promote an AI
    suggestion into ground truth."""
    if not chat_history:
        return ""
    return str(chat_history[-1])


def _confirmed_suggestion_tokens(chat_history: list[str]) -> set[str]:
    """Tokens the user used to confirm AI suggestions.

    Only the MOST RECENT chat turn is scanned (never the full history),
    and that turn must be a SHORT, unambiguous affirmative response. A
    turn like "Yes, but why did you pick 40 and not 50?" is a QUESTION,
    not an acceptance. A stray confirmatory token in an earlier turn has
    NO effect."""
    tokens: set[str] = set()
    text = _last_user_turn(chat_history)
    if not text:
        return tokens
    if not _is_clean_affirmation(text):
        return tokens
    tokens.add("suggested")
    for axis in DIMENSION_AXES:
        if re.search(rf"\b{axis}\b", text, re.IGNORECASE):
            tokens.add(f"suggested:{axis}")
    return tokens
