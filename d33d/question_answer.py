"""The chat pre-route (issue #249): "can the Brief answer this question?"

A question in the chat ("How tall is it now?") used to fall straight into
the design loop — which produced a wrong new version that discarded the
current part and named it after the question ("v22 — how tall is it now").
The pre-route sits in the /chat path BEFORE the design loop: if the message
is a question AND the project's design state can answer it, the answer is
emitted on the chat stream as an assistant message — no design run, no
render, no version, no filmstrip entry. Everything else, including
anything ambiguous, goes to the design loop exactly as today.

Two stages, per the ticket:

* **Stage 1** (:func:`is_candidate_question`) — deterministic, no LLM,
  microseconds. The message must end with ``?`` OR open with an
  interrogative (what/how/which/is/are/does/do/can/will/why/where/when…),
  AND contain NO change/imperative cue (make, set, change, add, … —
  scanned over the WHOLE message, so "How tall is it now? Make it 15."
  goes to the loop — the imperative wins). Anything failing stage 1
  goes to the design loop unchanged.
* **Stage 2** (:func:`ask_answer_call`) — one cheap single LLM call given
  the question and the current design-state block (provenance and labels
  from the merged ``state_block_for_version`` output), returning a
  structured ``{kind, answer}`` three-way outcome (issue #260): ``kind``
  is ``"answer"`` (the block contains every value the question needs),
  ``"unanswerable"`` (a genuine question the block does not establish),
  or ``"request"`` (the message asks for a design change). The legacy
  ``{answerable, answer}`` boolean shape is still accepted and mapped
  (``true`` → ``answer``, ``false`` → ``request``) so an old-style reply
  never becomes a false "unanswerable". The prompt forbids inventing
  values and forbids offering to set/confirm anything (that offer is a
  separate ticket).
* **The number guard** (:func:`guard_answer_numbers`) — deterministic,
  presence-only: every numeric token in the answer must appear in the
  design-state block (``value`` and ``stated_value``), unit suffixes
  stripped, exact match within 1e-6. A ``12.5`` in the block does not
  license ``"12"`` or ``"13"``. Citing ANOTHER axis's number is a known
  limitation (not caught — presence-only), by the operator's decision.

The wire (operator decision, overrides the earlier token-frame wording):
the answer path emits NO token frames and NO version-created frame. It
emits ONE terminal ``done`` frame whose ``message`` is the answer text
plus an ADDITIVE ``kind: "answer"`` field (``done`` frames without
``kind`` are unaffected and mean a design-loop completion). The two
no-run replies (issue #260) ride the SAME done frame: the design loop
is NEVER the fallback for a question the pre-route already took — a
failed stage-2 call (timeout, exception, malformed reply, guard failure)
replies ``COULD_NOT_ANSWER`` and an ``unanswerable`` classification
replies ``NOT_ESTABLISHED``, both fixed copy.ts strings pinned by the
design-contract test, both with no design run and no version.

Observability (issue #260): every stage-2 outcome — the three kinds AND
the four failure classes (timeout, exception, malformed, guard) — logs
exactly ONE ``WARNING`` record naming the outcome plus elapsed ms and
message length. NEVER the message text or the answer text (no PII in
logs). The stage-1 short-circuits (no versions, not a candidate,
no answer edge) stay at INFO.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time

try:  # The production edge is httpx-based; the timeout classification
    # below needs httpx's timeout exception. The import is guarded so the
    # module loads even in an environment without httpx (tests inject the
    # edge and never take the httpx timeout branch).
    import httpx
except ImportError:  # pragma: no cover - httpx is a hard dep in production
    httpx = None
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from d33d.axis_lexicon import (
    GLOBAL_WORDS,
    RELATIVE_WORDS,
    axis_for_question_word,
)
from d33d.confirm_offer import mm_formatted
from d33d.design_llm import LLM_CALL_TIMEOUT_SECONDS
from d33d.design_state import (
    BBOX_TOLERANCE_MIN_MM,
    BBOX_TOLERANCE_REL,
    build_design_state_block,
    format_design_state_block,
    state_block_for_version,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWER_DONE_KIND",
    "ANSWER_KINDS",
    "COULD_NOT_ANSWER",
    "DETERMINISTIC_AXIS_ADJECTIVES",
    "DETERMINISTIC_AXIS_NOUNS",
    "DETERMINISTIC_AXIS_SENTENCES",
    "DETERMINISTIC_COMPARISON_SENTENCES",
    "DETERMINISTIC_DIMENSION_LIST_RE",
    "LLM_CALL_TIMEOUT_SECONDS",
    "NOT_ESTABLISHED",
    "NO_OFFER_AFFIRMATION_REPLY",
    "NO_VERSION_QUESTION_REPLY",
    "UNANSWERABLE_MISSING_TEMPLATE",
    "AnswerOutcome",
    "ModelUnconfiguredError",
    "ask_answer_call",
    "build_answer_prompt",
    "deterministic_axis_answer",
    "deterministic_axis_outcome",
    "extract_answer_numbers",
    "extract_written_numbers",
    "guard_answer_numbers",
    "is_candidate_question",
    "is_interrogative",
    "parse_answer_reply",
    "route_chat_message",
    "state_block_for_chat",
    "state_block_numbers",
]

#: The stage-2 hard timeout: the ONE shared per-LLM-call constant
#: (issue #313, operator decision) — the design loop's per-call timeout
#: re-exported here (imported from ``d33d.design_llm`` above; the name
#: binds to that value, so ``d33d.question_answer.LLM_CALL_TIMEOUT_SECONDS``
#: is the same object and the monkeypatch target the timeout tests use).
#: The stage-2 budget covers the WHOLE answer call; a hung call is
#: treated exactly like a failed one (design loop, unchanged), never a
#: stall of the chat stream.

#: The done frame's additive discriminator (operator decision): an
#: answer-path terminal frame carries ``kind == "answer"``; a
#: design-loop terminal frame carries no ``kind`` at all (treated as
#: design — the old shape, byte-identical for existing frames).
ANSWER_DONE_KIND = "answer"  # note: the stage-2 reply's kind (issue #260) is a different, three-way value; the Python type for it is :data:`AnswerOutcome` below

#: The stage-2 reply's ``kind`` field (issue #260): a closed three-way
#: set. ``"answer"`` — the block contains every value the question needs;
#: ``"unanswerable"`` — a genuine question the block does not establish;
#: ``"request"`` — the message asks for a design change (routes to the
#: design loop). The legacy ``{answerable: bool}`` shape maps onto this
#: set (``true`` → ``"answer"``, ``false`` → ``"request"``).
#
#: Named ``AnswerOutcome`` (NOT ``ANSWER_DONE_KIND``) to avoid colliding
#: with the done-frame discriminator above: both are called "answer" but
#: one is the stage-2 reply's wire kind and the other is the done frame's
#: additive field.
AnswerOutcome = Literal["answer", "unanswerable", "request"]
ANSWER_KINDS: tuple[str, ...] = ("answer", "unanswerable", "request")

# ---------------------------------------------------------------------------
# The two no-run replies (issue #260) — copy.ts is the source of truth
# ---------------------------------------------------------------------------

#: The no-run reply for a FAILED stage-2 call (timeout, exception,
#: malformed reply, or number-guard failure): the chat says the answer
#: could not be produced just now and nothing changed — no design run,
#: no version. The design loop is NEVER the fallback for a failed answer
#: (the old behaviour — a stage-2 failure fell through to the design
#: loop, which then produced a wrong or failing new version).
COULD_NOT_ANSWER = "I couldn't answer that just now — nothing was changed."

#: The no-run reply for a ``kind: "unanswerable"`` stage-2 classification
#: (a genuine question the design state does not establish — e.g. a
#: question about colour, material, or finish): the design as it stands
#: doesn't establish that, and nothing changed. Also no design run, no
#: version: deciding whether a question is really a change REQUEST is the
#: model's call via ``kind: "request"`` — "unanswerable" never defaults
#: to the loop.
NOT_ESTABLISHED = "The design as it stands doesn't establish that — nothing was changed."

#: The parameterised no-run reply for a ``kind: "unanswerable"`` reply
#: that names the unknown fact (issue #278): the model supplies
#: ``missing`` (a short noun phrase — e.g. "the shelf's height") and the
#: backend builds the reply from this template. The ``missing`` text is
#: validated by :func:`_validate_missing_fact` first (non-string, blank,
#: over 60 chars, a digit, or sentence punctuation other than an
#: apostrophe → ``None`` → the fixed ``NOT_ESTABLISHED`` above); the
#: valid reply keeps the same "nothing was changed" close. The copy is
#: the wire string (the frontend renders the done frame's ``answer``
#: verbatim — no client-side build).
UNANSWERABLE_MISSING_TEMPLATE = (
    "I don't know {missing}. Tell me and I'll check — nothing was changed."
)

#: The no-loop reply for a question on a project with NO version yet
#: (issue #349, operator decision 2): a question (``is_candidate_question``
#: true — every question gets a reply, none is silently dropped) arrives
#: before anything is built. The design loop is never the fallback — a
#: version named after the question is not an answer. The imported v1
#: counts as a version, so this fires only for projects with nothing at
#: all. Verbatim copy of ``copy.ts answerRoute.nothingBuiltYet`` (pinned
#: by the design-contract test and the backend parity test — the #260
#: way).
NO_VERSION_QUESTION_REPLY = (
    "Nothing is built yet — tell me what to make first."
)

#: The no-loop reply for a BARE AFFIRMATION with NO offer pending
#: (issue #349): a clean affirmation (``_is_clean_affirmation`` — the
#: same predicate the #250 offer pre-route uses, unchanged per operator
#: decision 1 — "yes", "ok", …; a hedge like "yes but make it 2 mm" is
#: neither and flows to the loop) arrives when neither a #250 param
#: offer nor a fill-recut offer is live. The design loop is never the
#: fallback for a bare yes. Verbatim copy of ``copy.ts answerRoute.
#: nothingWaitingForYes`` (pinned the same way).
NO_OFFER_AFFIRMATION_REPLY = (
    "There's nothing waiting for a yes right now — what would you like to change?"
)


class ModelUnconfiguredError(Exception):
    """A model-backed question stage must NOT call the LLM (issue #303).

    Raised by the question-path model pre-flight (``d33d.app.
    _build_question_answer_call``) when ``model_preflight`` reports the
    model cannot be used (missing/empty ``${ENV}`` key, or an unresolved
    role/alias). The route's existing broad ``except Exception`` catch
    turns it into a structured terminal error frame (reason
    ``model_unconfigured``) — the SAME frame the design loop emits —
    instead of degrading into a silent LLM call.

    ``env_var`` carries the name of the missing (or empty) ``${ENV}``
    variable (e.g. ``"TRAIL_OPENERS_LLM_KEY"``) or ``None`` for an
    unresolved role/alias or catalogue-level failure. It is NEVER the key
    value: the variable NAME is what the SPA renders in the helper
    sentence ("Set <NAME> where the server runs, then restart it.").
    """

    def __init__(self, env_var: str | None = None) -> None:
        self.env_var = env_var
        super().__init__("model not configured")


def _validate_missing_fact(missing: Any) -> str | None:
    """Validate an unanswerable reply's ``missing`` field (issue #278).

    Returns the trimmed noun phrase iff it is a string that is non-empty
    after trim, at most 60 chars, carries no digits, has no sentence
    punctuation other than an apostrophe, and carries no HTML / markup
    characters (``< > = ' " ( ) [ ] { } `` and backtick — a short noun
    phrase never needs them; see the digit / punctuation rule below).
    Anything else — a non-string (``None``, a number, a list, …), a
    blank, over-length, a digit, sentence punctuation (period, comma,
    semicolon, colon, question mark, exclamation mark), or a markup
    character — is ``None``: the route falls back to the fixed
    ``NOT_ESTABLISHED`` copy. Never malformed (the reply was
    well-formed), never a crash. The validated text is what goes into
    ``UNANSWERABLE_MISSING_TEMPLATE``; it is never logged.
    """
    if not isinstance(missing, str):
        return None
    m = missing.strip()
    if not m:
        return None
    if len(m) > 60:
        return None
    if re.search(r"\d", m):
        return None
    if re.search(r"[\.,;:!?]", m):
        return None
    # HTML / markup characters are rejected outright: the ``missing``
    # text is model-suggested wire text interpolated into the reply
    # verbatim; security must not depend on the SPA's rendering
    # escaping it (a future markdown/HTML rendering of the done frame
    # would otherwise turn model-influenced text into stored XSS).
    if re.search(r'[<>=`"()&\[\]{}]', m):
        return None
    return m

# ---------------------------------------------------------------------------
# Stage 1 — the deterministic question detector
# ---------------------------------------------------------------------------

#: The interrogative openers. The message must START with one (word
#: boundary — "what" matches, "whatever" does not) for the non-``?``
#: branch of the detector.
_INTERROGATIVE_RE = re.compile(
    r"^(?:what|how|which|is|are|does|do|can|will|why|where|when)", re.IGNORECASE
)

#: The imperative/change cues, scanned over the WHOLE message. A hit on
#: ANY of them sends the message to the design loop, no matter what the
#: interrogative check said. The ticket's single-word list is the core;
#: the inflections ("making", "change it", …) are added so a cue is not
#: missed by conjugation. "set" IS in the list — "set" is a change cue
#: ("set the height to 15" goes to the loop, never the answer path);
#: only its conjugated inflections are listed, never misspellings.
#: The multi-word imperative forms ("can you set", …) are the cues
#: below.
#
#: issue #261: the word list is the UNION of the original imperative
#: words and the axis lexicon's relative + global sets ("taller",
#: "wider", "bigger", "half the size", …). "Can it be 20 mm wider?" is
#: a change request, not a question — the relative cue "wider" sends it
#: to the design loop. Absolute words ("tall", "wide", "height") are NOT
#: in this union: "how tall is it?" remains a candidate question.
_BASE_IMPERATIVE_WORDS: frozenset[str] = frozenset(
    [
        "make", "makes", "made", "making",
        "set", "sets", "setting",
        "change", "changes", "changed", "changing",
        "add", "adds", "added", "adding",
        "remove", "removes", "removed", "removing",
        "increase", "increases", "increased", "increasing",
        "decrease", "decreases", "decreased", "decreasing",
        "reduce", "reduces", "reduced", "reducing",
        "move", "moves", "moved", "moving",
        "widen", "widens", "widened", "widening",
        "lengthen", "lengthens", "lengthened", "lengthening",
        "shorten", "shortens", "shortened", "shortening",
        "thicker", "thinner",
        "round", "rounds", "rounded", "rounding",
        "fillet", "fillets", "bore", "bores",
        "drill", "drills", "drilled", "drilling",
    ]
)

#: The lexicon's relative + global words join the imperative set (issue
#: #261): "Can it be 20 mm wider?" is a change request, not a question.
#: Multi-word phrases ("half the size", "twice the size") are escaped in
#: the regex union below.
_LEXICON_IMPERATIVE_WORDS: frozenset[str] = frozenset(RELATIVE_WORDS) | GLOBAL_WORDS
_ALL_IMPERATIVE_WORDS: frozenset[str] = _BASE_IMPERATIVE_WORDS | _LEXICON_IMPERATIVE_WORDS

# The comparison-question carve-out (issue #261 fix batch): a lexicon
# imperative word immediately followed by "than" ("is it taller than the
# shelf?"), which is a comparison, not an imperative. The multi-word
# forms ("half the size") have no "than" carve-out. Stripping these from
# an interrogative message before the single union scan below is the
# entire carve-out: every remaining imperative hit — base word, lexicon
# word not in comparison form, or multi-word form — wins and sends the
# message to the design loop; a message whose only imperative hits are
# lexicon words in comparison form stays a candidate.
_COMPARISON_FORM_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(_LEXICON_IMPERATIVE_WORDS))
    + r")\s+than\b",
    re.IGNORECASE,
)

_IMPERATIVE_RE = re.compile(
    r"\b(?:" + "|".join(re.escape(w) for w in sorted(_ALL_IMPERATIVE_WORDS)) + r")\b",
    re.IGNORECASE,
)
#: The multi-word imperative cues the ticket lists explicitly ("can you
#: make", "could you add", …) — the interrogative opener alone is never
#: sufficient: these phrases OPEN with an interrogative yet are commands.
_MULTIWORD_IMPERATIVE_RE = re.compile(
    r"\b(?:"
    "can you make|can you add|can you remove|can you change|can you move"
    "|can you widen|can you shorten|can you lengthen|can you set"
    "|could you make|could you add|could you remove|could you change|could you move"
    "|could you widen|could you shorten|could you lengthen|could you set"
    "|please make|please add|please remove|please change|please move"
    "|please widen|please shorten|please lengthen|please set"
    r")\b",
    re.IGNORECASE,
)


#: The design/request cues the no-version guard excludes from the
#: "nothing built" reply (issue #349, adversarial review round 1): the
#: STAGE-1 imperative scan alone misses request-questions whose verb is
#: not in the change-cue set — "Could you design a phone stand 80 mm
#: wide?" (design), "Can I get a box 60 × 40 × 30 mm?" (get), "How
#: about a hook that holds 5 kg?" (hold), "What about a 30 mm spacer?"
#: (noun + dimensions, no verb at all). Such a message is a request, not
#: a question about the current design — it MUST start the loop (the
#: ticket's dispatch rule: an imperative verb such as make, design,
#: create, build, print, get, add; or a noun plus dimensions). The
#: predicate is the EXISTING stage-1 cue scan (the imperative union plus
#: the multi-word imperative forms — never widened, it has version-exists
#: callers through :func:`is_candidate_question`) OR one of these
#: request verbs. It is used ONLY in the no-version branch — a message
#: with a live version routes exactly as today.
_REQUEST_CUE_WORDS: frozenset[str] = frozenset(
    ["design", "create", "build", "print", "get", "hold", "holds"]
)
_REQUEST_CUE_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_REQUEST_CUE_WORDS)) + r")\b",
    re.IGNORECASE,
)
#: A "what about …?" / "how about …?" proposal — the noun-plus-dimensions
#: request shape with no verb at all ("What about a 30 mm spacer?").
#: Such a message proposes a design rather than asking about the current
#: one, so the no-version guard must let it start the loop.
_REQUEST_WHAT_ABOUT_RE = re.compile(
    r"\b(?:what|how)\s+about\b", re.IGNORECASE
)


def carries_request_cue(message: str) -> bool:
    """True iff the message carries a design/request cue: any stage-1
    imperative cue (the :data:`_ALL_IMPERATIVE_WORDS` union, including
    "make"/"set"/"add"/…), any multi-word imperative form ("can you
    make", …), one of :data:`_REQUEST_CUE_WORDS` ("design", "create",
    "build", "print", "get", "hold"/"holds"), or a "what about" /
    "how about" proposal form. Used ONLY by the no-version guard.
    The comparison-form carve-out deliberately does NOT apply here —
    a design never exists at this point, so a "taller than" form is
    not a comparison and still routes to the loop."""
    m = message.strip()
    if not m:
        return False
    return any(
        rx.search(m) is not None
        for rx in (
            _IMPERATIVE_RE,
            _MULTIWORD_IMPERATIVE_RE,
            _REQUEST_CUE_RE,
            _REQUEST_WHAT_ABOUT_RE,
        )
    )


def is_interrogative(message: str) -> bool:
    """True iff the message is interrogative in FORM: it ends with ``?``
    or opens with an interrogative word (what/how/which/is/are/does/do/
    can/will/why/where/when). Pure form check — no imperative scan.

    The gate for the comparison-question carve-out in
    :func:`is_candidate_question`: only an interrogative-in-form message
    whose sole imperative hits are lexicon words in comparison form
    ("…than") stays a candidate."""
    m = message.strip()
    if not m:
        return False
    return m.endswith("?") or _INTERROGATIVE_RE.match(m) is not None


def is_candidate_question(message: str) -> bool:
    """Stage 1: ``True`` iff the message is a question the design-state
    block MIGHT answer (no imperative present, interrogative form).

    Rules (the ticket's acceptance criterion):

    * the message ends with ``?`` OR starts with an interrogative
      (what/how/which/is/are/does/do/can/will/why/where/when);
    * the WHOLE message contains no imperative/change cue (make, set,
      change, add, remove, increase, decrease, reduce, move, widen,
      lengthen, shorten, thicker, thinner, taller, bigger, smaller,
      round, fillet, bore, drill, "can you make", "could you add", …).

    The imperative scan wins over the interrogative ("Is it tall enough
    for a 12 mm shelf? Make it 15." → ``False``). A blank message is not
    a candidate (design loop).
    """
    m = message.strip()
    if not m:
        return False
    # Interrogative form: the message ends with "?" OR starts with an
    # interrogative word (the two branches of the ticket's stage 1).
    if not is_interrogative(m):
        return False
    if _MULTIWORD_IMPERATIVE_RE.search(m) is not None:
        return False
    if _IMPERATIVE_RE.search(m) is None:
        return True
    # An imperative hit is present. The comparison-question carve-out:
    # strip every lexicon-word-"than" comparison form, then run the
    # single union scan on the stripped text. The scan is clean iff
    # every imperative hit was a comparison form ("is it taller than
    # the shelf?"); any base word ("make it taller than 30 mm"), any
    # lexicon hit not in comparison form ("Can it be 20 mm wider?"), or
    # any multi-word form ("half the size") survives the removal and
    # sends the message to the design loop.
    return _IMPERATIVE_RE.search(_COMPARISON_FORM_RE.sub("", m)) is None


# ---------------------------------------------------------------------------
# The number guard (deterministic, presence-only)
# ---------------------------------------------------------------------------

#: A numeric token: an integer, a decimal (``12.5`` / ``.5``), with an
#: optional surrounding unit suffix (``mm``, ``°``, ``×``/``x`` — the
#: "20 × 20" case). ``×`` is stripped as a UNIT/multiplication mark, so
#: "20 × 20" yields two ``20``s.
_NUMBER_TOKEN_RE = re.compile(r"\d+(?:\.\d+)?")


def state_block_numbers(entries: list[dict[str, Any]]) -> set[float]:
    """The set of numeric values present in the design-state block.

    ``entries`` is the output of ``state_block_for_version`` (the SAME
    callable the prompt and the SPA's GET render — this is the guard's
    source of truth). Every entry's ``value`` and, for ``disagrees``
    entries, ``stated_value`` contributes when numeric; booleans are
    not measurements (excluded, per the project's ``_is_number``
    convention) and ``None`` values (``unknown``) contribute nothing.
    """
    out: set[float] = set()
    for entry in entries:
        for key in ("value", "stated_value"):
            v = entry.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                out.add(float(v))
    return out


def extract_answer_numbers(answer: str) -> list[float]:
    """The numeric tokens in an answer text, in order (unit suffixes
    stripped — ``12 mm`` → ``12.0``, ``20 × 20`` → ``20.0, 20.0``,
    ``45°`` → ``45.0``). No deduplication (a guard is a per-occurrence
    check; dedup would not change the outcome but would obscure a
    repeated invented number).

    The guard is NUMBER-BLIND TO CONTEXT: a year or version-style number
    in the answer (e.g. "version 2024", "ISO 8997") is treated as a
    measurement and would fail the guard unless that exact number happens
    to appear in the block. Given the answer is model-generated from a
    block whose values are all mm measurements, this is extremely
    unlikely to trigger in practice; the comment documents the
    limitation for the future reader."""
    return [float(t) for t in _NUMBER_TOKEN_RE.findall(answer)]


#: Spelled-out numbers the guard also checks (issue #249 review): a model
#: that writes "twelve millimetres tall" for H=12 would pass the digit
#: guard (no digit present), so the spelled-out forms of the small
#: integers are mapped to their values and held to the same presence rule
#: as digit tokens. Zero–twenty plus the tens up to ninety cover the
#: realistic measurement vocabulary; "a dozen" (12) is the common idiom.
_WRITTEN_NUMBER_VALUES = {
    "zero": 0.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "thirteen": 13.0,
    "fourteen": 14.0,
    "fifteen": 15.0,
    "sixteen": 16.0,
    "seventeen": 17.0,
    "eighteen": 18.0,
    "nineteen": 19.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "fifty": 50.0,
    "sixty": 60.0,
    "seventy": 70.0,
    "eighty": 80.0,
    "ninety": 90.0,
}

_WRITTEN_NUMBER_RE = re.compile(
    r"\b(?:" + "|".join(sorted(_WRITTEN_NUMBER_VALUES, key=len, reverse=True))
    + r")\b",
    re.IGNORECASE,
)

_WRITTEN_DOZEN_RE = re.compile(r"\ba\s+dozen\b", re.IGNORECASE)


def extract_written_numbers(answer: str) -> list[float]:
    """The spelled-out numbers in an answer text, in order (zero–twenty,
    tens up to ninety, and "a dozen" → 12). Case-insensitive; "forty"
    and "fourty" — only the correct spelling is in the map ("fourty"
    matches nothing: an unlisted word is not a number)."""
    out: list[float] = []
    for m in _WRITTEN_NUMBER_RE.finditer(answer):
        out.append(_WRITTEN_NUMBER_VALUES[m.group(0).lower()])
    for _ in _WRITTEN_DOZEN_RE.findall(answer):
        out.append(12.0)
    return out


def _question_numbers(question: str) -> set[float]:
    """The set of numbers in the user's question text, extracted with
    the SAME rules the guard uses for the answer (digit tokens plus
    spelled-out forms — "thirty" → 30.0, "a dozen" → 12.0). These
    values join the block's values as licensed numbers (issue #278).
    """
    return set(extract_answer_numbers(question)) | set(
        extract_written_numbers(question)
    )


def guard_answer_numbers(
    answer: str,
    entries: list[dict[str, Any]],
    question: str | None = None,
    tolerance: float = 1e-6,
) -> bool:
    """The deterministic number guard: ``True`` iff every number in
    ``answer`` appears in the design-state block OR in the question's
    numbers (issue #278 — the model's answer may quote numbers the
    USER typed; the question is not a design-state source, but its
    numbers are not invented).

    ``question`` is the user's question text; when supplied, its numbers
    are extracted (with the same extraction the guard uses for the answer —
    digit tokens and spelled-out forms) and added to the allowed set.
    When it is ``None`` the guard behaves exactly as before (block only).

    The question's numbers license presence ONLY: the model may repeat
    a number the user typed ("a 30 mm screw" → "30 mm") but may not
    substitute it for a design-state value it did not state. Spelled-out
    forms are treated identically to digit tokens ("thirty" in the
    question licenses "30" and "thirty" in the answer).

    "Number" means a digit token (``12``, ``12.5``, ``20 × 20`` → …) OR
    a spelled-out form ("twelve", "fifteen", "a dozen", … — zero–twenty
    plus the tens up to ninety); both are held to the same presence
    rule, on both the answer's and the question's numbers (the same
    extraction runs on both sides).

    Presence-only (operator decision): citing another axis's number
    (e.g. "20 mm tall" when H=12, W=20) PASSES — the guard checks
    presence, not parameter attribution. A block value of ``12.5``
    does not license ``"12"`` or ``"13"`` (exact match within the
    tolerance, unit suffix stripped); the question's ``30`` likewise
    licenses ``30`` but not ``30.5``. An answer with no numbers is
    unanswerable by construction (the block is the only source of
    numbers) — such an answer answers nothing and is treated as
    answerable-true only if it contains no invented number, which the
    trivially-true check guarantees: the guard alone never vetoes a
    word-only answer.
    """
    allowed = state_block_numbers(entries)
    if question is not None:
        # Operator decision (issue #278): question numbers license answer numbers.
        allowed |= _question_numbers(question)
    for n in extract_answer_numbers(answer):
        if not any(abs(n - a) <= tolerance for a in allowed):
            return False
    for n in extract_written_numbers(answer):
        if not any(abs(n - a) <= tolerance for a in allowed):
            return False
    return True


# ---------------------------------------------------------------------------
# The deterministic axis-size stage (issue #263, between stage 1 and
# stage 2)
# ---------------------------------------------------------------------------

#: The dimension-list triggers (operator decision — CLOSED list): any of
#: these, in a stage-1 candidate message, answers deterministically with
#: the full W × D × H list. A message matching a dimension-list trigger
#: AND a single axis word answers the list (the operator decision).
#: Case-insensitive, whole-message (the message is short — a question).
DETERMINISTIC_DIMENSION_LIST_RE = re.compile(
    r"what are the dimensions"
    r"|what are its dimensions"
    r"|what size is it"
    r"|how big is it"
    r"|how large is it"
    r"|what's the size",
    re.IGNORECASE,
)

#: The noun phrases that refer to THE PART itself — "how tall is it",
#: "how tall is the part", "how tall is the model", "how tall is the
#: design", or "how tall is the <version-name>". Any other noun after the
#: axis word ("height of the hole", "how tall is the post") is a feature
#: question: it falls through to stage 2 (the deterministic stage answers
#: the PART's envelope only, never a feature's size).
_PART_NOUNS: frozenset[str] = frozenset(
    {"it", "the part", "the model", "the design"}
)

#: The dimension-list sentence ("It measures {W} × {D} × {H}."), with a
#: dash for an unestablished axis (a blank, never a fabricated number).
DETERMINISTIC_DIMENSION_LIST_FORMAT = "It measures {W} × {D} × {H}."

#: The per-provenance answer sentences (issue #263; values mm()-formatted
#: the way ``copy.ts mm()`` renders them — one decimal, U+202F, "mm").
#: The sibling ``copy`` workstream pins the matching deck strings in
#: ``web/src/copy.ts``; the backend's strings are the wire strings, pinned
#: by the Python tests here (the #250 way — both directions are checked
#: against the same sentences).
DETERMINISTIC_AXIS_SENTENCES = {
    "stated+measured": "It's {value} {axis} — you said that, and I measured it.",
    "measured": "It measures {value} {axis}.",
    "stated": "You said {value} {axis}. Nothing has measured it yet.",
    "disagrees": "You said {stated}; what came out measures {measured}.",
    "not_established": "The {axis_noun} isn't established yet.",
}

#: The axis adjective per axis (the ``{axis}`` slot of the templates
#: above — matches copy.ts' ``${axis}`` parameterisation).
DETERMINISTIC_AXIS_ADJECTIVES: dict[str, str] = {
    "W": "wide",
    "D": "deep",
    "H": "tall",
}

#: The axis noun per axis (the ``{axis_noun}`` slot of the
#: ``not_established`` template — matches copy.ts' ``${axisNoun}``).
DETERMINISTIC_AXIS_NOUNS: dict[str, str] = {
    "W": "width",
    "D": "depth",
    "H": "height",
}


def _normalise_noun(noun: str) -> str:
    """The noun phrase the part-noun rule matches on: lowercase,
    whitespace-normalised (the version-name match is case-insensitive,
    whitespace-normalised, exact equality — the operator decision)."""
    return " ".join(noun.lower().split())


def _noun_refers_to_part(noun: str, version_name: str | None) -> bool:
    """True iff the noun phrase (possibly with trailing adverbs or a
    leading "the") refers to the part itself.

    The operator decision: the version-name match is case-insensitive,
    whitespace-normalised, exact equality between the noun phrase after
    the axis word (a leading "the" is optional) and ``latest["name"]``.
    So "the shelf bracket" matches version "Shelf bracket" (the "the"
    is stripped); "shelf bracket" also matches.
    """
    n = _normalise_noun(noun)
    # Exact match against the fixed part nouns.
    if n in _PART_NOUNS:
        return True
    # Version-name match: the noun (with or without a leading "the")
    # must equal the version name exactly (case-insensitive, whitespace-
    # normalised). "the shelf bracket" → "shelf bracket" == "shelf
    # bracket" (version "Shelf bracket"). "shelf bracket" also matches.
    if version_name is not None:
        vn = _normalise_noun(version_name)
        if vn:
            if n == vn:
                return True
            # Strip a leading "the " from the noun and compare again.
            if n.startswith("the ") and n[4:] == vn:
                return True
            # Or the version name itself starts with "the " (unlikely
            # but symmetric): "the shelf bracket" == "the shelf bracket".
            if vn.startswith("the ") and n == vn[4:]:
                return True
    # Trailing-adverb case: "it now" → "it" is the part; the "now" is
    # a time adverb. Only "it" + trailing adverbs is a valid part match
    # ("the post now" is a feature with an adverb, not the part).
    words = n.split()
    # "it now", "it currently" — the subject is "it" (the part).
    return bool(words and words[0] == "it" and len(words) > 1)


# ---------------------------------------------------------------------------
# The deterministic comparison stage (issue #313, between stage 1 and
# stage 2, BEFORE the deterministic axis stage)
# ---------------------------------------------------------------------------

#: The comparison reply sentences (issue #313; values mm()-formatted the
#: way ``copy.ts mm()`` renders them — one decimal, U+202F, "mm"). The
#: sibling ``copy`` workstream pins the matching deck strings
#: (``deterministicAnswer``'s comparisonYes / comparisonNo /
#: comparisonAboutTheSame / comparisonMissingFact) in
#: ``web/src/copy.ts``; the backend's strings are the wire strings,
#: pinned by the Python tests here against the same sentences (the #250
#: way — both directions are checked).
DETERMINISTIC_COMPARISON_SENTENCES = {
    # The ``{relation}`` slot is the TRUE number relation between measured
    # and target — "more than" when measured > target, "less than" when
    # measured < target (issue #320). It follows the sign of measured −
    # target, INDEPENDENT of the Yes/No the direction decides; "short of"
    # is retired (it is only true for the at-least direction).
    "yes": "Yes — it measures {measured} {axis}, {delta} {relation} {target}.",
    "no": "No — it measures {measured} {axis}, {delta} {relation} {target}.",
    "about_the_same": "About the same — it measures {measured} {axis}.",
    "missing_fact": "How {axis} is {object}? The part is {measured} {axis}.",
}

#: The number the user named in the comparison question, in mm.
#: ``group 1`` is the number itself; ``group 2`` the unit word
#: immediately after it (``[a-z]+`` — a letter run only, so the regex
#: can never read a bare number's own digits as its unit; empty for a
#: bare number, which is mm by the stage's contract).
_COMPARISON_NUMBER_RE = re.compile(
    r"\b(\d+(?:\.\d+)?)\s*([a-z]+)?\b",
    re.IGNORECASE,
)

#: The missing-fact trigger (issue #313, operator decision): a relative
#: axis word + "than" + an article or possessive (the/a/an/my/our/your/
#: this/that) + a noun ("than the shelf", "than my drawer"). The noun is
#: validated in :func:`_comparison_direction` to be non-digit, non-markup,
#: and not a version-referring word ("last", "previous") — those fall
#: through to the existing routing.
_MISSING_FACT_RE = re.compile(
    r"\b(?:taller|shorter|higher|lower|wider|narrower|deeper|shallower)"
    r"\s+than\s+(the|a|an|my|our|your|this|that)\s+\S+",
    re.IGNORECASE,
)

#: The named-axis fit forms: "will it fit in a 45 mm deep gap?" — a
#: single axis word (absolute or relative) somewhere after the "fit"
#: verb, with at most one gap word (the "in" / "a" of "fit in a …")
#: between them. The axis-less "will it fit in a 45 mm gap?" is NOT a
#: fit form (no axis word) and falls through to stage 2 (the operator
#: decision).
_FIT_FORM_RE = re.compile(
    r"\bfit\b\s+(?:\S+\s+){0,5}\b(?:tall|high|height|wide|width|deep|depth"
    r"|taller|shorter|higher|lower|wider|narrower|deeper|shallower)\b",
    re.IGNORECASE,
)


def _comparison_number(message: str) -> float | None:
    """The user's comparison target in mm, or ``None`` (fall through).

    ONE regex pass over ``message`` reads the number-and-unit pair
    (``_COMPARISON_NUMBER_RE``): the number is ``group 1`` and the unit
    the letter word immediately after it (``group 2``, ``mm`` or empty —
    a bare number is mm by the stage's contract). A foreign unit
    (anything other than ``mm``) makes the stage abstain (inch units are
    out of scope — the question goes to stage 2). The count check
    (exactly one number, from ``_NUMBER_TOKEN_RE``) runs on the same
    single pass: zero numbers (the missing-fact form) or two or more
    (ambiguous) is ``None``.
    """
    pairs = list(_COMPARISON_NUMBER_RE.finditer(message))
    if len(pairs) != 1:
        return None
    m = pairs[0]
    unit = (m.group(2) or "").lower()
    if unit not in ("", "mm"):
        return None  # foreign unit (cm, in, …) → fall through
    return float(m.group(1))


def _comparison_direction(
    message: str, axis: str
) -> tuple[str, str | None] | None:
    """The comparison's direction for ONE axis, or ``None`` (fall
    through).

    ``"enough"`` — "deep enough for a 30 mm screw" (measured >=
    target, about-the-same counts as enough);
    ``"than"`` — "taller than 30 mm" (the relative word's polarity decides
    which side is "yes"); ``"fit"`` — "will it fit in a 45 mm deep gap?"
    (the gap is the object: the part fits iff measured <= target,
    about-the-same counts as fits); ``"missing"`` — "taller than the
    shelf" (no number → the missing-fact reply).

    A named-axis fit phrase ("fit in a 45 mm deep gap") forces the
    ``"fit"`` direction even when "enough"/"than" phrasing is absent —
    the fit form is the operator decision's only fit syntax, and the
    axis word's position (inside the fit clause) is what makes it a fit
    at all.

    Returns ``(direction, noun)`` — ``direction`` is ``"enough"``,
    ``"than"``, ``"fit"`` or ``"missing"``; for the ``"missing"``
    direction ``noun`` is the missing-fact object ("the shelf", "my
    drawer"), ``None`` otherwise. The caller uses the noun directly —
    never a re-search.
    """
    low = message.lower()
    if re.search(r"\benough\b", low) is not None:
        return ("enough", None)
    if re.search(r"\bthan\b", low) is not None:
        m = _MISSING_FACT_RE.search(low)
        if m is not None:
            # The trigger is article + \S+; the object is the article
            # (group 1) plus the noun (the last \S+ in the match).
            matched = m.group(0)
            parts = matched.rsplit(None, 1)
            noun = re.sub(r"[?!.,:]\s*$", "", parts[-1]) if len(parts) > 1 else ""
            # The closed exclusion (operator decision): a digit (a
            # version number — "v2"), a markup character, a
            # version-referring noun ("last", "previous"), or a
            # non-object token ("it", "before", "after") is NOT a
            # missing-fact object — the stage falls through to the
            # existing routing.
            if (
                noun
                and re.search(r"\d", noun) is None
                and re.search(r"[<>=`\"'()&\[\]{}]", noun) is None
                and noun.lower() not in ("last", "previous", "it", "before", "after")
            ):
                return ("missing", f"{m.group(1)} {noun}")
        return ("than", None)
    if _FIT_FORM_RE.search(low) is not None:
        return ("fit", None)
    return None


def _deterministic_comparison(
    message: str, latest: dict[str, Any] | None
) -> tuple[str, str] | None:
    """The deterministic comparison stage's ONE derivation for ONE
    message, or ``None`` (the stage does not take the message — fall
    through to the existing routing, which runs :func:
    ``_deterministic_decision`` next and then stage 2).

    Fires only when ALL of the following hold (operator decision, issue
    #313):

    * a latest version exists;
    * the message names EXACTLY ONE axis — the set of distinct axes over
      absolute words (``axis_for_question_word``) and relative words
      (``RELATIVE_WORDS``) has size 1. Feature nouns ("screw") do NOT
      block the stage: "is it deep enough for a 30 mm screw?" fires on
      the absolute word "deep" — the feature noun's clause suppression
      (the dimension protocol) never applies to a question-form message,
      and the comparison's number is the TARGET, not a stated part size;
    * it contains exactly one number (``mm``-suffixed or bare; a foreign
      unit falls through) — OR no number at all, in which case the
      message must be a relative-word "than the/a/… noun" missing-fact
      form ("is it taller than the shelf?");
    * :func:`_axis_value_for` returns a MEASURED value (``measured`` or
      ``stated+measured``, > 0). A stated-only, disagrees, or
      not-established axis falls through to stage 2.

    Returns ``(axis, reply)``. The reply is a computed string (never
    model text): the #278 stage-2 number guard is structurally exempt
    (the reply is returned before the guard's stage runs), and the
    stage makes zero LLM calls and changes nothing.
    """
    if latest is None:
        return None
    low_words = re.findall(r"\b\w+\b", message.lower())
    axes: dict[str, str] = {}
    for w in low_words:
        ax = axis_for_question_word(w) or RELATIVE_WORDS.get(w)
        if ax is not None and w not in axes:
            axes[w] = ax
    if len(axes) != 1:
        return None
    word, axis = next(iter(axes.items()))
    entries = state_block_for_chat(latest)
    cls, value, _ = _axis_value_for(axis, entries, latest)
    if cls not in ("measured", "stated+measured") or value is None or value <= 0:
        return None
    result = _comparison_direction(message, axis)
    if result is None:
        return None
    direction, missing_object = result
    # The fit form's number lives INSIDE the "fit in a 45 mm deep gap"
    # phrase (the gap's size) — any number outside it would make the
    # fit target ambiguous (fall through). Every other form scans the
    # whole message.
    if direction == "fit":
        fit_phrase = _FIT_FORM_RE.search(message)
        # ``direction == "fit"`` came from this same search, so the match
        # exists; the candidate is the matched phrase.
        candidate = fit_phrase.group(0) if fit_phrase is not None else ""
    else:
        candidate = message
    target = _comparison_number(candidate)
    if target is None and direction != "missing":
        return None
    adj = DETERMINISTIC_AXIS_ADJECTIVES[axis]
    if direction == "missing":
        # ``missing_object`` comes from the direction detection above
        # ("the shelf", "my drawer" — the article + noun the user said).
        reply = DETERMINISTIC_COMPARISON_SENTENCES["missing_fact"].format(
            axis=adj,
            object=missing_object,
            measured=mm_formatted(value),
        )
        return axis, reply
    tolerance = max(BBOX_TOLERANCE_REL * target, BBOX_TOLERANCE_MIN_MM)
    diff = value - target
    if abs(diff) <= tolerance:
        reply = DETERMINISTIC_COMPARISON_SENTENCES["about_the_same"].format(
            measured=mm_formatted(value), axis=adj
        )
        return axis, reply
    # The part exceeds the target on this axis (``higher``) — which side
    # is "yes" depends on the direction. ``"fit"`` is the special case:
    # the TARGET is the gap's size, not the part's — the part fits when
    # the part is at or below the gap (measured <= target), so the
    # polarity inverts relative to "enough".
    higher = diff > 0
    if direction == "enough":
        enough = higher  # "deep enough for N": measured >= target
    elif direction == "fit":
        enough = not higher  # "fit in a N mm deep gap": measured <= target
    else:  # "than": the RELATIVE word's polarity decides the reading.
        # "taller than 30" reads as measured >= 30 (higher is yes);
        # "shorter than 33" reads as measured <= 33 (lower is yes).
        enough = higher if word in ("taller", "higher", "wider", "deeper") else not higher
    # The relation word is the TRUE number relation between measured and
    # target (issue #320): "more than" when measured > target, "less
    # than" when measured < target. It is keyed on the SIGN of measured −
    # target, independent of the direction — "short of" is retired because
    # it is only true for the at-least direction.
    relation = "more than" if higher else "less than"
    if enough:
        reply = DETERMINISTIC_COMPARISON_SENTENCES["yes"].format(
            measured=mm_formatted(value),
            axis=adj,
            delta=mm_formatted(abs(diff)),
            relation=relation,
            target=mm_formatted(target),
        )
    else:
        reply = DETERMINISTIC_COMPARISON_SENTENCES["no"].format(
            measured=mm_formatted(value),
            axis=adj,
            delta=mm_formatted(abs(diff)),
            relation=relation,
            target=mm_formatted(target),
        )
    return axis, reply


def _deterministic_axis(message: str, version_name: str | None):
    """The deterministic-stage decision for ONE message.

    Returns ``"list"`` (the dimension-list answer), ``"W"|"D"|"H"`` (the
    single-axis answer), or ``None`` (fall through to stage 2).

    Rules (the operator decisions in issue #263):

    * a dimension-list trigger (the CLOSED ``DETERMINISTIC_DIMENSION_LIST_RE``
      set) → ``"list"`` — it wins even when a single axis word is also
      present ("How wide is it, and what are the dimensions?" → the list).
    * otherwise, exactly ONE absolute axis word via
      ``axis_for_question_word`` (tall/high/height/wide/width/deep/depth)
      → that axis, provided the noun immediately after the axis word
      refers to the part itself ("it" / "the part" / "the model" /
      "the design" / the version's name, case-insensitive,
      whitespace-normalised, exact equality). Any other noun ("height
      of the hole", "how tall is the post") → ``None``.
    * more than one distinct axis word, or no axis word → ``None``.
    """
    if DETERMINISTIC_DIMENSION_LIST_RE.search(message) is not None:
        return "list"

    # Collect every distinct absolute axis word in the message. More than
    # one distinct axis → not a single-axis size question (fall through).
    axes: dict[str, str] = {}
    for w in re.findall(r"\b\w+\b", message.lower()):
        ax = axis_for_question_word(w)
        if ax is not None and w not in axes:
            axes[w] = ax
    if len(axes) != 1:
        return None
    word = next(iter(axes))
    axis = axes[word]
    # The noun immediately after the axis word: everything from the word's
    # end to the next clause boundary or the end. It must be the part
    # itself, not a feature ("height of the hole" → the hole's height).
    m = re.search(r"\b" + re.escape(word) + r"\b", message, re.IGNORECASE)
    if m is None:
        return None
    tail = message[m.end():]
    # Cut the tail at the first punctuation that closes the noun phrase
    # (a comma, semicolon, question mark, or period — the list trigger is
    # already handled above, so a conjunction like "and" is not a boundary
    # for this stage: "how tall is it, and …" with a list trigger is the
    # list; without one, the comma closes the noun phrase and "and…" is
    # a new clause, not a noun).
    cut = re.search(r"[,;?!.]", tail)
    if cut is not None:
        tail = tail[: cut.start()]
    noun = tail.strip()
    # An empty noun ("how tall?" — the word is the last token) is the
    # part itself (the question's subject is implicit "it").
    if noun == "":
        return axis
    # Strip the copula ("is", "are", "was", "were") that separates the
    # axis word from the subject in the "how tall is it" / "how wide
    # is the post" pattern — the noun phrase is what remains after the
    # copula ("it", "the post", …). "is it" → "it" (the part); "is the
    # post" → "the post" (a feature).
    copula = re.match(r"^(?:is|are|was|were)\s+", noun, re.IGNORECASE)
    if copula is not None:
        noun = noun[copula.end():].strip()
        if noun == "":
            return axis
    # The noun may carry a trailing adverb ("it now" — "now" is a time
    # adverb, not part of the noun). Match the noun against the part
    # phrases by PREFIX: if the noun starts with a part noun (and the
    # rest is a trailing word, not a feature qualifier), it is the part.
    # "it now" → "it" (part); "the post here" → "the post" (feature,
    # not a part noun → fall through).
    if _noun_refers_to_part(noun, version_name):
        return axis
    # The noun is a feature ("the hole", "the post", "the wall", …) or
    # any other non-part noun: fall through to stage 2.
    return None


def _axis_value_for(
    axis: str,
    entries: list[dict[str, Any]],
    latest: dict[str, Any] | None,
) -> tuple[str, float | None, float | None]:
    """The answer source for ONE axis, per the operator decision's
    precedence: the design-state AXIS row first (``kind == "axis"``
    explicitly — a param row named "H" is never an answer source), then
    the version's measured bbox extent, then "not established".

    Returns ``(class, value, stated_value)`` where ``class`` is one of
    ``"stated+measured" | "measured" | "stated" | "disagrees" |
    "not_established"`` and the values are floats (mm).

    The ``measured`` provenance in the block is ambiguous: it can mean
    "the user stated it AND the measurement confirmed it" (stated+
    measured) or "the axis was never stated, just measured" (measured
    only). The block does not carry this distinction, so we check
    ``latest["stated_dims"]`` to disambiguate: if the axis was in the
    stated set, the sentence is "you said that, and I measured it";
    otherwise, "It measures...".
    """
    stated_dims = latest.get("stated_dims") if latest else None
    for entry in entries:
        if entry.get("kind") != "axis" or entry.get("name") != axis:
            continue
        prov = entry.get("provenance")
        value = entry.get("value")
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if prov == "measured":
            # Disambiguate: was this axis also stated? If so, the
            # sentence is "you said that, and I measured it" (the
            # stated+measured case). Otherwise, "It measures..."
            if stated_dims and axis in stated_dims:
                return "stated+measured", float(value), None
            return "measured", float(value), None
        if prov == "stated":
            return "stated", float(value), None
        if prov == "disagrees":
            stated = entry.get("stated_value")
            if not isinstance(stated, (int, float)) or isinstance(stated, bool):
                stated = None
            return "disagrees", float(value), (
                float(stated) if stated is not None else None
            )
    # No axis row: the version's bbox extent (if it exists and is
    # positive — a zero extent is the encoded absence, not a measurement).
    if latest is not None:
        bbox = latest.get("bbox")
        if isinstance(bbox, dict):
            key = {"W": "x", "D": "y", "H": "z"}[axis]
            v = bbox.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0:
                return "measured", float(v), None
    return "not_established", None, None


def _deterministic_decision(
    message: str,
    latest: dict[str, Any] | None,
) -> tuple[str, str, str] | None:
    """The deterministic stage's ONE derivation for ONE message, or
    ``None`` (the stage does not take the message — fall through to
    stage 2).

    Returns ``(axis, provenance_class, answer)`` where ``axis`` is
    ``"W"`` / ``"D"`` / ``"H"`` / ``"list"`` and ``provenance_class``
    is one of the :data:`DETERMINISTIC_AXIS_SENTENCES` keys. Computing
    the stage decision, the stage outcome (for the WARNING log) and the
    wire answer in ONE place means :func:`deterministic_axis_answer`
    and :func:`deterministic_axis_outcome` can never diverge (they
    project different fields of the same tuple), and the design-state
    block (:func:`state_block_for_chat`) is built exactly once per
    message. The dimension-list case reports its per-axis provenance
    classes joined by ``+`` in W/D/H order
    (``"measured+measured+not_established"`` — a closed vocabulary,
    never free text).
    """
    if latest is None:
        return None
    # Target-number guard (adversarial finding 1): a message carrying a
    # number ("Can it be 15 mm tall?", "Is it 12 mm tall?") is a
    # PROPOSED change or a yes/no check, not a plain size question — it
    # falls through to stage 2 (where the model decides) instead of
    # being answered with the part's CURRENT height. Conservative and
    # closed: any digit anywhere in the message abstains.
    if re.search(r"\d", message) is not None:
        return None
    entries = state_block_for_chat(latest)
    what = _deterministic_axis(message, latest.get("name"))
    if what is None:
        return None
    if what == "list":
        parts: list[str] = []
        classes: list[str] = []
        for ax in ("W", "D", "H"):
            cls, value, _ = _axis_value_for(ax, entries, latest)
            classes.append(cls)
            if cls == "not_established":
                parts.append("—")
            else:
                # The list uses each axis's best value (the measured one
                # when the block says disagrees; the stated one
                # otherwise).
                parts.append(mm_formatted(value))
        return (
            "list",
            "+".join(classes),
            DETERMINISTIC_DIMENSION_LIST_FORMAT.format(
                W=parts[0], D=parts[1], H=parts[2]
            ),
        )
    cls, value, stated = _axis_value_for(what, entries, latest)
    if cls == "not_established":
        answer = DETERMINISTIC_AXIS_SENTENCES["not_established"].format(
            axis_noun=DETERMINISTIC_AXIS_NOUNS[what]
        )
    elif cls == "disagrees":
        answer = DETERMINISTIC_AXIS_SENTENCES["disagrees"].format(
            stated=mm_formatted(stated) if stated is not None else "?",
            measured=mm_formatted(value) if value is not None else "?",
        )
    else:
        answer = DETERMINISTIC_AXIS_SENTENCES[cls].format(
            value=mm_formatted(value) if value is not None else "?",
            axis=DETERMINISTIC_AXIS_ADJECTIVES[what],
        )
    return what, cls, answer


def deterministic_axis_answer(
    message: str,
    latest: dict[str, Any] | None,
) -> str | None:
    """The deterministic axis-size answer for ONE message, or ``None``
    (fall through to stage 2).

    Only reachable for a stage-1 candidate with a version present — the
    ``latest is None`` early exit runs first in :func:`route_chat_message`.
    The answer rides the #249 plain-message path (``{"kind": "answer",
    "answer": …}``), no version, no design run.

    The stage outcome (the axis, or the dimension-list marker, plus the
    provenance class) is available to the caller via
    :func:`deterministic_axis_outcome` for the WARNING log (one record
    naming the outcome, axis, provenance class — no message text); both
    project the same :func:`_deterministic_decision`, so they cannot
    diverge.
    """
    decision = _deterministic_decision(message, latest)
    if decision is None:
        return None
    return decision[2]


def deterministic_axis_outcome(
    message: str,
    latest: dict[str, Any] | None,
) -> tuple[str, str] | None:
    """The deterministic stage's log outcome for ONE message, or ``None``
    (the stage does not take the message — fall through to stage 2).

    Returns ``(axis, provenance_class)`` where ``axis`` is ``"W"`` / ``"D"``
    / ``"H"`` / ``"list"`` and ``provenance_class`` is one of the
    :data:`DETERMINISTIC_AXIS_SENTENCES` keys. The route logs exactly ONE
    WARNING naming this outcome (plus message length) — never the message
    text or the answer text (no PII in logs). The dimension-list case
    reports its per-axis provenance classes joined by ``+`` in W/D/H order
    (``"measured+measured+not_established"`` — a closed vocabulary, never
    free text).

    Projects the same :func:`_deterministic_decision` as
    :func:`deterministic_axis_answer`, so the log fields and the wire
    answer are derived from one computation.
    """
    decision = _deterministic_decision(message, latest)
    if decision is None:
        return None
    return decision[0], decision[1]


# ---------------------------------------------------------------------------
# Stage 2 — the cheap single LLM call
# ---------------------------------------------------------------------------


def build_answer_prompt(
    question: str,
    entries: list[dict[str, Any]],
    block: dict[str, Any] | None = None,
) -> str:
    """The stage-2 user message: the question + the design-state block
    (with provenance and labels — the ``state_block_for_version`` output
    rendered by ``format_design_state_block``), plus the value-integrity
    contract the model must honour.

    The prompt forbids inventing values (every number must come from the
    block, each cited value naming its provenance in plain words: "you
    said that" / "I measured" / "I assumed" / "not established" / for a
    user-source disagreement, "you said X, I measured Y" / for a
    model-source disagreement, "I set X, it measures Y"), and — per the
    operator's decision — it does NOT ask the model to offer to
    set/confirm anything: the answer ends after the provenance
    citation.
    """
    if block is None:
        block = build_design_state_block(entries)
    state_text = format_design_state_block(block)
    return (
        "You are answering a user's question about their current 3D "
        "design from the design-state block below. The block is the ONLY "
        "source of truth: if it does not contain the value the question "
        "asks about, you cannot answer.\n\n"
        "Rules:\n"
        "- choose the reply kind, one of three:\n"
        '- kind "answer" — the block contains every value you need to '
        "answer (for example \"How tall is it now?\" with the height "
        "stated 12 mm, answer \"It is 12 mm tall — you said that.\"), "
        "answering from the block alone.\n"
        '- kind "unanswerable" — a genuine question the block does not '
        "establish (for example \"Is it taller than the shelf?\" with no "
        "shelf height in the block → leave the answer empty and add "
        "\"missing\": \"the shelf's height\") → name the unknown fact in "
        "the \"missing\" field: one short noun phrase, no digits, no "
        "sentence punctuation. Do not guess.\n"
        '- kind "request" — the message asks for a change to the design '
        '(for example "Can it be 20 mm wider?") → leave the answer '
        "empty.\n"
        "- every number in your answer must come from the block or be a "
        "number the user's own question stated; the user's question may be "
        "quoted in the answer — but only by REPEATING the number as the "
        "user typed it: a number the user typed in the question is quoted "
        "back exactly as typed (\"30 mm\" stays \"30 mm\") and never stands in "
        "for a design-state value (it cannot confirm, deny, or round a "
        "measurement the block does or does not make). Never invent, round "
        "to a different value, or combine values, and no other number is "
        "allowed.\n"
        "- each value you cite must name its provenance in plain words: "
        "'you said that' (stated), 'I measured' (measured), 'I assumed' "
        "(assumed), 'not established' (unknown); when the block shows a "
        "user-source disagreement, say 'you said X, I measured Y'; when "
        "it shows a model-source disagreement, say 'I set X, it measures "
        "Y'.\n"
        "- do NOT offer to change, set, or confirm anything. The answer "
        "ends after the provenance citation.\n\n"
        f"{state_text}\n\n"
        f"Question: {question}\n"
        'Reply with a single JSON object: {"kind": "answer"|'
        '"unanswerable"|"request", "answer": "…"} — for kind '
        '"unanswerable" the object may also carry "missing" (a short '
        'noun phrase naming the unknown fact); no other text.'
        ' Example unanswerable reply: {"kind": "unanswerable", '
        '"answer": "", "missing": "the shelf' + chr(39) + 's height"}.'
    )


def parse_answer_reply(
    text: str,
) -> tuple[AnswerOutcome, str, str | None] | None:
    """The model reply → ``(kind, answer, missing)`` or ``None``
    (malformed).

    ``None`` (not an exception) is the contract: a malformed reply is a
    FAILED ANSWER — the route replies with the fixed
    ``COULD_NOT_ANSWER`` no-run message (issue #260), never the design
    loop. Extraction is lenient (a JSON object inside surrounding prose
    is still parseable), but the shape is strict:

    * the current three-way shape: ``kind`` must be one of the closed
      ``ANSWER_KINDS`` set; ``answer`` must be a non-empty string when
      ``kind`` is ``"answer"`` (an ``"answer"`` with an empty answer is
      malformed — an empty answer answers nothing). A
      ``"unanswerable"`` reply may carry an ADDITIONAL ``missing``
      field (issue #278) — the short noun phrase naming the fact the
      block does not establish (e.g. "the shelf's height"). ``missing``
      is validated by :func:`_validate_missing_fact` in the parser (the
      parser is the single point of truth for the reply's ``missing``
      shape): a value that is not a trimmed, non-empty string of
      ≤ 60 chars, free of digits, sentence punctuation (other than
      apostrophes), and HTML / markup characters is returned as
      ``None`` — the route then falls back to the fixed
      ``NOT_ESTABLISHED``. Invalid ``missing`` is NEVER malformed here;
      absent ``missing`` is ``None``, and the reply's ``answer`` field
      (empty or not) is ignored in favour of the ``missing``-built copy.
    * the LEGACY ``{answerable: bool, answer}`` shape is still accepted
      and mapped (issue #260's pitfall): ``true`` → ``"answer"`` and
      ``false`` → ``"request"`` — preserving today's routing (an
      unanswerable-old-style reply is a change REQUEST to the design
      loop, never a false "unanswerable"). A reply carrying BOTH or
      NEITHER discriminator shape is malformed.
    """
    if not isinstance(text, str):
        return None
    candidate = text.strip()
    # A bare object, or the first balanced {...} block in the reply.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    body = candidate[start : end + 1]
    try:
        doc = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    answer = doc.get("answer")
    if not isinstance(answer, str):
        return None
    # A reply carrying BOTH discriminator shapes (kind AND answerable) is
    # ambiguous: malformed. Neither present: malformed.
    has_kind = isinstance(doc.get("kind"), str)
    has_legacy = isinstance(doc.get("answerable"), bool)
    if has_kind == has_legacy:
        return None
    # The current shape: a closed-set ``kind`` (answer/unanswerable/request).
    kind = doc.get("kind")
    if has_kind:
        if kind not in ANSWER_KINDS:
            return None
        if kind == "answer" and not answer.strip():
            return None
        if kind == "unanswerable":
            # Issue #278: the unanswerable reply may carry ``missing``
            # (the noun phrase naming the unknown fact). The parser is
            # the single point of truth for the reply shape — ``missing``
            # is validated HERE (string, trimmed non-empty, <= 60
            # chars, no digits, no sentence punctuation but apostrophes,
            # no HTML / markup characters); anything invalid is
            # ``None`` so the route falls back to NOT_ESTABLISHED.
            # Never malformed, never a crash; the ``answer`` field
            # (if non-empty) is ignored in favour of the
            # missing-built copy.
            return kind, answer.strip(), _validate_missing_fact(doc.get("missing"))
        return kind, answer.strip(), None
    # The legacy shape: ``answerable`` bool → mapped, never a false
    # "unanswerable" (a legacy false routes to the design loop exactly
    # as it does today). The legacy shape has no missing field.
    answerable = doc.get("answerable")
    legacy_kind: AnswerOutcome = "answer" if answerable else "request"
    if answerable and not answer.strip():
        return None
    return legacy_kind, answer.strip(), None


AnswerFn = Callable[[str], Awaitable[Any]]

#: The production edge's signature: ``async (question_text, entries) ->
#: reply_text`` (the app's ``_build_question_answer_call`` returns this
#: shape — the entries are passed so a future variant can render a
#: per-call block). ``route_chat_message`` adapts it to the
#: one-arg ``AnswerFn`` the guard stage expects.
AnswerEdge = Callable[[str, list[dict[str, Any]]], Awaitable[str]]


async def ask_answer_call(
    question: str,
    entries: list[dict[str, Any]],
    answer_fn: AnswerFn,
    timeout: float = LLM_CALL_TIMEOUT_SECONDS,
    project_id: str | None = None,
) -> tuple[AnswerOutcome, str, str | None] | None:
    """Stage 2: one cheap LLM call + the deterministic number guard.

    ``answer_fn`` is the injected single-shot completion
    (``prompt_text -> raw reply text``) — the route supplies the real
    model edge; tests supply a stub. The guard runs on the parsed
    ``"answer"`` reply BEFORE the route trusts it (a model that invents
    a number fails the answer, deterministically — not a judgment call);
    a ``"request"`` or ``"unanswerable"`` reply skips the guard entirely
    (its answer field is empty by contract).

    Returns ``(kind, answer, missing)`` on a usable outcome —
    ``"answer"`` (guard passing), ``"unanswerable"``, or
    ``"request"`` — and ``None`` in every failure case (the caller
    replies with the fixed ``COULD_NOT_ANSWER`` no-run message — the
    design loop is never the fallback for a failed answer): a malformed
    reply, a guard failure, an exception from ``answer_fn``, or the
    hard ``timeout`` (the shared per-LLM-call bound,
    :data:`LLM_CALL_TIMEOUT_SECONDS` — a hung call fails exactly like a
    failed one).

    ``missing`` is the unanswerable reply's ``missing`` field (issue
    #278: the short noun phrase naming the fact the block does not
    establish — e.g. "the shelf's height") or ``None`` for every other
    outcome; the route validates it and builds the unanswerable copy
    from it, falling back to the fixed ``NOT_ESTABLISHED`` string.

    Every outcome logs exactly ONE ``WARNING`` record naming the outcome
    (the kind, or the failure class ``timeout`` / ``exception`` /
    ``malformed`` / ``guard``) plus elapsed ms and message length —
    never the message text, the answer text, or the missing text (no
    PII in logs).

    Every stage-2 outcome ALSO logs exactly ONE ``INFO`` record (issue
    #313) naming the project id (or ``-`` when absent), the outcome
    (``answered`` / ``timeout`` / ``error`` / ``unanswerable`` — the
    ``request`` outcome logs NO INFO line: the message is routed to the
    design loop, not answered) and ``latency_ms``. ONE ``_finish`` call
    per outcome measures the stage-2 span ONCE and writes BOTH records
    with that single value, so the WARNING's ``elapsed_ms`` and the
    INFO's ``latency_ms`` are the same number by construction.
    """
    prompt = build_answer_prompt(question, entries)
    started = time.monotonic()
    # The question's own numbers (issue #278) — extracted ONCE up front
    # so the guard below licenses them on every attempt path (the
    # answer may quote the user's own numbers; a number in neither the
    # question nor the block is invented).

    def _warn(outcome: str, elapsed_ms: float) -> None:
        logger.warning(
            "question-answer stage 2: outcome=%s elapsed_ms=%.0f "
            "len(message)=%d",
            outcome,
            elapsed_ms,
            len(question),
        )

    def _finish(warn_outcome: str, info_outcome: str | None) -> None:
        # One measurement, two records (issue #313): the stage-2 span is
        # measured ONCE and both records carry it, so the WARNING's
        # ``elapsed_ms`` and the INFO's ``latency_ms`` are the same
        # number by construction. The INFO record names the project id
        # (or ``-``), the outcome and the latency — never the prompt, the
        # answer text or the key (no PII in logs); ``info_outcome=None``
        # (the ``request`` outcome) logs no INFO line (the caller routes
        # to the design loop — it is not an answer outcome).
        elapsed_ms = (time.monotonic() - started) * 1000
        _warn(warn_outcome, elapsed_ms)
        if info_outcome is not None:
            logger.info(
                "question-answer stage 2: project_id=%s outcome=%s latency_ms=%.0f",
                project_id if project_id is not None else "-",
                info_outcome,
                elapsed_ms,
            )

    raw: Any
    try:
        raw = await asyncio.wait_for(answer_fn(prompt), timeout=timeout)
    except asyncio.CancelledError:
        # Cancellation is a control-flow signal, NOT a failed answer: it
        # must propagate so the caller's cancellation (and any of its
        # ``except CancelledError`` cleanup) is never swallowed into a
        # no-run reply.
        raise
    except ModelUnconfiguredError:
        # Pre-flight (issue #303) configuration failure: re-raise to the route's ``except ModelUnconfiguredError`` — never a swallowed ``COULD_NOT_ANSWER``.
        raise
    except TimeoutError:
        # The hard ``timeout`` bound fired (``asyncio.wait_for`` raises
        # ``TimeoutError`` — ``asyncio.TimeoutError`` is an alias of the
        # builtin in 3.11+): a failed answer of the ``timeout`` class.
        _finish("timeout", "timeout")
        return None
    except Exception as exc:  # noqa: BLE001 — any non-timeout failure is a failed answer; the classification below is by class, not blind
        # ANY other failure is a failed answer — classify by the exception
        # class, never by elapsed time (the old heuristic misclassified a
        # late-firing non-timeout error as a timeout). The production edge
        # is httpx-based: an httpx timeout (the per-request client bound
        # in ``_http_request_factory``) is a ``timeout`` too; anything else
        # (a plain exception, an HTTP error, a connection reset) is an
        # ``exception``. The INFO record carries the exception's class
        # name — a capability failure and a network blip must not both
        # read as the same unclassified ``error``.
        if httpx is not None and isinstance(exc, httpx.TimeoutException):
            _finish("timeout", "timeout")
        else:
            elapsed_ms = (time.monotonic() - started) * 1000
            _warn("exception", elapsed_ms)
            logger.info(
                "question-answer stage 2: project_id=%s outcome=error error_class=%s latency_ms=%.0f",
                project_id if project_id is not None else "-",
                type(exc).__name__,
                elapsed_ms,
            )
        return None
    parsed = parse_answer_reply(raw)
    if parsed is None:
        _finish("malformed", "error")
        return None
    kind, answer, missing = parsed
    if kind == "request":
        # No INFO line for the ``request`` outcome (the message routes to
        # the design loop — it is not an answer outcome).
        _finish("request", None)
        return "request", answer, None
    if kind == "unanswerable":
        _finish("unanswerable", "unanswerable")
        return "unanswerable", answer, missing
    # The guard licenses the answer's numbers from the block AND the
    # question (issue #278 — the answer may quote the user's own
    # numbers, e.g. the "30" of "a 30 mm screw"); a number in neither
    # is invented → a failed answer of the ``guard`` class.
    if not guard_answer_numbers(answer, entries, question=question):
        _finish("guard", "error")
        return None
    _finish("answer", "answered")
    return "answer", answer, None


# ---------------------------------------------------------------------------
# The route seam (called from the /chat handler)
# ---------------------------------------------------------------------------


def state_block_for_chat(
    latest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The latest version's design-state entries (the SAME
    ``state_block_for_version`` the prompt and the SPA's GET call) —
    ``None`` (no version) yields an empty block (an honest empty state,
    never fabricated dimensions).

    The version's ``confirmed_params`` (issue #250, rule (b)) rides the
    latest row too — the chat pre-route's block renders a confirmed param
    ``stated`` exactly as the design-state GET (and the prompt) do, so
    the offer path (which runs BEFORE this pre-route) and the answer
    path agree on the state they see."""
    if latest is None:
        return []
    return state_block_for_version(
        dict(latest.get("params") or {}),
        latest.get("bbox"),
        latest.get("stated_dims"),
        latest.get("param_meta"),
        latest.get("confirmed_params"),
    )


async def route_chat_message(
    message: str,
    latest: dict[str, Any] | None,
    answer_edge: AnswerEdge | None = None,
    timeout: float = LLM_CALL_TIMEOUT_SECONDS,
    project_id: str | None = None,
) -> dict[str, Any] | None:
    """The pre-route decision for ONE chat message.

    Returns ``{"kind": "answer", "answer": <text>}`` (the ``kind`` field
    is the wire discriminator ``ANSWER_DONE_KIND`` — a done frame with
    ``kind == "answer"`` — NOT the stage-2 outcome kind) in every case
    the pre-route takes ownership of the message:

    * a stage-2 ``"answer"`` (number guard passing) — the design-state
      block's answer (the #249 path, byte-for-byte unchanged);
    * a stage-2 ``"unanswerable"`` with a valid ``missing`` field
      (issue #278) — the reply ``I don't know {missing}. Tell me and I'll
      check — nothing was changed.`` built from the parameterised
      ``UNANSWERABLE_MISSING_TEMPLATE``; no design run, no version;
      an invalid or absent ``missing`` falls back to the fixed
      ``NOT_ESTABLISHED`` no-run reply (issue #260);
    * a stage-2 failure (timeout, exception, malformed reply, or
      number-guard failure) — the fixed ``COULD_NOT_ANSWER`` no-run
      reply (issue #260); no design run, no version.

    Returns ``None`` (the caller routes to the design loop exactly as
    today) ONLY when:

    * stage 1 rejects (not a question, or an imperative is present —
      ambiguous → loop, per the ticket);
    * the no-version guard declines the message (NOT a question —
      a change request on an empty project still starts the loop,
      issue #349);
    * no answer edge is wired;
    * stage 2 classifies the message as a ``"request"`` — the model
      says the message asks for a change, and the design loop is the
      right home (exactly as a non-question message would route).

    The ``timeout`` parameter (default: :data:`LLM_CALL_TIMEOUT_SECONDS`
    — the shared per-LLM-call constant, issue #313) is the hard bound
    the stage-2 call runs under. Tests pass a shorter value to pin the
    timeout contract in <1 s of wall clock.

    The ``project_id`` (issue #313) rides the stage-2 INFO line (``-``
    when absent); the existing callers/tests pass nothing and keep
    working (the default ``None``).

    The stage-1 short-circuits log at INFO; every stage-2 outcome logs
    at WARNING and (except the ``request`` outcome) at INFO from
    :func:`ask_answer_call` (issues #260 / #313).
    """
    if latest is None:
        # The no-version guard (issue #349, operator decision 2 — keyed
        # on ``latest is None``; the imported v1 IS a version, so it
        # never reaches this branch): a QUESTION with nothing built gets
        # the deterministic "nothing built" reply (``kind: "answer"``
        # done frame — the same wire shape as every other no-run reply),
        # never the design loop (no pending version named after the
        # question, no LLM call). Every question gets a reply — none is
        # silently dropped. A change request (stage 1 rejects it —
        # imperative cue or no interrogative form) still starts the
        # loop exactly as today, including the first design request on
        # an empty project.
        # A question-shaped message carrying a request cue ("Could you
        # design …?" / "Can I get a box …?" / "What about a 30 mm
        # spacer?" — a noun + dimensions, no change verb) is a request
        # in question form, not a question about the current design:
        # it starts the loop exactly like any other first design request
        # (issue #349 — the no-version reply is reserved for genuine
        # questions about something that does not yet exist).
        if is_candidate_question(message) and not carries_request_cue(message):
            logger.info(
                "question-answer: no versions yet — replying with the "
                "nothing-built notice (len(message)=%d)",
                len(message),
            )
            return {
                "kind": ANSWER_DONE_KIND,
                "answer": NO_VERSION_QUESTION_REPLY,
            }
        logger.info(
            "question-answer: no versions yet — design loop"
        )
        return None
    if not is_candidate_question(message):
        # Length-only log (no PII — the message text is never logged).
        logger.info(
            "question-answer: message is not a stage-1 question "
            "(len(message)=%d) — design loop",
            len(message),
        )
        return None
    # The deterministic comparison stage (issue #313): a stage-1
    # candidate that names exactly one axis (absolute or relative word),
    # carries exactly one mm number (or is a relative-word "than the/…
    # object" missing-fact form), and has a MEASURED axis value is
    # answered from the design state with NO LLM call (no timeout, no
    # model call, no guard — the reply is computed, so the #278 stage-2
    # number guard is structurally exempt). It runs BEFORE
    # :func:`_deterministic_decision` (whose digit-abstain would block
    # every digit-bearing comparison question). Anything it does not
    # take falls through to the existing routing exactly as today.
    comparison = _deterministic_comparison(message, latest)
    if comparison is not None:
        axis, reply = comparison
        logger.info(
            "question-answer: outcome=deterministic axis=%s "
            "provenance=comparison (len(message)=%d)",
            axis,
            len(message),
        )
        return {"kind": ANSWER_DONE_KIND, "answer": reply}
    # The deterministic axis-size stage (issue #263): a stage-1 candidate
    # that asks for exactly one axis's size — or the full dimension list —
    # is answered from the design state with NO LLM call (no timeout,
    # no model call, no guard). It runs BEFORE the answer-edge check
    # (the deterministic answer needs no model). Anything it does not
    # take falls through to stage 2 exactly as today. ONE derivation
    # (:func:`_deterministic_decision`) yields the stage decision, the
    # log fields and the wire answer at once, so they cannot diverge;
    # ONE WARNING names the outcome (axis, provenance class — no
    # message text).
    decision = _deterministic_decision(message, latest)
    if decision is not None:
        axis, prov, deterministic = decision
        logger.warning(
            "question-answer: outcome=deterministic axis=%s "
            "provenance=%s (len(message)=%d)",
            axis,
            prov,
            len(message),
        )
        return {"kind": ANSWER_DONE_KIND, "answer": deterministic}
    entries = state_block_for_chat(latest)
    if answer_edge is None:
        logger.info(
            "question-answer: no answer model wired — design loop"
        )
        return None

    async def _answer_fn(prompt: str) -> Any:
        return await answer_edge(message, entries)

    result = await ask_answer_call(
        message, entries, _answer_fn, timeout=timeout, project_id=project_id
    )
    if result is None:
        # A FAILED stage-2 call (timeout, exception, malformed reply,
        # or number-guard failure): the fixed no-run reply. The design
        # loop is never the fallback for a failed answer (issue #260).
        return {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
    kind, answer, missing = result
    if kind == "request":
        # The model says the message asks for a change to the design:
        # the design loop is the right home, exactly as a non-question
        # message routes. No no-run reply.
        return None
    if kind == "unanswerable":
        # A genuine question the design state does not establish: the
        # reply names the unknown fact when the model supplied a valid
        # ``missing`` (issue #278), else the fixed no-run copy
        # (issue #260). Deciding when a question is really a request is
        # the model's call via kind "request" — "unanswerable" never
        # defaults to the loop. The missing text is never logged.
        # The parser (parse_answer_reply → _validate_missing_fact) has
        # already validated ``missing``; the ``is not None`` check
        # below is the passthrough (a None means the value was invalid
        # or absent — the parser returned None for either case).
        validated = _validate_missing_fact(missing)
        reply = (
            UNANSWERABLE_MISSING_TEMPLATE.format(missing=validated)
            if validated is not None
            else NOT_ESTABLISHED
        )
        return {"kind": ANSWER_DONE_KIND, "answer": reply}
    # kind == "answer" (the guard passed — ask_answer_call runs it):
    # the #249 path, byte-for-byte unchanged. The stage-2 outcome
    # already logged its one WARNING from ask_answer_call (issue #260);
    # this INFO keeps the answered case separately visible with lengths
    # only (no PII in either).
    logger.info(
        "question-answer: answering from the design-state block "
        "(len(message)=%d, len(answer)=%d)",
        len(message),
        len(answer),
    )
    return {"kind": ANSWER_DONE_KIND, "answer": answer}
