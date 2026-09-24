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
  structured ``{answerable, answer}``. The prompt forbids inventing
  values and forbids offering to set/confirm anything (that offer is a
  separate ticket). ``answerable`` false, any call failure, or the
  10 s hard timeout (the operator's latency bound) all degrade to the
  design loop.
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
``kind`` are unaffected and mean a design-loop completion).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any

from d33d.design_state import (
    build_design_state_block,
    format_design_state_block,
    state_block_for_version,
)

logger = logging.getLogger(__name__)

__all__ = [
    "ANSWER_CALL_TIMEOUT_SECONDS",
    "ANSWER_DONE_KIND",
    "is_candidate_question",
    "state_block_numbers",
    "extract_answer_numbers",
    "guard_answer_numbers",
    "build_answer_prompt",
    "parse_answer_reply",
    "ask_answer_call",
    "state_block_for_chat",
    "route_chat_message",
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
#: missed by conjugation. "set" is deliberately NOT in the list: the
#: ticket's example answer ("Want to set it?") and common design-state
#: prose make it ambiguous in a question context, and the multi-word
#: cues below cover the imperative forms of it.
_IMPERATIVE_RE = re.compile(
    r"\b(?:"
    "make|makes|made|making|set|sets|seting|setted|setting|change|changes|changed|changing"
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
    if _MULTIWORD_IMPERATIVE_RE.search(m) is not None:
        return False
    return True


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


def guard_answer_numbers(
    answer: str, entries: list[dict[str, Any]], tolerance: float = 1e-6
) -> bool:
    """The deterministic number guard: ``True`` iff every number in
    ``answer`` appears in the design-state block.

    Presence-only (operator decision): citing another axis's number
    (e.g. "20 mm tall" when H=12, W=20) PASSES — the guard checks
    digit presence, not parameter attribution. A block value of ``12.5``
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
        "- answerable is true ONLY if the block contains every value you "
        "need; if the block cannot answer the question, set answerable "
        "to false and leave answer empty.\n"
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
        'Reply with a single JSON object: {"answerable": true|false, '
        '"answer": "…"} — no other text.'
    )


def parse_answer_reply(text: str) -> tuple[bool, str] | None:
    """The model reply → ``(answerable, answer)`` or ``None`` (malformed).

    ``None`` (not an exception) is the contract: a malformed reply is
    "not answerable" — the route degrades to the design loop exactly as
    today. Extraction is lenient (a JSON object inside surrounding
    prose is still parseable), but the shape is strict: ``answerable``
    must be a bool; ``answer`` must be a non-empty string when
    ``answerable`` is true (an ``answerable: true`` with an empty answer
    is malformed — an empty answer answers nothing).
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
    answerable = doc.get("answerable")
    if not isinstance(answerable, bool):
        return None
    answer = doc.get("answer")
    if not isinstance(answer, str):
        return None
    if answerable and not answer.strip():
        return None
    return answerable, answer.strip()


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
) -> tuple[bool, str] | None:
    """Stage 2: one cheap LLM call + the deterministic number guard.

    ``answer_fn`` is the injected single-shot completion
    (``prompt_text -> raw reply text``) — the route supplies the real
    model edge; tests supply a stub. The guard runs on the parsed answer
    BEFORE the route trusts it (a model that invents a number is
    unanswerable, deterministically — not a judgment call).

    Returns ``(True, answer)`` on success, ``None`` in every failure
    case (the caller routes to the design loop unchanged): a malformed
    reply, an ``answerable: false`` reply, a guard failure, an exception
    from ``answer_fn``, or the hard ``timeout`` (operator decision: the
    10 s bound is hard — a hung call degrades exactly like a failed
    one).
    """
    prompt = build_answer_prompt(question, entries)
    raw: Any
    try:
        raw = await asyncio.wait_for(answer_fn(prompt), timeout=timeout)
    except (asyncio.TimeoutError, Exception):  # noqa: BLE001 — ANY failure degrades to the loop
        logger.info(
            "question-answer stage 2 call failed or timed out "
            "(question=%r) — routing to the design loop",
            question,
        )
        return None
    parsed = parse_answer_reply(raw)
    if parsed is None:
        raw_repr = raw if isinstance(raw, str) else type(raw).__name__
        logger.info(
            "question-answer stage 2 reply was malformed (raw=%r) — "
            "routing to the design loop",
            raw_repr,
        )
        return None
    answerable, answer = parsed
    if not answerable:
        logger.info(
            "question-answer stage 2: block cannot answer (question=%r) — "
            "routing to the design loop",
            question,
        )
        return None
    if not guard_answer_numbers(answer, entries):
        logger.info(
            "question-answer number guard failed (answer=%r) — routing "
            "to the design loop",
            answer,
        )
        return None
    return True, answer


# ---------------------------------------------------------------------------
# The route seam (called from the /chat handler)
# ---------------------------------------------------------------------------


def state_block_for_chat(
    latest: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The latest version's design-state entries (the SAME
    ``state_block_for_version`` the prompt and the SPA's GET call) —
    ``None`` (no version) yields an empty block (an honest empty state,
    never fabricated dimensions)."""
    if latest is None:
        return []
    return state_block_for_version(
        dict(latest.get("params") or {}),
        latest.get("bbox"),
        latest.get("stated_dims"),
        latest.get("param_meta"),
    )


async def route_chat_message(
    message: str,
    latest: dict[str, Any] | None,
    answer_edge: AnswerEdge | None = None,
    timeout: float = ANSWER_CALL_TIMEOUT_SECONDS,
) -> dict[str, Any] | None:
    """The pre-route decision for ONE chat message.

    Returns ``{"kind": "answer", "answer": <text>}`` when the message
    clears stage 1 (a question, no imperative) AND the project has at
    least one version AND stage 2 answers from the design-state block
    (number guard passing). Returns ``None`` in EVERY other case (the
    caller routes to the design loop exactly as today):

    * no version yet (the design-state block is empty — nothing to
      answer from);
    * stage 1 rejects (not a question, or an imperative is present —
      ambiguous → loop, per the ticket);
    * stage 2 fails, times out, is unanswerable, or trips the guard.

    The ``timeout`` parameter (default: ``ANSWER_CALL_TIMEOUT_SECONDS``
    = 10 s) is the hard bound the stage-2 call runs under — the operator
    decision's latency bound. Tests pass a shorter value to pin the
    timeout contract in <1 s of wall clock.

    The decision is logged at INFO (which route was taken and why — the
    operator's requirement).
    """
    if latest is None:
        logger.info("question-answer: no versions yet — design loop")
        return None
    if not is_candidate_question(message):
        logger.info(
            "question-answer: message is not a stage-1 question "
            "(%r) — design loop",
            message,
        )
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
        return None
    _ok, answer = result
    logger.info(
        "question-answer: answering from the design-state block "
        "(question=%r)",
        message,
    )
    return {"kind": ANSWER_DONE_KIND, "answer": answer}
