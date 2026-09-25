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

The tier-1/tier-2 sentence builders (``tier_1_sentence`` /
``tier_2_sentence``) are deck mirrors of ``copy.ts``: the backend is
the writer of the wire string, ``web/src/copy.ts`` holds the same
templates so the design-contract test can pin that deck and server
match in substance (the #250 way).

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
(``copy.confirmOffer.acknowledged``). Tier 3 (issue #261) is this
#250 template, terminal when tiers 1 and 2 are both empty.

The tier-3 ``{value}`` slot is the :func:`mm_value_str` spelling
(issue #265): ``mm_formatted`` (one decimal + U+202F + ``mm`` — the
deck's ``mm()``) ONLY when the param is genuinely an mm measurement —
its metadata carries ``unit: "mm"`` explicitly OR it declares an axis
(the evidence ``state_block_from_params`` carries onto the entry itself
— an explicit ``unit`` key plus the ``axis`` key; the entry's default
``unit: "mm"`` for every numeric param does NOT count); a non-mm /
unitless value keeps :func:`format_param_value` verbatim. The
model-sentence value-name check accepts EITHER the bare or the
mm-formatted spelling of the value.

Tier-1 (a released axis) and tier-2 (a user-quoted unmapped number)
sentences (issue #261) render ``{value}`` via :func:`mm_formatted`
when the param's metadata unit is ``mm`` (the ``meta_unit`` seam of
``state_block_from_params``'s join — the default ``unit: "mm"``
does NOT count) — NEVER the raw number: the deck's ``mm()``
(``toFixed(1)`` + U+202F + ``mm``) is the wire string, and Python's
``:g`` (``format_param_value``) would diverge (``12`` vs ``12.0``).

Import graph (acyclic by construction): this module is a LEAF of the
design-state chain — ``design_state`` imports
``CONFIRMED_VALUE_TOLERANCE`` from here (rule (b) promotion), and this
module lazily imports ``d33d.design_state`` / ``d33d.question_answer`` /
``d33d.dimension_protocol`` inside functions. Do NOT add a module-level
import of ``d33d.design_state`` or ``d33d.question_answer`` here.
"""

from __future__ import annotations

import logging
from collections.abc import Collection
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
    "mm_formatted",
    "mm_value_str",
    "offer_entry",
    "offer_sentence",
    "select_offer_candidate",
    "tier_1_cue",
    "tier_1_sentence",
    "tier_1_sentence_template",
    "tier_2_sentence",
    "tier_2_sentence_template",
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
    disagree_names: Collection[str] = frozenset(),
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
    user's own moves).

    ``disagree_names`` (issue #264) is the EXPLICIT set of param names
    whose ``state_block_for_version`` entry has provenance ``disagrees``
    (either source — user-stated or model-emitted). A value the
    measurement contradicts is never an assumption to confirm: offering
    it would ask the user to affirm a number the part itself disproves.
    The caller computes the set from the FULL ``state_block_for_version``
    output (the only place the measurement comparison runs) and passes
    it in — this helper itself is measurement-blind (it reads
    ``state_block_from_params``), so the exclusion is always explicit,
    never inferred. The default ``frozenset()`` keeps pre-#264 callers
    (and the tier tests) exactly as they were: an empty set excludes
    nothing."""
    from d33d.design_state import state_block_from_params

    confirmed = confirmed or {}
    changed = {name for name, value in params.items() if name in excluded}
    changed.update(excluded)
    disagrees = set(disagree_names)
    entries = state_block_from_params(params, param_meta)
    by_name = {e["name"]: e for e in entries}
    out: list[dict[str, Any]] = []
    for name, value in params.items():
        if name in changed or name in confirmed or name in disagrees:
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
    released_axes: set[str] | None = None,
    user_quoted_mm: set[float] | None = None,
    disagree_names: Collection[str] = frozenset(),
) -> str | None:
    """The ONE assumed param to offer for this version, or ``None``.

    Precedence (issue #261's operator decision — tiers in order, an empty
    tier falls through to the next, tier 3 is today's order and is
    terminal; still at most ONE offer, never a confirmed or changed
    param):

    1. an assumed numeric param with a declared axis ON AN AXIS RELEASED
       by this turn's relative/global cue (``released_axes`` — "make it
       taller" released H → the H-declared assumed param is the one the
       user's own words are about);
    2. an assumed numeric param whose value equals (±1e-6) a user-quoted
       UNMAPPED mm number (``user_quoted_mm`` — the tier-2 helper's
       output; "a spacer to lift a shelf 12 mm" → the 12-valued param);
    3. today's order: a declared-axis assumed param (declaration order),
       else the validated ``confirm_first`` (the model's fit-critical
       flag — rejected when it names a non-assumed, non-numeric,
       already-confirmed, or user-changed param, in which case the offer
       is ABSENT rather than a silent second fallback), else none.

    ``released_axes`` / ``user_quoted_mm`` default to ``None`` — callers
    that do not run the #261 tiering (the pre-#261 behaviour) get exactly
    today's order.

    ``disagree_names`` (issue #264) is the explicit set of param names
    whose ``state_block_for_version`` entry has provenance ``disagrees``
    (either source) — a value the measurement contradicts is never
    offerable (offering it would ask the user to affirm a number the
    part itself disproves). The caller computes it from the FULL
    ``state_block_for_version`` output (the only place the measurement
    comparison runs) and passes it in; the default ``frozenset()``
    excludes nothing (the pre-#264 callers' behaviour, unchanged).
    """
    changed_set = set(changed or ())
    eligible = _assumed_numeric_params(
        params, param_meta, confirmed, changed_set, disagree_names
    )
    if not eligible:
        return None
    if released_axes:
        # Tier 1: a declared-axis assumed param on a released axis
        # (declaration order — the first in the params snapshot).
        for entry in eligible:
            if entry.get("axis") in released_axes:
                return entry["name"]
    if user_quoted_mm is not None and user_quoted_mm:
        # Tier 2: a value that equals (±1e-6) a user-quoted unmapped mm
        # number (declaration order).
        for entry in eligible:
            value = entry.get("value")
            if _is_number(value) and any(
                abs(float(value) - n) <= CONFIRMED_VALUE_TOLERANCE
                for n in user_quoted_mm
            ):
                return entry["name"]
    # Tier 3: today's order (terminal).
    for entry in eligible:
        if entry.get("axis"):
            return entry["name"]
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
    disagree_names: Collection[str] = frozenset(),
) -> bool:
    """``confirm_first`` is valid iff it names an assumed numeric param of
    THIS version (in the eligible set — declared, non-zero numeric
    value, not confirmed, not user-changed, not a disagrees param —
    see :func:`select_offer_candidate` for ``disagree_names``)."""
    if not isinstance(confirm_first, str) or not confirm_first:
        return False
    changed_set = set(changed or ())
    eligible = _assumed_numeric_params(
        params, param_meta, confirmed, changed_set, disagree_names
    )
    return any(e["name"] == confirm_first for e in eligible)


def offer_entry(
    params: dict[str, Any],
    param_meta: dict[str, Any] | None,
    name: str,
) -> dict[str, Any] | None:
    """The chosen param's design-state entry (label + value from the
    shared block builder), with the param's own metadata grafted onto
    the ``meta_unit`` / ``param_axis`` keys (see the module docstring's
    tier rules) — or ``None`` when the name is not a declared param
    (a guard — the caller should never reach here with a bad name)."""
    from d33d.design_state import normalize_param_meta, state_block_from_params

    meta = normalize_param_meta(param_meta)
    for entry in state_block_from_params(params, param_meta):
        if entry["name"] == name:
            m = meta.get(name) or {}
            unit = m.get("unit")
            axis = m.get("axis")
            entry["meta_unit"] = unit if isinstance(unit, str) and unit else None
            entry["param_axis"] = (
                axis if isinstance(axis, str) and axis else None
            )
            return entry
    return None


def _is_mm_evidence(entry: dict[str, Any]) -> bool:
    """The single #265 mm-evidence predicate, shared by :func:`mm_value_str`
    (tier 3) and :func:`_value_str_or_format` (tiers 1 and 2): ``True``
    when the param's METADATA says ``unit: "mm"`` explicitly (the
    ``meta_unit`` key — the model-declared unit, never the entry's
    default) OR it declares an axis (the ``param_axis`` key — the
    binding operator decision). The design-state entry's default
    ``unit: "mm"`` (every numeric param) does NOT count — a count like
    ``hole_count 3`` stays ``"3"``."""
    unit = entry.get("meta_unit")
    axis = entry.get("param_axis")
    return (
        (isinstance(unit, str) and unit == "mm")
        or (isinstance(axis, str) and axis)
    )


def mm_value_str(entry: dict[str, Any]) -> str:
    """The value spelling an MM param renders with in the tier-3 offer
    and ack (issue #265): the ``mm()``-formatted string (``12`` →
    ``"12.0\u202fmm"`` — one decimal, the U+202F narrow no-break space,
    ``mm`` — the deck's ``copy.ts mm()`` rendering, byte-for-byte) ONLY
    when the param is genuinely an mm measurement (:func:`_is_mm_evidence`
    — metadata ``unit: "mm"`` explicitly, or a declared axis; the
    entry's default ``unit: "mm"`` does not count).

    A non-mm unit, no unit, or a non-numeric value keeps
    :func:`format_param_value` verbatim — no unit is ever fabricated."""
    value = entry.get("value")
    if not _is_number(value):
        return _format_value(value)
    if _is_mm_evidence(entry):
        return mm_formatted(value)
    return _format_value(value)


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
    sentence must carry the value, not just a mood).

    The value-name check (issue #265) accepts the chosen value spelled
    EITHER the bare ``:g`` rendering ("40") OR the ``mm()``-formatted
    rendering ("40.0 mm" — U+202F or a regular space): a model sentence
    that says "I assumed 40 for …" is as valid as one that says "I
    assumed 40.0 mm for …". The template render always uses the mm
    spelling for an mm param and the bare spelling otherwise.

    The value-name check uses a token-exact match (not a substring ``in``):
    the sentence's digit tokens are tokenized (``_tokenize_numbers``) and
    the value names it if any token equals the bare ``:g`` rendering or
    the numeric prefix of the mm-formatted rendering.  A substring match
    would accept "I assumed 4 for Spacer depth" (a wrong value that
    happens to equal another param in the block) when the chosen value is
    40 — ``"4" in "40"`` is True — letting the wrong sentence through
    verbatim."""
    from d33d.question_answer import guard_answer_numbers

    if isinstance(sentence, str) and sentence.strip():
        label = entry.get("label") or entry.get("name") or ""
        if label and label in sentence and _sentence_names_value(
            entry.get("value"), sentence
        ) and guard_answer_numbers(sentence, block_entries):
            return sentence.strip()
    value_str = mm_value_str(entry)
    label = entry.get("label") or entry.get("name") or entry["name"]
    return f"I assumed {value_str} for {label}. Want it different?"


#: Regex: a number — optional leading dot ("4" or ".5") or bare digits;
#: one optional decimal fraction.  Splits on non-digit boundaries so that
#: ``"40"`` and ``"4"`` are distinct tokens, while ``"40.0 mm"`` →
#: ``["40.0"]`` (the trailing unit is a separate non-numeric token).
_NUM_RE = "\\.?\\d+(?:\\.\\d+)?"


def _tokenize_numbers(sentence: str) -> list[str]:
    """Digit tokens in ``sentence`` (``re.findall`` with ``_NUM_RE``).

    A substring ``in`` check (``"4" in "40"``) accepts a sentence that
    names a WRONG value when the wrong number happens to be a digit
    prefix of the correct one — the adversarial finding on issue #265.
    Token-exact matching (``"4" in ["40"]`` → False) closes that gap
    while keeping the operator-decided matrix: bare ("40"), mm-formatted
    ("40.0"), and decimal ("40.0") spellings all name the value 40.
    Non-numeric values (strings, bools) have no digit tokens → the bare
    string ``in sentence`` substring check is retained (no regression —
    the pre-#265 behaviour)."""
    import re

    return re.findall(_NUM_RE, sentence)


def _sentence_names_value(value: Any, sentence: str) -> bool:
    """Token-exact value-name check for :func:`offer_sentence` (issue #265).

    True iff the sentence names the value — in either the bare ``:g``
    spelling or the mm-formatted spelling — AND every number in the
    sentence is accounted for by the token-exact rule.  For a numeric
    value the check is:

    - The sentence contains a token equal to the bare ``:g`` rendering
      ("40"), OR
    - The sentence contains a token equal to the numeric prefix of the
      mm-formatted rendering ("40.0" — the part before the U+202F
      ``mm``), which covers both ``"40.0\u202fmm"`` and ``"40.0 mm"``.

    The two spellings may be the same string (value 40 → bare "40",
    mm prefix "40.0") or differ (value 4 → bare "4", mm prefix "4.0");
    either one matching is sufficient.  A token that TRUNCATES the bare
    spelling's decimal fraction (value 1.23456 → the natural "1.2" or
    "1.23" roundings) also names the value (see :func:
    ``_token_truncates_bare``) — without it a value whose ``:g`` spelling
    needs more than one decimal digit would demote every plausible model
    rounding to the template.

    A non-numeric value falls back to a plain ``in`` substring check (the
    pre-#265 behaviour — strings and bools have no digit-token structure)."""
    bare = _format_value(value)
    if not bare:
        return False
    # Non-numeric values: no digit tokens to match; use the plain substring
    # check (the pre-#265 behaviour — a string value like "matte" is named
    # by its verbatim spelling in the sentence).
    if not _is_number(value):
        return bare in sentence
    # Numeric value: token-exact match on the bare or mm numeric prefix,
    # or a decimal truncation of the bare spelling.
    tokens = set(_tokenize_numbers(sentence))
    mm_prefix = mm_formatted(value).split("\u202f")[0]  # "40.0" (no U+202F, no mm)
    return any(
        tok == bare or tok == mm_prefix or _token_truncates_bare(tok, bare)
        for tok in tokens
    )


def _token_truncates_bare(token: str, bare: str) -> bool:
    """``True`` iff ``token`` is a decimal TRUNCATION of the bare ``:g``
    spelling ``bare`` (the model rounding a value to fewer decimal digits
    — value ``1.23456`` → the natural "1.2" or "1.23" roundings both name
    it, issue #265's adversarial finding on the mm-prefix gap).

    A token truncates ``bare`` iff it is a PROPER prefix of ``bare``
    (any number of trailing digits dropped — "1.23" truncates "1.23456",
    "1.2" truncates "1.23456"), the integer parts are identical, and
    ``bare``'s digit at the truncation point (the first dropped one)
    is either ``0`` (the rounding is exact — "1.2" truncates "1.20")
    or was followed in ``bare`` by further fraction (the kept digits are
    the truncated ones — "1.23" truncates "1.23456"). A token that
    merely shares a prefix but rounds UP ("1.3" vs "1.23456") is NOT a
    truncation — the mm-side check still catches the exact one-decimal
    ``mm()`` rounding the wire always spells.

    The sign is handled by the caller: the tokenizer produces unsigned
    tokens, so the sign of ``bare`` (which may be negative) is stripped
    before comparison."""
    t = token.lstrip("-")
    b = bare.lstrip("-")
    if t == b:
        return False
    if not b.startswith(t):
        return False
    int_t, _, frac_t = t.partition(".")
    int_b, _, frac_b = b.partition(".")
    if int_t != int_b:
        return False
    first_dropped = frac_b[len(frac_t)] if len(frac_b) > len(frac_t) else "0"
    if first_dropped == "0":
        return True
    return len(frac_b) > len(frac_t) + 1


def _format_value(value: Any) -> str:  # alias — the private name predates the public one
    return format_param_value(value)


#: The no-break space copy.ts's ``mm()`` uses (U+202F narrow no-break
#: space — keeps "34 mm" from breaking across a line). The backend's
#: mm-formatted strings must byte-match the deck's (the design-contract
#: test pins the backend sentence against copy.ts — the #250 way).
_NB_SPACE = "\u202F"


def mm_formatted(value: Any) -> str:
    """A value rendered the way the deck's ``copy.ts mm()`` renders it:
    ONE decimal, the U+202F narrow no-break space, ``mm`` (``12`` →
    ``"12.0\u202fmm"``). The tier-1/tier-2 offer sentences' ``{value}``
    slot is ALWAYS this form when the param unit is mm — never the raw
    number, never ``:g`` (Python's ``12`` would diverge from the deck's
    ``12.0``)."""
    return f"{float(value):.1f}{_NB_SPACE}mm"


# The closed relative + global word sets (built once, lazily —
# ``d33d.axis_lexicon`` is a LEAF, so the import is cycle-safe, but the
# set is built at first call to keep module import order free of d33d-
# internal edges at import time).
_REL_GLOBAL_SET: frozenset[str] | None = None


def _rel_global_set() -> frozenset[str]:
    global _REL_GLOBAL_SET
    if _REL_GLOBAL_SET is None:
        from d33d.axis_lexicon import GLOBAL_WORDS, RELATIVE_WORDS

        _REL_GLOBAL_SET = frozenset(RELATIVE_WORDS) | GLOBAL_WORDS
    return _REL_GLOBAL_SET


def tier_1_cue(message: str) -> str | None:
    """The tier-1 ``{cue}`` — the FIRST entry of
    ``classify(message).cue_words`` that belongs to the lexicon's
    relative or global word sets (issue #261's operator decision: no
    second scan of the raw message), verbatim, lowercased ("taller",
    "half the size"). ``None`` when the message carried no
    relative/global cue (the tier-1 sentence is not the offer)."""
    from d33d.axis_lexicon import classify

    cues = classify(message)
    if not (cues.relative or cues.global_):
        return None
    rel_global = _rel_global_set()
    for word in cues.cue_words:
        if word.lower() in rel_global:
            return word.lower()
    return None


def _value_str_or_format(entry: dict[str, Any]) -> str:
    """The entry's ``{value}`` slot (tiers 1 and 2): ``mm_formatted``
    only for a numeric value whose metadata declares ``unit: "mm"``
    explicitly (the ``meta_unit`` key — the default ``unit: "mm"`` does
    not count); a non-numeric or unitless value falls back to
    :func:`_format_value` (never a fabricated ``mm`` unit)."""
    value = entry.get("value")
    if entry.get("meta_unit") == "mm" and _is_number(value):
        return mm_formatted(value)
    return _format_value(value)


def tier_1_sentence(entry: dict[str, Any], cue: str) -> str:
    """The tier-1 offer sentence (issue #261 — "You asked for {cue} — I
    made {label} {value}. Right?"): ``{value}`` is always the ``mm()``-
    formatted string when the param's metadata unit is mm
    (``meta_unit == "mm"``, ``mm_formatted``), never the raw number; the
    label per #248 (identifier fallback)."""
    value_str = _value_str_or_format(entry)
    label = entry.get("label") or entry.get("name") or entry["name"]
    return f"You asked for {cue} — I made {label} {value_str}. Right?"


def tier_1_sentence_template() -> str:
    """The tier-1 offer template, slots in place — the deck-mirror shape
    (``copy.ts`` ``confirmOffer.tier1Offer``, the #250 pinning pattern:
    the deck holds the string so a test can render it with its own
    ``mm()``-formatted value and compare it slot-for-slot against this
    function's output)."""
    return "You asked for {cue} — I made {label} {value}. Right?"


def tier_2_sentence_template() -> str:
    """The tier-2 offer template, slots in place (``copy.ts``
    ``confirmOffer.tier2Offer`` — see :func:`tier_1_sentence_template`
    for the pinning pattern)."""
    return "You said {value} — I used it for {label}. Right?"


def tier_2_sentence(entry: dict[str, Any]) -> str:
    """The tier-2 offer sentence (issue #261 — "You said {value} — I used
    it for {label}. Right?"): the user-quoted unmapped mm number the
    param's value equals; ``{value}`` ``mm()``-formatted (``mm_formatted``)
    when the param's metadata unit is mm."""
    value_str = _value_str_or_format(entry)
    label = entry.get("label") or entry.get("name") or entry["name"]
    return f"You said {value_str} — I used it for {label}. Right?"


def ack_sentence(entry: dict[str, Any]) -> str:
    """The acknowledgement the accepted-offer flow replies with (the
    SPA's ``confirmOffer.acknowledged`` in ``copy.ts``): "Got it —
    {label} stays {value}." (label per #248, identifier fallback — the
    template is deterministic; the model never writes it).

    ``{value}`` is the :func:`mm_value_str` spelling (issue #265): the
    ``mm()``-formatted string for a genuinely-mm param (``"3.0\u202fmm"``),
    the bare ``:g`` rendering otherwise — the same spelling the tier-3
    offer used, so the offer and its ack agree."""
    value_str = mm_value_str(entry)
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


