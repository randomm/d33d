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
from collections.abc import Awaitable, Callable
from typing import Any, Literal

from d33d.design_state import (
    build_design_state_block,
    format_design_state_block,
    state_block_for_version,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWER_CALL_TIMEOUT_SECONDS",
    "ANSWER_DONE_KIND",
    "ANSWER_KINDS",
    "COULD_NOT_ANSWER",
    "NOT_ESTABLISHED",
    "ask_answer_call",
    "build_answer_prompt",
    "extract_answer_numbers",
    "extract_written_numbers",
    "guard_answer_numbers",
    "is_candidate_question",
    "parse_answer_reply",
    "route_chat_message",
    "state_block_for_chat",
    "state_block_numbers",
]

#: The stage-2 hard timeout (operator decision: 10 s). The stage-2 call
#: is a single cheap completion — a hung call is treated exactly like a
#: failed one (design loop, unchanged), never a stall of the chat stream.
ANSWER_CALL_TIMEOUT_SECONDS = 10.0

#: The done frame's additive discriminator (operator decision): an
#: answer-path terminal frame carries ``kind == "answer"``; a
#: design-loop terminal frame carries no ``kind`` at all (treated as
#: design — the old shape, byte-identical for existing frames).
ANSWER_DONE_KIND = "answer"

#: The stage-2 reply's ``kind`` field (issue #260): a closed three-way
#: set. ``"answer"`` — the block contains every value the question needs;
#: ``"unanswerable"`` — a genuine question the block does not establish;
#: ``"request"`` — the message asks for a design change (routes to the
#: design loop). The legacy ``{answerable: bool}`` shape maps onto this
#: set (``true`` → ``"answer"``, ``false`` → ``"request"``).
AnswerKind = Literal["answer", "unanswerable", "request"]
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
_IMPERATIVE_RE = re.compile(
    r"\b(?:"
    "make|makes|made|making|set|sets|setting|change|changes|changed|changing"
    "|add|adds|added|adding|remove|removes|removed|removing|increase|increases|increased|increasing"
    "|decrease|decreases|decreased|decreasing|reduce|reduces|reduced|reducing"
    "|move|moves|moved|moving|widen|widens|widened|widening"
    "|lengthen|lengthens|lengthened|lengthening|shorten|shortens|shortened|shortening"
    "|thicker|thinner|taller|bigger|smaller"
    "|round|rounds|rounded|rounding|fillet|fillets|bore|bores|drill|drills|drilled|drilling"
    r")\b",
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
    is_interrogative = m.endswith("?") or _INTERROGATIVE_RE.match(m) is not None
    if not is_interrogative:
        return False
    # The imperative scan runs over the WHOLE message and WINS over the
    # interrogative form: "Is it tall enough for a 12 mm shelf? Make it
    # 15." → not a candidate (the imperative is present).
    if _IMPERATIVE_RE.search(m) is not None:
        return False
    return _MULTIWORD_IMPERATIVE_RE.search(m) is None


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


def guard_answer_numbers(
    answer: str, entries: list[dict[str, Any]], tolerance: float = 1e-6
) -> bool:
    """The deterministic number guard: ``True`` iff every number in
    ``answer`` appears in the design-state block.

    "Number" means a digit token (``12``, ``12.5``, ``20 × 20`` → …) OR
    a spelled-out form ("twelve", "fifteen", "a dozen", … — zero–twenty
    plus the tens up to ninety); both are held to the same presence rule.

    Presence-only (operator decision): citing another axis's number
    (e.g. "20 mm tall" when H=12, W=20) PASSES — the guard checks
    presence, not parameter attribution. A block value of ``12.5``
    does not license ``"12"`` or ``"13"`` (exact match within the
    tolerance, unit suffix stripped). An answer with no numbers is
    unanswerable by construction (the block is the only source of
    numbers) — such an answer answers nothing and is treated as
    answerable-true only if it contains no invented number, which the
    trivially-true check guarantees: the guard alone never vetoes a
    word-only answer.
    """
    allowed = state_block_numbers(entries)
    for n in extract_answer_numbers(answer):
        if not any(abs(n - a) <= tolerance for a in allowed):
            return False
    for n in extract_written_numbers(answer):
        if not any(abs(n - a) <= tolerance for a in allowed):
            return False
    return True


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
    disagreement, "you said X, I measured Y"), and — per the operator's
    decision — it does NOT ask the model to offer to set/confirm
    anything: the answer ends after the provenance citation.
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
        "establish (for example a question about colour, material or "
        "finish) → leave the answer empty. Do not guess.\n"
        '- kind "request" — the message asks for a change to the design '
        '(for example "Can it be 20 mm wider?") → leave the answer '
        "empty.\n"
        "- every number in your answer must come from the block — never "
        "invent, round to a different value, or combine values.\n"
        "- each value you cite must name its provenance in plain words: "
        "'you said that' (stated), 'I measured' (measured), 'I assumed' "
        "(assumed), 'not established' (unknown); when the block shows a "
        "disagreement, say 'you said X, I measured Y'.\n"
        "- do NOT offer to change, set, or confirm anything. The answer "
        "ends after the provenance citation.\n\n"
        f"{state_text}\n\n"
        f"Question: {question}\n"
        'Reply with a single JSON object: {"kind": "answer"|'
        '"unanswerable"|"request", "answer": "…"} — no other text.'
    )


def parse_answer_reply(text: str) -> tuple[AnswerKind, str] | None:
    """The model reply → ``(kind, answer)`` or ``None`` (malformed).

    ``None`` (not an exception) is the contract: a malformed reply is a
    FAILED ANSWER — the route replies with the fixed
    ``COULD_NOT_ANSWER`` no-run message (issue #260), never the design
    loop. Extraction is lenient (a JSON object inside surrounding prose
    is still parseable), but the shape is strict:

    * the current three-way shape: ``kind`` must be one of the closed
      ``ANSWER_KINDS`` set; ``answer`` must be a non-empty string when
      ``kind`` is ``"answer"`` (an ``"answer"`` with an empty answer is
      malformed — an empty answer answers nothing).
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
        return kind, answer.strip()
    # The legacy shape: ``answerable`` bool → mapped, never a false
    # "unanswerable" (a legacy false routes to the design loop exactly
    # as it does today).
    answerable = doc.get("answerable")
    legacy_kind: AnswerKind = "answer" if answerable else "request"
    if answerable and not answer.strip():
        return None
    return legacy_kind, answer.strip()


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
    timeout: float = ANSWER_CALL_TIMEOUT_SECONDS,
) -> tuple[AnswerKind, str] | None:
    """Stage 2: one cheap LLM call + the deterministic number guard.

    ``answer_fn`` is the injected single-shot completion
    (``prompt_text -> raw reply text``) — the route supplies the real
    model edge; tests supply a stub. The guard runs on the parsed
    ``"answer"`` reply BEFORE the route trusts it (a model that invents
    a number fails the answer, deterministically — not a judgment call);
    a ``"request"`` or ``"unanswerable"`` reply skips the guard entirely
    (its answer field is empty by contract).

    Returns ``(kind, answer)`` on a usable outcome — ``"answer"``
    (guard passing), ``"unanswerable"``, or ``"request"`` — and
    ``None`` in every failure case (the caller replies with the fixed
    ``COULD_NOT_ANSWER`` no-run message — the design loop is never the
    fallback for a failed answer): a malformed reply, a guard failure,
    an exception from ``answer_fn``, or the hard ``timeout`` (operator
    decision: the 10 s bound is hard — a hung call fails exactly like a
    failed one).

    Every outcome logs exactly ONE ``WARNING`` record naming the outcome
    (the kind, or the failure class ``timeout`` / ``exception`` /
    ``malformed`` / ``guard``) plus elapsed ms and message length —
    never the message text or the answer text (no PII in logs).
    """
    prompt = build_answer_prompt(question, entries)
    started = time.monotonic()

    def _warn(outcome: str, elapsed_ms: float) -> None:
        logger.warning(
            "question-answer stage 2: outcome=%s elapsed_ms=%.0f "
            "len(message)=%d",
            outcome,
            elapsed_ms,
            len(question),
        )

    raw: Any
    try:
        raw = await asyncio.wait_for(answer_fn(prompt), timeout=timeout)
    except Exception:  # noqa: BLE001 — ANY failure (incl. the 10 s TimeoutError) is a failed answer
        # The elapsed time distinguishes the two failure classes the
        # operator decision names separately: a call that ran the whole
        # bound is a timeout; an early failure is an exception.
        elapsed = (time.monotonic() - started) * 1000
        _warn("timeout" if elapsed >= timeout * 1000 else "exception", elapsed)
        return None
    parsed = parse_answer_reply(raw)
    if parsed is None:
        _warn("malformed", (time.monotonic() - started) * 1000)
        return None
    kind, answer = parsed
    if kind == "request":
        _warn("request", (time.monotonic() - started) * 1000)
        return "request", answer
    if kind == "unanswerable":
        _warn("unanswerable", (time.monotonic() - started) * 1000)
        return "unanswerable", answer
    if not guard_answer_numbers(answer, entries):
        _warn("guard", (time.monotonic() - started) * 1000)
        return None
    _warn("answer", (time.monotonic() - started) * 1000)
    return "answer", answer


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
    timeout: float = ANSWER_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """The pre-route decision for ONE chat message.

    Returns ``{"kind": "answer", "answer": <text>}`` (the ``kind`` field
    is the wire discriminator ``ANSWER_DONE_KIND`` — a done frame with
    ``kind == "answer"`` — NOT the stage-2 outcome kind) in every case
    the pre-route takes ownership of the message:

    * a stage-2 ``"answer"`` (number guard passing) — the design-state
      block's answer (the #249 path, byte-for-byte unchanged);
    * a stage-2 ``"unanswerable"`` — the fixed ``NOT_ESTABLISHED``
      no-run reply (issue #260); no design run, no version;
    * a stage-2 failure (timeout, exception, malformed reply, or
      number-guard failure) — the fixed ``COULD_NOT_ANSWER`` no-run
      reply (issue #260); no design run, no version.

    Returns ``None`` (the caller routes to the design loop exactly as
    today) ONLY when:

    * no version yet (the design-state block is empty — nothing to
      answer from);
    * stage 1 rejects (not a question, or an imperative is present —
      ambiguous → loop, per the ticket);
    * no answer edge is wired;
    * stage 2 classifies the message as a ``"request"`` — the model
      says the message asks for a change, and the design loop is the
      right home (exactly as a non-question message would route).

    The ``timeout`` parameter (default: ``ANSWER_CALL_TIMEOUT_SECONDS``
    = 10 s) is the hard bound the stage-2 call runs under — the operator
    decision's latency bound. Tests pass a shorter value to pin the
    timeout contract in <1 s of wall clock.

    The stage-1 short-circuits log at INFO; every stage-2 outcome logs
    at WARNING from :func:`ask_answer_call` (issue #260).
    """
    if latest is None:
        logger.info("question-answer: no versions yet — design loop")
        return None
    if not is_candidate_question(message):
        logger.info(
            "question-answer: message is not a stage-1 question "
            "(len(message)=%d) — design loop",
            len(message),
        )
        logger.debug("question-answer: message=%r", message)
        return None
    entries = state_block_for_chat(latest)
    if answer_edge is None:
        logger.info(
            "question-answer: no answer model wired — design loop"
        )
        return None

    async def _answer_fn(prompt: str) -> Any:
        return await answer_edge(message, entries)

    result = await ask_answer_call(message, entries, _answer_fn, timeout=timeout)
    if result is None:
        # A FAILED stage-2 call (timeout, exception, malformed reply,
        # or number-guard failure): the fixed no-run reply. The design
        # loop is never the fallback for a failed answer (issue #260).
        return {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
    kind, answer = result
    if kind == "request":
        # The model says the message asks for a change to the design:
        # the design loop is the right home, exactly as a non-question
        # message routes. No no-run reply.
        return None
    if kind == "unanswerable":
        # A genuine question the design state does not establish: the
        # fixed no-run reply (issue #260). Deciding when a question is
        # really a request is the model's call via kind "request" —
        # "unanswerable" never defaults to the loop.
        return {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}
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
