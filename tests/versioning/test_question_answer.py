"""Issue #249: the chat pre-route — "can the Brief answer this question?"

A question in the chat ("How tall is it now?") used to fall straight into
the design loop, which produced a wrong new version that discarded the
current part and named it after the question ("v22 — how tall is it now").
The pre-route sits in the /chat path BEFORE the design loop: if the
message is a question AND the project's design state can answer it, the
answer is emitted on the chat stream as an assistant message — no design
run, no render, no version, no filmstrip entry. Anything else, including
anything ambiguous, goes to the design loop exactly as today.

Issue #260: a question the stage-1 filter accepts must never become a
design run because the stage-2 call failed. A stage-2 timeout, exception,
malformed reply, or number-guard failure replies with the fixed
"couldn't answer" no-run message (never the design loop); a kind
"unanswerable" reply gets the fixed "not established" no-run message;
a kind "request" reply routes to the design loop exactly as a
non-question message. Every stage-2 outcome logs exactly one WARNING
record naming the outcome (no message or answer text in it).

This file covers the Python side of the seam (task-a scope):

* stage 1 — the deterministic question detector (no LLM, no app);
* stage 2 — the cheap single LLM call + the number guard (stub answer_fn);
  issue #260: the three-way outcome (answer/unanswerable/request) and the
  no-run replies — a stage-2 failure (timeout, exception, malformed reply,
  guard failure) or an ``unanswerable`` reply gets the fixed no-run copy,
  NO design run, NO version, and exactly one WARNING log; a ``request``
  reply routes to the design loop exactly as a non-question does;
* the /chat route wiring — the answer path emits ONE terminal done frame
  (``kind: "answer"``) and NO version is created; the design-loop path
  is unchanged for every non-answer message.

All fast (no LLM, no Docker).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import pytest

from d33d.question_answer import (
    ANSWER_DONE_KIND,
    COULD_NOT_ANSWER,
    DETERMINISTIC_AXIS_ADJECTIVES,
    DETERMINISTIC_AXIS_NOUNS,
    DETERMINISTIC_AXIS_SENTENCES,
    DETERMINISTIC_DIMENSION_LIST_FORMAT,
    DETERMINISTIC_DIMENSION_LIST_RE,
    NOT_ESTABLISHED,
    UNANSWERABLE_MISSING_TEMPLATE,
    build_answer_prompt,
    deterministic_axis_answer,
    guard_answer_numbers,
    is_candidate_question,
    parse_answer_reply,
    route_chat_message,
    state_block_numbers,
)

from tests.seam_schemas_d import validate_frames_stream
from tests.versioning.helpers import create_project, run_async

# ---------------------------------------------------------------------------
# Stage 1 — the deterministic question detector (pure function, no app)
# ---------------------------------------------------------------------------


class TestStage1Detector:
    """``is_candidate_question`` — the ticket's acceptance-criterion
    matrix, pinned as a pure function (no app, no LLM, no DB)."""

    def test_interrogative_question_with_question_mark(self) -> None:
        assert is_candidate_question("How tall is it now?")

    def test_interrogative_opener_no_question_mark(self) -> None:
        # The non-``?`` branch: the message starts with an interrogative
        # word (what/how/which/is/are/does/do/can/will/why/where/when).
        assert is_candidate_question("how tall is it now")
        assert is_candidate_question("What is the height?")
        assert is_candidate_question("Is it 12 mm?")
        assert is_candidate_question("which version is current")

    def test_imperative_message_not_a_candidate(self) -> None:
        # No ``?``, no interrogative opener → straight to the design loop.
        assert not is_candidate_question("make it taller")
        assert not is_candidate_question("Make it 15")
        assert not is_candidate_question("add a rib to the side")
        assert not is_candidate_question("change the bore")

    def test_imperative_wins_over_interrogative(self) -> None:
        # The ticket's edge case: the message ends with an imperative,
        # starts with an interrogative. The WHOLE message is scanned —
        # the imperative cue wins.
        msg = "Is it tall enough for a 12 mm shelf? Make it 15."
        assert not is_candidate_question(msg)

    def test_can_you_make_is_imperative(self) -> None:
        # The ticket's explicit edge: "Can you make it 15?" opens with an
        # interrogative ("can") but contains "make" — the multi-word
        # cue "can you make" overrides the interrogative opener.
        assert not is_candidate_question("Can you make it 15?")
        assert not is_candidate_question("could you add a fillet")

    def test_blank_message_not_a_candidate(self) -> None:
        assert not is_candidate_question("")
        assert not is_candidate_question("   ")

    def test_relative_cue_wider_is_not_a_candidate(self) -> None:
        # #261: "wider" is a relative cue in the lexicon and is now part
        # of _IMPERATIVE_RE. "Can it be 20 mm wider?" is a change request,
        # not a question — stage 1 rejects it.
        assert not is_candidate_question("Can it be 20 mm wider?")

    def test_relative_cue_taller_is_not_a_candidate(self) -> None:
        assert not is_candidate_question("Can it be 20 mm taller?")

    def test_relative_cue_deeper_is_not_a_candidate(self) -> None:
        assert not is_candidate_question("make it deeper")

    def test_global_cue_bigger_is_not_a_candidate(self) -> None:
        assert not is_candidate_question("can it be bigger?")

    def test_global_cue_half_the_size_is_not_a_candidate(self) -> None:
        assert not is_candidate_question("resize to half the size")

    def test_absolute_word_tall_is_still_a_candidate(self) -> None:
        # #261: "tall" is an ABSOLUTE word, not relative — it must NOT be
        # in _IMPERATIVE_RE. "how tall is it?" remains a candidate.
        assert is_candidate_question("how tall is it?")
        assert is_candidate_question("How tall is it now?")

    def test_absolute_word_wide_is_still_a_candidate(self) -> None:
        assert is_candidate_question("how wide is it?")

    def test_absolute_word_height_is_still_a_candidate(self) -> None:
        assert is_candidate_question("What is the height?")

    def test_comparison_question_stays_a_candidate(self) -> None:
        """Issue #261 fix batch: an interrogative message whose only
        lexicon imperative hits are immediately followed by "than" is a
        comparison question, not a change request — it stays a
        candidate (stage 2 answers or classifies it; no wasted render).
        The carve-out is lexicon-only: base imperative words (make,
        set, change, …) and the multi-word global forms ("half the
        size") still win and send the message to the loop."""
        assert is_candidate_question("is it taller than the shelf?")
        assert is_candidate_question("is it bigger than 20 mm?")

    def test_change_request_with_relative_cue_is_not_a_candidate(self) -> None:
        """"Can it be 20 mm wider?" is a change request — the relative
        cue is NOT in comparison form (no "than"), so it is not a
        candidate."""
        assert not is_candidate_question("can it be 20 mm wider?")

    def test_imperative_with_comparison_form_is_not_a_candidate(self) -> None:
        """"make it taller than 30 mm" — the base imperative word wins
        even though the relative word is in comparison form (the
        carve-out requires EVERY lexicon hit to be followed by "than",
        and base words are never carved out)."""
        assert not is_candidate_question("make it taller than 30 mm")

    def test_comparison_question_with_base_imperative_not_a_candidate(self) -> None:
        """"is it wider than 30 mm, can we change it?" — the base
        imperative word "change" is present: the carve-out only covers
        lexicon words, so the imperative wins."""
        assert not is_candidate_question("is it wider than 30 mm, can we change it?")

    def test_multiword_global_form_has_no_comparison_carveout(self) -> None:
        """The multi-word global forms ("half the size", …) have no
        "than" carve-out: "is it half the size of the other one?" stays
        non-candidate (accepted trade-off: distinguishing question-form
        from change-form requires intent classification, which stage 1
        is not). Pinned so a future change is a conscious decision."""
        assert not is_candidate_question("is it half the size of the other one?")

    def test_two_lexicon_hits_only_one_in_comparison_form(self) -> None:
        """"is it wider than 30 mm or taller than 20 mm" — both lexicon
        hits are in comparison form (each immediately followed by
        "than"), so the message stays a candidate."""
        assert is_candidate_question("is it wider than 30 mm or taller than 20 mm?")


# ---------------------------------------------------------------------------
# Stage 2 — the number guard (pure function)
# ---------------------------------------------------------------------------


def _entries(*rows: tuple[str, float | str | bool | None]) -> list[dict[str, Any]]:
    """Build a minimal design-state entry list from (name, value) rows."""
    out = []
    for name, value in rows:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            unit = "mm"
        elif value is None:
            unit = None
        else:
            unit = None
        out.append(
            {
                "name": name,
                "kind": "param",
                "label": name,
                "value": value,
                "unit": unit,
                "provenance": "assumed" if value is not None else "unknown",
            }
        )
    return out


class TestNumberGuard:
    """``guard_answer_numbers`` — the deterministic presence-only check
    (block numbers + the user's question numbers, issue #278)."""

    def test_number_in_block_passes(self) -> None:
        entries = _entries(("H", 12.0), ("W", 20.0))
        assert guard_answer_numbers("It is 12 mm tall.", entries)

    def test_invented_number_fails(self) -> None:
        entries = _entries(("H", 12.0), ("W", 20.0))
        assert not guard_answer_numbers("It is 15 mm tall.", entries)

    def test_question_number_licenses_answer(self) -> None:
        # issue #278 repro: "30" comes from the QUESTION, not the block
        # (the block carries only 43.9) — the answer that quotes 30
        # passes; 43.9 is licensed by the block.
        entries = _entries(("D", 43.9))
        assert guard_answer_numbers(
            "Yes — it's 43.9 mm deep, more than the 30 mm screw needs.",
            entries,
            question="Is it deep enough for a 30 mm screw?",
        )

    def test_number_in_neither_question_nor_block_fails(self) -> None:
        # The same screw question, answer citing 25 (in neither the
        # question nor the block) → the guard still rejects.
        entries = _entries(("D", 43.9))
        assert not guard_answer_numbers(
            "Yes — it's 43.9 mm deep, more than the 25 mm screw needs.",
            entries,
            question="Is it deep enough for a 30 mm screw?",
        )

    def test_spelled_out_question_number_licenses_answer(self) -> None:
        # The SAME extraction runs on both sides: "thirty" in the
        # question licenses both "30" and "thirty" in the answer.
        entries = _entries(("D", 43.9))
        assert guard_answer_numbers(
            "Yes, a thirty mm screw fits.",
            entries,
            question="Is it deep enough for a thirty mm screw?",
        )
        assert guard_answer_numbers(
            "Yes — it's 43.9 mm deep, more than the thirty mm screw needs.",
            entries,
            question="Is it deep enough for a thirty mm screw?",
        )

    def test_question_number_exact_tolerance(self) -> None:
        # The question's "30" licenses exactly 30 — an answer of
        # "30.5" fails (30.5 is in neither the question nor a 30.0
        # block; the block value of 30.5 does not license 30, and the
        # question's 30 does not license 30.5 — the same exact-match
        # within 1e-6 rule the block values get, mirroring
        # ``test_float_block_value_does_not_license_integer``).
        entries2 = _entries(("D", 30.0))
        assert not guard_answer_numbers(
            "It's 30.5 mm.", entries2, question="a 30 mm screw"
        )
        assert guard_answer_numbers(
            "It's 30 mm.", entries2, question="a 30 mm screw"
        )
        # The question's "30.0" licenses the block's 30 (symmetric):
        # question "a 30.0 mm screw" + block D 30.0 + answer "30.0".
        assert guard_answer_numbers(
            "It's 30.0 mm.", entries2, question="a 30.0 mm screw"
        )
        # The float-block direction with a question present: a block of
        # 30.5 does not license the answer's "30" (the question's "a 30
        # mm screw" licenses 30, but the answer's 30 must match a
        # LICENSED number within 1e-6 — 30 IS licensed by the question,
        # so "It's 30 mm." passes; "It's 30.5 mm." passes via the block;
        # the invented middle "It's 30.2 mm." fails via both).
        entries = _entries(("D", 30.5))
        assert guard_answer_numbers(
            "It's 30 mm.", entries, question="a 30 mm screw"
        )
        assert guard_answer_numbers(
            "It's 30.5 mm.", entries, question="a 30 mm screw"
        )
        assert not guard_answer_numbers(
            "It's 30.2 mm.", entries, question="a 30 mm screw"
        )

    def test_question_numbers_not_added_to_block(self) -> None:
        # ``state_block_numbers`` is the block's own set — the question's
        # numbers never leak into it (the question is not a design-state
        # source; only the guard's allowed set unions them).
        entries = _entries(("D", 43.9))
        allowed = state_block_numbers(entries)
        assert 30.0 not in allowed
        assert guard_answer_numbers(
            "30 mm.", entries, question="a 30 mm screw"
        )
        # Without the question, the same answer fails.
        assert not guard_answer_numbers("30 mm.", entries)

    def test_two_axis_numbers_pass(self) -> None:
        entries = _entries(("W", 20.0), ("D", 20.0))
        assert guard_answer_numbers("20 × 20", entries)

    def test_float_block_value_does_not_license_integer(self) -> None:
        # 12.5 in the block does NOT license "12" or "13".
        entries = _entries(("H", 12.5))
        assert not guard_answer_numbers("It is 12 mm.", entries)
        assert not guard_answer_numbers("It is 13 mm.", entries)
        assert guard_answer_numbers("It is 12.5 mm.", entries)

    def test_cross_axis_citation_passes_presence_only(self) -> None:
        # Known limitation (operator decision): "20 mm tall" when H=12,
        # W=20 PASSES the guard (20 IS in the block). The guard checks
        # digit presence, not parameter attribution.
        entries = _entries(("H", 12.0), ("W", 20.0))
        assert guard_answer_numbers("It is 20 mm tall.", entries)

    def test_word_only_answer_passes(self) -> None:
        entries = _entries(("H", 12.0))
        assert guard_answer_numbers("It is tall.", entries)

    def test_empty_block_no_numbers_pass(self) -> None:
        # A block with no numeric values: any number in the answer fails.
        entries = _entries(("note", "left-handed"))
        assert not guard_answer_numbers("It is 12 mm.", entries)
        assert guard_answer_numbers("It is left-handed.", entries)

    def test_block_only_call_still_rejects_question_number(self) -> None:
        # Without a ``question`` argument the guard behaves exactly as
        # before: the 30 is not in the block → fails.
        entries = _entries(("D", 43.9))
        assert not guard_answer_numbers(
            "Yes — it's 43.9 mm deep, more than the 30 mm screw needs.",
            entries,
        )

    def test_question_kwarg(self) -> None:
        # The production seam passes the raw question via the ``question``
        # kwarg (the guard extracts its numbers internally).
        entries = _entries(("D", 43.9))
        answer = "Yes — it's 43.9 mm deep, more than the 30 mm screw needs."
        q = "Is it deep enough for a 30 mm screw?"
        assert guard_answer_numbers(
            answer, entries, question=q
        )
        assert not guard_answer_numbers(
            answer.replace("30", "25"),
            entries,
            question=q,
        )


class TestParseAnswerReply:
    """``parse_answer_reply`` — the stage-2 reply codec (the #260
    three-way ``kind`` shape, plus the legacy ``answerable`` bool for
    backward compatibility)."""

    def test_valid_json_kind_answer(self) -> None:
        assert parse_answer_reply(
            '{"kind": "answer", "answer": "It is 12 mm tall."}'
        ) == ("answer", "It is 12 mm tall.", None)

    def test_kind_unanswerable(self) -> None:
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": ""}'
        ) == ("unanswerable", "", None)

    def test_kind_unanswerable_with_missing_carries_it(self) -> None:
        # issue #278: an unanswerable reply may carry ``missing`` (the
        # noun phrase naming the unknown fact) — the parser validates it
        # (single point of truth for the missing shape); the ``answer``
        # field is ignored in favour of the missing-built copy downstream.
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": "", '
            '"missing": "the shelf\'s height"}'
        ) == ("unanswerable", "", "the shelf's height")
        # A non-empty ``answer`` on an unanswerable reply is still
        # well-formed (not malformed) — its value rides the tuple but
        # the route never uses it.
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": "ignored", '
            '"missing": "the shelf\'s height"}'
        ) == ("unanswerable", "ignored", "the shelf's height")

    def test_kind_unanswerable_missing_non_string_returns_none(self) -> None:
        # A ``missing`` that is not a string (number, null, array) is
        # NOT malformed — it is invalid, the parser returns None and
        # the route falls back to NOT_ESTABLISHED. Never a crash, never
        # malformed.
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": "", "missing": 42}'
        ) == ("unanswerable", "", None)
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": "", "missing": null}'
        ) == ("unanswerable", "", None)
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": "", "missing": ["a", "b"]}'
        ) == ("unanswerable", "", None)

    def test_kind_request(self) -> None:
        assert parse_answer_reply(
            '{"kind": "request", "answer": ""}'
        ) == ("request", "", None)

    def test_kind_answer_extra_missing_field_ignored(self) -> None:
        # A kind "answer" or "request" reply carrying an extra
        # ``missing`` field must not break the closed-shape contract —
        # ``missing`` is only honoured for unanswerable replies.
        assert parse_answer_reply(
            '{"kind": "answer", "answer": "It is 12 mm tall.", '
            '"missing": "ignored"}'
        ) == ("answer", "It is 12 mm tall.", None)
        assert parse_answer_reply(
            '{"kind": "request", "answer": "", "missing": "x"}'
        ) == ("request", "", None)

    def test_kind_must_be_closed_set(self) -> None:
        assert parse_answer_reply('{"kind": "maybe", "answer": "x"}') is None
        assert parse_answer_reply('{"kind": true, "answer": ""}') is None

    def test_kind_answer_requires_nonempty_answer(self) -> None:
        assert parse_answer_reply('{"kind": "answer", "answer": ""}') is None
        assert parse_answer_reply('{"kind": "answer", "answer": "  "}') is None

    def test_legacy_answerable_true_maps_to_answer(self) -> None:
        # The old boolean schema may still come back from the model:
        # mapped, never a false "unanswerable".
        assert parse_answer_reply(
            '{"answerable": true, "answer": "It is 12 mm tall."}'
        ) == ("answer", "It is 12 mm tall.", None)

    def test_legacy_answerable_false_maps_to_request(self) -> None:
        # Preserving today's routing: a legacy false reply is a change
        # REQUEST to the design loop, never a false "unanswerable".
        assert parse_answer_reply(
            '{"answerable": false, "answer": ""}'
        ) == ("request", "", None)

    def test_legacy_answerable_true_missing_field_not_honoured(self) -> None:
        # The legacy shape has no missing field: a legacy reply carrying
        # ``missing`` still maps onto the closed set and the missing
        # value is ignored (never an unanswerable outcome).
        assert parse_answer_reply(
            '{"answerable": false, "answer": "", "missing": "x"}'
        ) == ("request", "", None)

    def test_legacy_answerable_true_empty_answer_is_malformed(self) -> None:
        assert parse_answer_reply('{"answerable": true, "answer": ""}') is None

    def test_both_shapes_is_malformed(self) -> None:
        # A reply carrying both discriminators is ambiguous: malformed.
        assert parse_answer_reply(
            '{"kind": "answer", "answerable": true, "answer": "x"}'
        ) is None

    def test_malformed_returns_none(self) -> None:
        assert parse_answer_reply("not json") is None
        assert parse_answer_reply("") is None
        assert parse_answer_reply('{"answerable": "yes"}') is None
        assert parse_answer_reply('{"answerable": true}') is None  # no answer key
        assert parse_answer_reply('{"kind": "answer"}') is None  # no answer key
        assert parse_answer_reply('{"kind": "unanswerable"}') is None

    def test_json_in_surrounding_prose(self) -> None:
        assert parse_answer_reply(
            'Here is the answer: {"answerable": true, "answer": "12 mm"}'
        ) == ("answer", "12 mm", None)
        assert parse_answer_reply(
            'Sure: {"kind": "request", "answer": ""}'
        ) == ("request", "", None)


# ---------------------------------------------------------------------------
# Stage 2 — the prompt (the block must be present; no confirmation offer)
# ---------------------------------------------------------------------------


class TestBuildAnswerPrompt:
    """``build_answer_prompt`` — the stage-2 user message (the #260
    three-way ``kind`` contract, one example per kind)."""

    def test_prompt_contains_block_values(self) -> None:
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert "12" in prompt
        assert "How tall is it?" in prompt

    def test_prompt_forbids_offering_to_set(self) -> None:
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        # The operator's decision: the prompt must NOT ask the model to
        # offer to set/confirm values.
        assert "do NOT offer to change, set, or confirm" in prompt

    def test_prompt_asks_for_three_kinds_with_examples(self) -> None:
        # #260: the reply schema is the closed three-way kind, with one
        # example per kind in the prompt.
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert '{"kind": "answer"|' in prompt
        assert '"unanswerable"|"request", "answer": "…"}' in prompt
        assert "\"answer\" — the block contains every value you need" in prompt
        assert "\"unanswerable\" — a genuine question" in prompt
        assert "\"request\" — the message asks for a change" in prompt
        # One example per kind (the operator decision).
        assert "\"How tall is it now?\" with the height stated 12 mm" in prompt
        # The unanswerable example now carries the ``missing`` field
        # (issue #278): "Is it taller than the shelf?" → "the shelf's
        # height" — the worked example the operator decision pins.
        assert "Is it taller than the shelf?" in prompt
        assert "the shelf\'s height" in prompt
        assert "\"Can it be 20 mm wider?\"" in prompt

    def test_prompt_no_longer_speaks_the_answerable_bool(self) -> None:
        # The legacy boolean schema is retired from the production prompt
        # (parsing still accepts it for old-style model replies).
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert '"answerable"' not in prompt
        assert "answerable: true" not in prompt
        assert "answerable: false" not in prompt

    def test_prompt_distinguishes_user_and_model_source_disagreement(self) -> None:
        # issue #264 task-c: the prompt must distinguish the two kinds
        # of disagreement. User-source: "you said X, I measured Y".
        # Model-source: "I set X, it measures Y".
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert "user-source disagreement" in prompt
        assert "you said X, I measured Y" in prompt
        assert "model-source disagreement" in prompt
        assert "I set X, it measures Y" in prompt


# ---------------------------------------------------------------------------
# Stage 2 — the route seam (stub answer_fn, no app)
# ---------------------------------------------------------------------------


def _latest(params: dict, stated: dict | None = None, bbox: dict | None = None) -> dict:
    return {
        "params": params,
        "stated_dims": stated,
        "bbox": bbox,
        "param_meta": None,
    }


class TestRouteChatMessage:
    """``route_chat_message`` — the pre-route decision (stub answer_fn)."""

    def _answer_edge(self, reply: str):
        async def _edge(question: str, entries: list) -> str:
            return reply
        return _edge

    def test_answered_question_returns_kind_answer(self) -> None:
        # A non-axis-size question ("How tall is it now?" is now answered
        # deterministically by the #263 stage — this test exercises the
        # stage-2 LLM path, so it uses a question the deterministic stage
        # does not take).
        latest = _latest({"H": 12.0, "W": 20.0})
        edge = self._answer_edge('{"answerable": true, "answer": "It is 12 mm tall."}')
        result = run_async_safe(route_chat_message("What is the material?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": "It is 12 mm tall."}

    def test_no_versions_returns_none(self) -> None:
        # No version → the deterministic stage does not run (the existing
        # early exit runs first). A non-axis question is used to make it
        # clear the test is about the no-version path, not the
        # deterministic stage.
        result = run_async_safe(route_chat_message("What is the material?", None))
        assert result is None

    def test_non_question_returns_none(self) -> None:
        latest = _latest({"H": 12.0})
        result = run_async_safe(route_chat_message("make it taller", latest))
        assert result is None

    def test_imperative_question_returns_none(self) -> None:
        latest = _latest({"H": 12.0})
        msg = "Is it tall enough for a 12 mm shelf? Make it 15."
        result = run_async_safe(route_chat_message(msg, latest))
        assert result is None

    def test_kind_unanswerable_returns_not_established_no_run(self) -> None:
        # #260: kind "unanswerable" → the fixed no-run reply (NOT the
        # design loop — today's buggy behaviour, which inverted this).
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"kind": "unanswerable", "answer": ""}')
        result = run_async_safe(
            route_chat_message("What colour is it?", latest, edge)
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}

    def test_kind_unanswerable_valid_missing_builds_reply(self) -> None:
        # issue #278: an unanswerable reply with a valid ``missing``
        # field → the reply built from the parameterised template
        # (exact text), not NOT_ESTABLISHED and not COULD_NOT_ANSWER.
        latest = _latest({"H": 12.0})
        edge = self._answer_edge(
            '{"kind": "unanswerable", "answer": "", '
            '"missing": "the shelf\'s height"}'
        )
        result = run_async_safe(
            route_chat_message("Is it taller than the shelf?", latest, edge)
        )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": UNANSWERABLE_MISSING_TEMPLATE.format(
                missing="the shelf's height"
            ),
        }
        assert result["answer"] == (
            "I don't know the shelf's height. Tell me and I'll check — "
            "nothing was changed."
        )

    def test_kind_unanswerable_missing_whitespace_returns_not_established(
        self,
    ) -> None:
        # A ``missing`` that is blank/whitespace-only → the fixed
        # NOT_ESTABLISHED fallback (never an empty template fill).
        latest = _latest({"H": 12.0})
        for reply in (
            '{"kind": "unanswerable", "answer": "", "missing": "  "}',
            '{"kind": "unanswerable", "answer": "", "missing": ""}',
        ):
            edge = self._answer_edge(reply)
            result = run_async_safe(
                route_chat_message("What colour is it?", latest, edge)
            )
            assert result == {
                "kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED
            }, reply

    def test_kind_unanswerable_missing_too_long_returns_not_established(
        self,
    ) -> None:
        # A ``missing`` of 61+ chars (after trim) → NOT_ESTABLISHED.
        long_fact = "the " + "x" * 58  # 62 chars
        latest = _latest({"H": 12.0})
        edge = self._answer_edge(
            '{"kind": "unanswerable", "answer": "", "missing": "' + long_fact + '"}'
        )
        result = run_async_safe(
            route_chat_message("What colour is it?", latest, edge)
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}
        # 60 chars is the bound: a 60-char missing is valid.
        edge60 = self._answer_edge(
            '{"kind": "unanswerable", "answer": "", "missing": "' + "y" * 60 + '"}'
        )
        result60 = run_async_safe(
            route_chat_message("What colour is it?", latest, edge60)
        )
        assert result60 == {
            "kind": ANSWER_DONE_KIND,
            "answer": UNANSWERABLE_MISSING_TEMPLATE.format(missing="y" * 60),
        }

    def test_kind_unanswerable_missing_digit_returns_not_established(self) -> None:
        # A ``missing`` containing a digit → NOT_ESTABLISHED.
        latest = _latest({"H": 12.0})
        edge = self._answer_edge(
            '{"kind": "unanswerable", "answer": "", "missing": "the 2nd shelf\'s height"}'
        )
        result = run_async_safe(
            route_chat_message("Is it taller than the shelf?", latest, edge)
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}

    def test_kind_unanswerable_missing_sentence_punctuation_returns_not_established(
        self,
    ) -> None:
        # Sentence punctuation other than an apostrophe (period, comma,
        # semicolon, colon, question mark, exclamation) → NOT_ESTABLISHED.
        latest = _latest({"H": 12.0})
        for bad in ("the shelf's height.", "the shelf, if any", "the shelf;", "the shelf?", "the shelf!", "the shelf: "):
            edge = self._answer_edge(
                '{"kind": "unanswerable", "answer": "", "missing": "' + bad + '"}'
            )
            result = run_async_safe(
                route_chat_message("Is it taller than the shelf?", latest, edge)
            )
            assert result == {
                "kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED
            }, bad

    def test_kind_unanswerable_missing_markup_returns_not_established(
        self,
    ) -> None:
        # issue #278 adversarial finding: HTML / markup characters in
        # ``missing`` must be rejected (the validation is the security
        # boundary — the SPA renders the done frame verbatim; a future
        # markdown/HTML rendering must not turn model-influenced text
        # into stored XSS). The character class regex had an unescaped
        # ``[`` that closed the class early, matching nothing — this is
        # the regression pin.
        latest = _latest({"H": 12.0})
        for bad in (
            "<b>x</b>",
            "the <script> height",
            "the [shelf] height",
            "the {shelf} height",
            "the (shelf) height",
            "the & shelf height",
        ):
            edge = self._answer_edge(
                '{"kind": "unanswerable", "answer": "", "missing": "' + bad + '"}'
            )
            result = run_async_safe(
                route_chat_message("Is it taller than the shelf?", latest, edge)
            )
            assert result == {
                "kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED
            }, bad

    def test_kind_unanswerable_missing_non_string_returns_not_established(self) -> None:
        # A ``missing`` that is not a string (number, null, array) →
        # NOT_ESTABLISHED. Never malformed (no COULD_NOT_ANSWER), never
        # a crash.
        latest = _latest({"H": 12.0})
        for reply in (
            '{"kind": "unanswerable", "answer": "", "missing": 42}',
            '{"kind": "unanswerable", "answer": "", "missing": null}',
            '{"kind": "unanswerable", "answer": "", "missing": ["a", "b"]}',
            '{"kind": "unanswerable", "answer": "", "missing": true}',
        ):
            edge = self._answer_edge(reply)
            result = run_async_safe(
                route_chat_message("What colour is it?", latest, edge)
            )
            assert result == {
                "kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED
            }, reply

    def test_kind_unanswerable_nonempty_answer_ignored_in_favour_of_missing(
        self,
    ) -> None:
        # A non-empty ``answer`` on an unanswerable reply is well-formed
        # and IGNORED in favour of the missing-built copy (operator
        # decision: the wire shape is
        # {"kind":"unanswerable","answer":"","missing":"..."} but a
        # non-empty answer never becomes malformed).
        latest = _latest({"H": 12.0})
        edge = self._answer_edge(
            '{"kind": "unanswerable", "answer": "some prose", '
            '"missing": "the shelf\'s height"}'
        )
        result = run_async_safe(
            route_chat_message("Is it taller than the shelf?", latest, edge)
        )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": UNANSWERABLE_MISSING_TEMPLATE.format(
                missing="the shelf's height"
            ),
        }

    def test_kind_request_returns_none_goes_to_loop(self) -> None:
        # #260: kind "request" → the design loop, exactly as a
        # non-question message (the model says the message asks for a
        # change). The message must pass stage 1 (no imperative cue) so
        # it reaches stage 2.
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"kind": "request", "answer": ""}')
        result = run_async_safe(
            route_chat_message("What is the material?", latest, edge)
        )
        assert result is None

    def test_invented_number_guard_fails_returns_could_not_answer(self) -> None:
        # #260: a guard failure is a FAILED ANSWER — the fixed no-run
        # reply, not a design run. Uses a non-axis question so the
        # deterministic stage does not intercept it.
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"answerable": true, "answer": "It is 15 mm tall."}')
        result = run_async_safe(route_chat_message("What is the material?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_malformed_reply_returns_could_not_answer(self) -> None:
        latest = _latest({"H": 12.0})
        edge = self._answer_edge("not json at all")
        result = run_async_safe(route_chat_message("What is the material?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_exception_from_edge_returns_could_not_answer(self) -> None:
        latest = _latest({"H": 12.0})

        async def _raise_edge(question: str, entries: list) -> str:
            raise RuntimeError("simulated stage-2 failure")

        result = run_async_safe(
            route_chat_message("What is the material?", latest, _raise_edge)
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_stage2_timeout_returns_could_not_answer(self) -> None:
        # The hard timeout at a short test value (the 10 s production
        # bound's contract, pinned in <1 s of wall clock).
        latest = _latest({"H": 12.0})

        async def _hanging_edge(question: str, entries: list) -> str:
            await asyncio.sleep(0.5)  # well past the 0.05 s bound
            return ""

        result = run_async_safe(
            route_chat_message(
                "What is the material?", latest, _hanging_edge, timeout=0.05
            )
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_no_answer_edge_returns_none(self) -> None:
        # A non-axis question with no answer edge → None (the design
        # loop). An axis-size question would be answered deterministically
        # even with no edge (the deterministic stage does not need an
        # edge), so this test uses a non-axis question to exercise the
        # no-edge path.
        latest = _latest({"H": 12.0})
        result = run_async_safe(route_chat_message("What is the material?", latest))
        assert result is None


def run_async_safe(coro) -> Any:
    """Drive a coroutine under a fresh event loop (no app, no lifespan)."""
    return asyncio.run(coro)


# ---------------------------------------------------------------------------
# Issue #263 — the deterministic axis-size stage (no LLM, no app)
# ---------------------------------------------------------------------------


def _latest263(
    params: dict | None = None,
    stated: dict | None = None,
    bbox: dict | None = None,
    name: str | None = None,
    param_meta: dict | None = None,
) -> dict:
    """Build a minimal version dict for the #263 deterministic-stage tests."""
    out: dict = {
        "params": params or {},
        "stated_dims": stated,
        "bbox": bbox,
        "param_meta": param_meta,
    }
    if name is not None:
        out["name"] = name
    return out


class TestDeterministicAxisStage:
    """Issue #263: the deterministic axis-size stage — a stage-1 candidate
    that asks for exactly one axis's size (or the full dimension list) is
    answered from the design state with NO LLM call, NO timeout, NO
    guard. The answer rides the #249 plain-message path.

    Uses ``deterministic_axis_answer`` directly (the pure function the
    route calls between stage 1 and stage 2) and ``route_chat_message``
    for the integration cases (no LLM edge called, no version created).
    """

    def test_measured_only_bbox_z(self) -> None:
        # "How tall is it now?" on a version with bbox z=12.0 and no
        # stated H → "It measures 12.0 mm tall."
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "It measures 12.0\u202fmm tall."

    def test_measured_only_bbox_x(self) -> None:
        # "How wide is it?" → W (bbox x).
        latest = _latest263(bbox={"x": 25.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("How wide is it?", latest)
        assert result == "It measures 25.0\u202fmm wide."

    def test_measured_only_bbox_y(self) -> None:
        # "what's the depth?" → D (bbox y).
        latest = _latest263(bbox={"x": 20.0, "y": 30.0, "z": 12.0})
        result = deterministic_axis_answer("what's the depth?", latest)
        assert result == "It measures 30.0\u202fmm deep."

    def test_stated_and_measured_agree(self) -> None:
        # Stated H=12 and matching bbox z=12 → the stated+measured
        # sentence ("you said that, and I measured it").
        latest = _latest263(stated={"H": 12.0}, bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "It's 12.0\u202fmm tall \u2014 you said that, and I measured it."

    def test_stated_and_measured_disagree(self) -> None:
        # Stated H=12 and mismatched bbox z=15 → the disagrees sentence
        # (both numbers, mm()-formatted).
        latest = _latest263(stated={"H": 12.0}, bbox={"x": 20.0, "y": 20.0, "z": 15.0})
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "You said 12.0\u202fmm; what came out measures 15.0\u202fmm."

    def test_stated_only_no_measurement(self) -> None:
        # Stated H=12, no bbox → the stated-only sentence.
        latest = _latest263(stated={"H": 12.0})
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "You said 12.0\u202fmm tall. Nothing has measured it yet."

    def test_zero_extent_bbox_stated_only(self) -> None:
        # Stated H=12 with bbox z=0 (zero extent → abstain) → the
        # stated-only sentence, not "not established" and not disagrees.
        latest = _latest263(stated={"H": 12.0}, bbox={"x": 20.0, "y": 20.0, "z": 0})
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "You said 12.0\u202fmm tall. Nothing has measured it yet."

    def test_not_established(self) -> None:
        # No bbox, no stated → "not established".
        latest = _latest263()
        result = deterministic_axis_answer("How tall is it now?", latest)
        assert result == "The height isn't established yet."

    def test_not_established_width(self) -> None:
        latest = _latest263()
        result = deterministic_axis_answer("How wide is it?", latest)
        assert result == "The width isn't established yet."

    def test_not_established_depth(self) -> None:
        latest = _latest263()
        result = deterministic_axis_answer("what's the depth?", latest)
        assert result == "The depth isn't established yet."

    def test_dimension_list(self) -> None:
        # "How big is it?" → the full W × D × H list.
        latest = _latest263(bbox={"x": 20.0, "y": 25.0, "z": 12.0})
        result = deterministic_axis_answer("How big is it?", latest)
        assert result == "It measures 20.0\u202fmm × 25.0\u202fmm × 12.0\u202fmm."

    def test_dimension_list_with_unestablished_axis(self) -> None:
        # W and D measured, H not established → dash for H.
        latest = _latest263(bbox={"x": 20.0, "y": 25.0, "z": 0})
        result = deterministic_axis_answer("How big is it?", latest)
        # z=0 → the H axis row is omitted (zero extent abstains), so H
        # falls to the bbox fallback which also abstains (z=0) → "not
        # established" → dash.
        assert result == "It measures 20.0\u202fmm × 25.0\u202fmm × \u2014."

    def test_list_wins_over_single_axis_word(self) -> None:
        # "How wide is it, and what are the dimensions?" → the list
        # (the operator decision: a message matching BOTH a dimension-list
        # trigger and a single axis word answers the list).
        latest = _latest263(bbox={"x": 20.0, "y": 25.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How wide is it, and what are the dimensions?", latest
        )
        assert result == "It measures 20.0\u202fmm × 25.0\u202fmm × 12.0\u202fmm."

    def test_dimension_list_triggers_closed_list(self) -> None:
        # The dimension-list trigger list is CLOSED (operator decision).
        # Every member of the closed set answers the list; any other
        # phrasing falls through (None).
        latest = _latest263(bbox={"x": 20.0, "y": 25.0, "z": 12.0})
        expected = "It measures 20.0\u202fmm × 25.0\u202fmm × 12.0\u202fmm."
        for trigger in (
            "What are the dimensions?",
            "what are its dimensions?",
            "What size is it?",
            "How big is it?",
            "How large is it?",
            "What's the size?",
        ):
            assert DETERMINISTIC_DIMENSION_LIST_RE.search(trigger) is not None, (
                f"closed-list trigger {trigger!r} not matched"
            )
            result = deterministic_axis_answer(trigger, latest)
            assert result == expected, f"trigger {trigger!r} → {result!r}"
        # Anything else is NOT a dimension-list trigger: it falls through
        # to the single-axis rule or to stage 2.
        for non_trigger in (
            "What are the measurements?",
            "How large is the hole?",
            "What is the size of the post?",
            "What is the material?",
            "How big is the shelf bracket?",
        ):
            result = deterministic_axis_answer(non_trigger, latest)
            assert result is None, (
                f"{non_trigger!r} must fall through, got {result!r}"
            )

    def test_param_row_never_an_answer_source(self) -> None:
        # A param row named "H" (assumed) is NEVER an answer source —
        # the deterministic stage filters on kind == "axis" explicitly.
        # Param H=40 + bbox z=43.8 → the answer uses 43.8 (the axis row
        # from the bbox), not 40 (the param row).
        latest = _latest263(
            params={"H": 40.0}, bbox={"x": 43.8, "y": 20.0, "z": 43.8}
        )
        result = deterministic_axis_answer("How tall is it now?", latest)
        # The axis row (from the bbox) says 43.8 measured; the param row
        # says 40 assumed. The answer must use 43.8.
        assert result == "It measures 43.8\u202fmm tall."

    def test_model_source_disagrees_param_row_never_an_answer_source(self) -> None:
        # Issue #264: a param row with ``disagrees_source: "model"``
        # (an assumed param whose declared axis contests the measurement)
        # is NEVER an answer source — the deterministic stage filters on
        # ``kind == "axis"`` explicitly. After #264, axis rows are
        # measured or user-disagrees, so the axis-row answer is always
        # correct even when a model-source param row also claims the
        # axis. Here: spacer_width=40 (assumed, axis W) + bbox x=43.8 →
        # the param row renders disagrees_source "model" with value 43.8
        # (the measured extent) and stated_value 40; the W axis row is
        # measured at 43.8. "How wide is it?" answers 43.8 (the axis
        # row), never 40 (the model's value).
        from d33d.design_state import state_block_for_version

        entries = state_block_for_version(
            {"spacer_width": 40.0},
            {"x": 43.8, "y": 43.9, "z": 12.0},
            None,
            {"spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"}},
        )
        # Sanity: the block carries a model-source disagrees param row
        # AND a W axis row (measured at 43.8).
        model_rows = [
            e
            for e in entries
            if e.get("kind") == "param"
            and e.get("provenance") == "disagrees"
            and e.get("disagrees_source") == "model"
        ]
        assert model_rows, f"expected a model-source disagrees row: {entries}"
        w_axis_rows = [
            e for e in entries if e.get("kind") == "axis" and e.get("name") == "W"
        ]
        assert w_axis_rows and w_axis_rows[0]["value"] == 43.8

        latest = _latest263(
            params={"spacer_width": 40.0},
            bbox={"x": 43.8, "y": 43.9, "z": 12.0},
            param_meta={
                "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"}
            },
        )
        result = deterministic_axis_answer("How wide is it?", latest)
        # The axis row (43.8) wins; the model's 40 never answers.
        assert result == "It measures 43.8\u202fmm wide."

    def test_feature_noun_falls_through(self) -> None:
        # "How tall is the post?" → None (feature noun, not the part).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("How tall is the post?", latest)
        assert result is None

    def test_feature_noun_height_of_hole(self) -> None:
        # "What is the height of the hole?" → None (feature noun).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("What is the height of the hole?", latest)
        assert result is None

    def test_thick_not_in_lexicon(self) -> None:
        # "How thick is the wall?" → None ("thick" is excluded from the
        # lexicon — not an absolute axis word).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("How thick is the wall?", latest)
        assert result is None

    def test_relative_cue_not_a_size_question(self) -> None:
        # "Is it taller than 20 mm?" → None ("taller" is a relative cue;
        # stage 1 already rejects it per #261, but the deterministic
        # stage also does not take it).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer("Is it taller than 20 mm?", latest)
        assert result is None

    def test_version_name_match(self) -> None:
        # Version named "Shelf bracket": "how tall is the shelf bracket?"
        # → H (the version name matches the noun phrase).
        latest = _latest263(
            bbox={"x": 20.0, "y": 20.0, "z": 12.0}, name="Shelf bracket"
        )
        result = deterministic_axis_answer("How tall is the shelf bracket?", latest)
        assert result == "It measures 12.0\u202fmm tall."

    def test_version_name_no_match(self) -> None:
        # Version named "Shelf bracket": "how tall is the shelf?" → None
        # (the noun "the shelf" does not match the version name).
        latest = _latest263(
            bbox={"x": 20.0, "y": 20.0, "z": 12.0}, name="Shelf bracket"
        )
        result = deterministic_axis_answer("How tall is the shelf?", latest)
        assert result is None

    def test_proposed_change_question_with_number_falls_through(self) -> None:
        # Adversarial finding (PR #272 round 1): a message carrying a
        # number ("Can it be 15 mm tall?") is a PROPOSED change or a
        # yes/no check, not a plain size question — the target-number
        # guard makes the deterministic stage abstain (fall through to
        # stage 2) instead of answering with the part's CURRENT height.
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        for msg in (
            "Can it be 15 mm tall?",
            "Can it be 20 mm deep?",
            "Is it 12 mm tall?",
            "How big is it, 20 mm?",
        ):
            assert deterministic_axis_answer(msg, latest) is None, (
                f"{msg!r} must fall through (target-number guard), "
                "got a deterministic answer"
            )

    def test_number_in_version_name_does_not_trip_guard(self) -> None:
        # The guard scans the MESSAGE, not the version name: a version
        # named "12 mm shelf spacer" asked about by a digit-free question
        # still answers deterministically.
        latest = _latest263(
            bbox={"x": 20.0, "y": 20.0, "z": 12.0}, name="12 mm shelf spacer"
        )
        result = deterministic_axis_answer(
            "How tall is the 12 mm shelf spacer?", latest
        )
        # The message carries a digit (the name's "12"), so the guard
        # abstains — a digit anywhere in the message is a conservative
        # abstain, even inside a version-name reference.
        assert result is None
        # A digit-free phrasing of the same question still answers.
        latest2 = _latest263(
            bbox={"x": 20.0, "y": 20.0, "z": 12.0}, name="shelf spacer"
        )
        result2 = deterministic_axis_answer("How tall is the shelf spacer?", latest2)
        assert result2 == "It measures 12.0\u202fmm tall."

    def test_version_name_case_insensitive(self) -> None:
        # Version named "Shelf bracket": "how tall is the SHELF BRACKET?"
        # → H (case-insensitive match).
        latest = _latest263(
            bbox={"x": 20.0, "y": 20.0, "z": 12.0}, name="Shelf bracket"
        )
        result = deterministic_axis_answer("How tall is the SHELF BRACKET?", latest)
        assert result == "It measures 12.0\u202fmm tall."

    def test_no_version_returns_none(self) -> None:
        # latest=None → the deterministic stage does not run (the
        # existing early exit in route_chat_message runs first).
        result = deterministic_axis_answer("How tall is it now?", None)
        assert result is None

    def test_route_deterministic_no_edge_called(self) -> None:
        # Integration: "How tall is it now?" on a version with bbox
        # z=12.0 and no stated H → answered deterministically, the
        # answer-edge stub is NOT called, no version is created.
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        result = run_async_safe(
            route_chat_message("How tall is it now?", latest, _edge)
        )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": "It measures 12.0\u202fmm tall.",
        }
        assert not edge_called[0], "the answer edge must NOT be called for a deterministic answer"

    def test_route_deterministic_no_version_created(self) -> None:
        # Integration: the deterministic stage does not create a version
        # (the answer is a plain message, no design run).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})

        async def _edge(question: str, entries: list) -> str:
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        result = run_async_safe(
            route_chat_message("How tall is it now?", latest, _edge)
        )
        # The result is a plain answer message (no version, no design run).
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND
        assert "12.0" in result["answer"]

    def test_route_axis_question_no_edge_still_answered(self) -> None:
        # An axis-size question with NO answer edge is still answered
        # deterministically (the deterministic stage does not need an
        # edge). A non-axis question with no edge returns None.
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = run_async_safe(
            route_chat_message("How tall is it now?", latest)
        )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": "It measures 12.0\u202fmm tall.",
        }

    def test_route_thick_wall_not_answered_deterministically(self) -> None:
        # "How thick is the wall?" is NOT a size question for the
        # deterministic stage ("thick" is excluded from the lexicon, and
        # "wall" is a feature noun). It falls through to stage 2:
        # with no answer edge, the route returns None (design loop).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = run_async_safe(
            route_chat_message("How thick is the wall?", latest)
        )
        assert result is None


class TestDeterministicWarningLog:
    """Issue #263: every deterministic answer logs exactly ONE WARNING
    naming the outcome ("deterministic"), the axis (or the dimension-list
    marker), and the provenance class — NEVER the message text or the
    answer text (no PII in logs)."""

    def _latest(self, params: dict, stated: dict | None, bbox: dict | None) -> dict:
        return {
            "params": params,
            "stated_dims": stated,
            "bbox": bbox,
            "param_meta": None,
        }

    def _warnings(self, caplog) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_single_axis_emits_one_warning_axis_and_provenance(self, caplog) -> None:
        latest = self._latest({}, None, {"x": 20.0, "y": 20.0, "z": 12.0})
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message("How tall is it now?", latest)
            )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": "It measures 12.0\u202fmm tall.",
        }
        warnings = self._warnings(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert "outcome=deterministic" in msg
        assert "axis=H" in msg
        assert "provenance=measured" in msg
        # Never the message or the answer text (no PII in logs).
        assert "How tall is it now?" not in msg
        assert "12.0" not in msg

    def test_dimension_list_emits_one_warning_list_and_provenance_classes(
        self, caplog
    ) -> None:
        latest = self._latest({}, None, {"x": 20.0, "y": 25.0, "z": 12.0})
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(route_chat_message("How big is it?", latest))
        assert result is not None
        warnings = self._warnings(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert "outcome=deterministic" in msg
        assert "axis=list" in msg
        # The dimension list reports each axis's provenance class
        # (W/D/H order, "+"-joined — a closed vocabulary, no free text).
        assert "provenance=measured+measured+measured" in msg
        assert "How big is it?" not in msg

    def test_not_established_emits_one_warning(self, caplog) -> None:
        latest = self._latest({}, None, None)
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(route_chat_message("How tall is it now?", latest))
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": "The height isn't established yet.",
        }
        warnings = self._warnings(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert "outcome=deterministic" in msg
        assert "axis=H" in msg
        assert "provenance=not_established" in msg
        # No PII: no message text, no number.
        assert "How tall is it now?" not in msg
        assert "12.0" not in msg

    def test_disagrees_emits_one_warning(self, caplog) -> None:
        latest = self._latest({}, {"H": 12.0}, {"x": 20.0, "y": 20.0, "z": 15.0})
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(route_chat_message("How tall is it now?", latest))
        assert result is not None
        warnings = self._warnings(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        msg = warnings[0].getMessage()
        assert "outcome=deterministic" in msg
        assert "axis=H" in msg
        assert "provenance=disagrees" in msg
        # No PII: neither the stated number nor the measured number
        # (the provenance class is a closed vocabulary token, not a value).
        assert "12.0" not in msg
        assert "15.0" not in msg

    def test_non_axis_question_falls_through_no_deterministic_warning(
        self, caplog
    ) -> None:
        # A stage-1 candidate the deterministic stage does not take
        # ("What is the material?" — no axis word) falls through to
        # stage 2; no deterministic WARNING is emitted (the stage-2
        # outcome logs its own WARNING via ask_answer_call).
        latest = self._latest({"H": 12.0}, None, None)

        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message("What is the material?", latest, _edge)
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": "It is 12 mm tall."}
        warnings = self._warnings(caplog)
        # The only WARNING is the stage-2 outcome ("answer"), NOT
        # a deterministic one.
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=answer" in warnings[0].getMessage()
        assert "outcome=deterministic" not in warnings[0].getMessage()


class TestDeterministicCopyDeckParity:
    """Issue #263: the backend's deterministic answer sentences are
    pinned against the exact strings the W263 design-contract test pins
    for ``web/src/copy.ts``'s ``deterministicAnswer`` deck (the same
    strings the #250 ``confirmOffer`` and #260 ``answerRoute`` parity
    tests pin) — a deck edit without the backend (or vice versa) is a
    drift this catches. No placeholder surgery: each backend template
    is formatted with fixed sample values and compared to the exact
    sentences."""

    def test_backend_deterministic_sentences_match_pinned_deck_strings(self) -> None:
        # The W263 design-contract test's fixed sample values (the
        # acceptance criterion's examples, mm-formatted the way the deck
        # and the backend both render them: one decimal, U+202F, "mm").
        H = "12.0\u202fmm"
        W = "60.0\u202fmm"
        D = "45.0\u202fmm"
        stated = "12.0\u202fmm"
        measured = "43.8\u202fmm"

        # The expected sentences: the exact strings the W263 design-
        # contract test in web/src/__tests__/design-contract.test.ts
        # pins for copy.deterministicAnswer — one per provenance class,
        # per axis where the class carries an axis slot.
        expected = [
            # stated+measured (one template, three axis adjectives)
            DETERMINISTIC_AXIS_SENTENCES["stated+measured"].format(
                value=H, axis="tall"
            ),
            DETERMINISTIC_AXIS_SENTENCES["stated+measured"].format(
                value=W, axis="wide"
            ),
            DETERMINISTIC_AXIS_SENTENCES["stated+measured"].format(
                value=D, axis="deep"
            ),
            # measured
            DETERMINISTIC_AXIS_SENTENCES["measured"].format(value=H, axis="tall"),
            DETERMINISTIC_AXIS_SENTENCES["measured"].format(value=D, axis="deep"),
            # stated
            DETERMINISTIC_AXIS_SENTENCES["stated"].format(value=H, axis="tall"),
            DETERMINISTIC_AXIS_SENTENCES["stated"].format(value=W, axis="wide"),
            # disagrees (axis-independent)
            DETERMINISTIC_AXIS_SENTENCES["disagrees"].format(
                stated=stated, measured=measured
            ),
            # not established (one template, three axis nouns)
            DETERMINISTIC_AXIS_SENTENCES["not_established"].format(axis_noun="height"),
            DETERMINISTIC_AXIS_SENTENCES["not_established"].format(axis_noun="width"),
            DETERMINISTIC_AXIS_SENTENCES["not_established"].format(axis_noun="depth"),
            # dimension list (W x D x H order)
            DETERMINISTIC_DIMENSION_LIST_FORMAT.format(W=W, D=D, H=H),
        ]
        expected_exact = [
            "It's 12.0\u202fmm tall \u2014 you said that, and I measured it.",
            "It's 60.0\u202fmm wide \u2014 you said that, and I measured it.",
            "It's 45.0\u202fmm deep \u2014 you said that, and I measured it.",
            "It measures 12.0\u202fmm tall.",
            "It measures 45.0\u202fmm deep.",
            "You said 12.0\u202fmm tall. Nothing has measured it yet.",
            "You said 60.0\u202fmm wide. Nothing has measured it yet.",
            "You said 12.0\u202fmm; what came out measures 43.8\u202fmm.",
            "The height isn't established yet.",
            "The width isn't established yet.",
            "The depth isn't established yet.",
            "It measures 60.0\u202fmm \u00d7 45.0\u202fmm \u00d7 12.0\u202fmm.",
        ]
        # The backend's formatted sentences are exactly the W263 design-
        # contract test's pinned strings — no regex, no placeholder
        # conversion, both directions pinned by the literals above.
        assert expected == expected_exact

        # The per-axis adjective/noun lookups carry the axis words the
        # W263 design-contract test pins (tall/wide/deep, height/width/
        # depth) — a deck edit to a different axis word breaks this.
        assert DETERMINISTIC_AXIS_ADJECTIVES == {
            "W": "wide", "D": "deep", "H": "tall"
        }
        assert DETERMINISTIC_AXIS_NOUNS == {
            "W": "width", "D": "depth", "H": "height"
        }




class TestDeterministicAxisStageRouteLevel:
    """Issue #263 route-level tests: the deterministic stage answers
    axis-size questions with NO LLM call, NO design run, NO version.
    Uses the real app fixture (``app_with_versions``)."""

    def test_axis_question_deterministic_no_edge_no_version(
        self, app_with_versions
    ) -> None:
        """"How tall is it now?" on a version with bbox z=12.0 and no
        stated H → answered deterministically, the answer-edge stub is
        NOT called, no version is created, ONE done frame."""

        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        def _loop(app, **kwargs):
            raise AssertionError("the design loop must NOT be called for a deterministic answer")

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            # A version with bbox z=12.0 and no stated_dims.
            await app_with_versions.state.versions.create_version(
                pid,
                {"H": 12.0, "W": 20.0, "D": 20.0},
                stated_dims=None,
                bbox=(20.0, 20.0, 12.0),
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _edge
            r, frames = await _drive_chat_with_answer(
                app_with_versions,
                client,
                pid,
                {"message": "How tall is it now?", "chat_history": []},
            )
            versions = app_with_versions.state.versions.list_versions(pid)
            return r, frames, len(versions), edge_called[0]

        r, frames, version_count, was_edge_called = run_async(app_with_versions, _call)
        assert r.status_code == 202, r.text
        assert not was_edge_called, "the answer edge must NOT be called for a deterministic answer"
        assert version_count == 1, f"expected 1 version, got {version_count}"
        # Exactly ONE frame: the terminal done frame with kind "answer".
        assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
        event, data = frames[0]
        assert event == "done"
        assert data.get("kind") == ANSWER_DONE_KIND
        assert "12.0" in data["message"]

    def test_dimension_list_deterministic_no_edge(self, app_with_versions) -> None:
        """"How big is it?" → the dimension list, no LLM call, no version."""

        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "It is 20 x 20 x 12 mm."}'

        def _loop(app, **kwargs):
            raise AssertionError("the design loop must NOT be called for a deterministic answer")

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            await app_with_versions.state.versions.create_version(
                pid,
                {"H": 12.0, "W": 20.0, "D": 20.0},
                stated_dims=None,
                bbox=(20.0, 25.0, 12.0),
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _edge
            r, frames = await _drive_chat_with_answer(
                app_with_versions,
                client,
                pid,
                {"message": "How big is it?", "chat_history": []},
            )
            versions = app_with_versions.state.versions.list_versions(pid)
            return r, frames, len(versions), edge_called[0]

        r, frames, version_count, was_edge_called = run_async(app_with_versions, _call)
        assert r.status_code == 202, r.text
        assert not was_edge_called, "the answer edge must NOT be called for a deterministic answer"
        assert version_count == 1, f"expected 1 version, got {version_count}"
        assert len(frames) == 1
        event, data = frames[0]
        assert event == "done"
        assert data.get("kind") == ANSWER_DONE_KIND
        assert "20.0" in data["message"]
        assert "25.0" in data["message"]
        assert "12.0" in data["message"]


# ---------------------------------------------------------------------------
# The /chat route — the answer path (real app, stub answer edge)
# ---------------------------------------------------------------------------


async def _drive_chat_with_answer(
    app, client, pid, body, *, answer_reply: str | None = None, answer_edge=None
):
    """POST ``body`` to /chat and drive the registered event source to its
    terminal frame. ``answer_reply`` (when not None) wires a stub
    ``answer_question`` on ``app.state`` that returns the fixed reply
    string (the production edge's contract: ``async (question, entries)
    -> reply_text``). When ``answer_edge`` is supplied it overrides
    (for the malformed-reply and exception cases)."""

    async def _default_edge(question: str, entries: list) -> str:
        return answer_reply or ""

    app.state.answer_question = answer_edge or _default_edge

    r = await client.post(f"/api/projects/{pid}/chat", json=body)
    source = app.state.event_sources.get(pid)
    frames = []
    assert source is not None, "event source not registered before 202 response"
    async for event, data in source:
        frames.append((event, data))
        if event in ("done", "error"):
            break
    return r, frames


def test_answered_question_emits_done_frame_no_version(app_with_versions) -> None:
    """DECISIVE (issue #249): a question the stage-2 LLM answers against
    a project whose latest version has H stated 12 → answered via ONE
    terminal done frame (``kind: "answer"``), NO version created, NO
    version-created frame, NO token frame. The design loop is never
    called.

    Uses a non-axis-size question ("What is the material?") so the
    #263 deterministic stage does not intercept it — this test exercises
    the stage-2 LLM answer path specifically."""

    captured_loop_called = False

    def _loop(app, **kwargs):
        nonlocal captured_loop_called
        captured_loop_called = True
        raise AssertionError("the design loop must NOT be called on the answer path")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # A version with H stated 12 (the ticket's example).
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0, "W": 20.0, "D": 20.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        reply = '{"answerable": true, "answer": "It is 12 mm tall — you said that."}'
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What is the material?", "chat_history": []},
            answer_reply=reply,
        )
        # The version count is still 1 (no new version created).
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not captured_loop_called, "the design loop was called on the answer path"
    assert version_count == 1, f"expected 1 version, got {version_count}"
    # Exactly ONE frame: the terminal done frame with kind "answer".
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == "It is 12 mm tall — you said that."


def test_design_loop_message_goes_to_loop_not_answer(app_with_versions) -> None:
    """A non-question message ("make it taller") goes to the design loop
    exactly as today — the answer path is not invoked, the loop IS
    invoked."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "make it taller", "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called for a non-question message"
    # The frames come from the design loop (not the answer path): the
    # terminal frame is a done WITHOUT kind "answer".
    terminal = frames[-1]
    assert terminal[0] == "done"
    assert terminal[1].get("kind") != ANSWER_DONE_KIND


def test_imperative_question_goes_to_loop_not_answer(app_with_versions) -> None:
    """The ticket's edge case: "Is it tall enough for a 12 mm shelf?
    Make it 15." — an interrogative with an imperative → design loop,
    NOT the answer path."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "Is it tall enough for a 12 mm shelf? Make it 15.",
             "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called for an imperative question"
    terminal = frames[-1]
    assert terminal[0] == "done"
    assert terminal[1].get("kind") != ANSWER_DONE_KIND


def test_unanswerable_kind_gets_no_run_reply_not_loop(app_with_versions) -> None:
    """#260: kind "unanswerable" (a genuine question the block does not
    establish, "What colour is it?") → the fixed no-run copy, NO design
    run, NO version. Today's behaviour (inverted by this ticket) was the
    design loop — which produced a wrong or failing new version."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What colour is it?", "chat_history": []},
            answer_reply='{"kind": "unanswerable", "answer": ""}',
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, "the design loop was called for an unanswerable question"
    assert version_count == 1, f"expected 1 version, got {version_count}"
    # ONE terminal done frame, kind "answer" (the #249 plain-message
    # path), carrying the fixed no-run copy — never the design loop's
    # frame.
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == NOT_ESTABLISHED


def test_unanswerable_valid_missing_gets_no_run_reply_not_loop(
    app_with_versions,
) -> None:
    """issue #278: kind "unanswerable" with a valid ``missing`` → the
    done frame carries exactly "I don't know the shelf's height. Tell me
    and I'll check — nothing was changed." (built from the
    parameterised copy) — NO design run, NO version, ONE WARNING with
    outcome=unanswerable that does NOT carry the missing text."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "Is it taller than the shelf?", "chat_history": []},
            answer_reply=(
                '{"kind": "unanswerable", "answer": "", '
                '"missing": "the shelf\'s height"}'
            ),
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, "the design loop was called for an unanswerable question"
    assert version_count == 1, f"expected 1 version, got {version_count}"
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == (
        "I don't know the shelf's height. Tell me and I'll check — "
        "nothing was changed."
    )


def test_invented_number_guard_failure_gets_no_run_reply_not_loop(
    app_with_versions,
) -> None:
    """#260: an LLM answer containing a number not in the block → guard
    fails → the fixed no-run copy. No design run, no version — the
    design loop is never the fallback for a failed answer.

    Uses a non-axis-size question so the #263 deterministic stage does
    not intercept it."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What is the material?", "chat_history": []},
            answer_reply='{"kind": "answer", "answer": "It is 15 mm tall."}',
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, (
        "the design loop was called for an invented-number answer "
        "(the design loop is never the fallback for a failed answer)"
    )
    assert version_count == 1, f"expected 1 version, got {version_count}"
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == COULD_NOT_ANSWER


def test_screw_question_guard_licenses_question_number(app_with_versions) -> None:
    """issue #278 REPRO: "Is it deep enough for a 30 mm screw?" with a
    design-state block carrying D 43.9 (measured): the stage-2 answer
    that quotes the question's "30" PASSES the guard → the done frame
    carries the answer text (kind "answer"), not COULD_NOT_ANSWER.
    No design run, no new version.

    The question contains a digit, so the #263 deterministic stage
    already abstains (the target-number guard) — this exercises the
    stage-2 guard at the route level."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"D": 43.9},
            bbox=(20.0, 43.9, 12.0),
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "Is it deep enough for a 30 mm screw?",
             "chat_history": []},
            answer_reply=(
                '{"kind": "answer", "answer": "Yes — it\'s 43.9 mm deep, '
                'more than the 30 mm screw needs."}'
            ),
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, "the design loop must not run on the answer path"
    assert version_count == 1, f"expected 1 version, got {version_count}"
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == (
        "Yes — it's 43.9 mm deep, more than the 30 mm screw needs."
    )


def test_screw_question_answer_citing_25_still_guard_failure(
    app_with_versions,
) -> None:
    """issue #278 negative control: the same screw question with an
    answer citing 25 (in neither the question nor the block) still
    fails the guard → COULD_NOT_ANSWER, no design run."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"D": 43.9},
            bbox=(20.0, 43.9, 12.0),
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "Is it deep enough for a 30 mm screw?",
             "chat_history": []},
            answer_reply=(
                '{"kind": "answer", "answer": "Yes — it\'s 43.9 mm deep, '
                'more than the 25 mm screw needs."}'
            ),
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, (
        "the design loop was called for a guard-failing answer (the design "
        "loop is never the fallback for a failed answer)"
    )
    assert version_count == 1
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == COULD_NOT_ANSWER


def test_no_versions_goes_to_loop(app_with_versions) -> None:
    """A fresh project (no versions) receiving a question →
    stage 1 is skipped (no design state) → design loop, unchanged."""

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # No version created — a fresh project.
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What is the material?", "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called for a fresh project"


def test_stage2_llm_timeout_gets_no_run_reply(app_with_versions, monkeypatch) -> None:
    """#260 REPRO-VERIFICATION: a stage-2 LLM call that times out
    (simulated via a hanging stub — the same 0.1 s monkeypatch contract
    as before) → the fixed "couldn't answer" no-run done frame, the
    design loop is NEVER invoked, the version count is unchanged. The
    design loop is never the fallback for a failed answer.

    The timeout is monkeypatched to 0.1 s so the test runs in <1 s of
    wall clock (the same contract as the 10 s production bound, at a
    shorter value — the operator's latency decision is pinned by the
    ``ask_answer_call`` timeout param, not by the literal value of
    ``ANSWER_CALL_TIMEOUT_SECONDS``)."""
    import d33d.question_answer as _qa_mod
    monkeypatch.setattr(_qa_mod, "ANSWER_CALL_TIMEOUT_SECONDS", 0.1)

    loop_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _hanging_edge(question: str, entries: list) -> str:
        await asyncio.sleep(0.5)  # well past the 0.1 s bound
        return ""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        app_with_versions.state.answer_question = _hanging_edge
        r = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "What is the material?", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        frames = []
        assert source is not None
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not loop_called, (
        "the design loop was called after a stage-2 timeout (the design "
        "loop is never the fallback for a failed answer)"
    )
    assert version_count == 1, f"expected 1 version, got {version_count}"
    # The no-run done frame: ONE terminal frame, kind "answer", the
    # fixed couldn't-answer copy — no design-loop frame, no
    # version-created frame.
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == COULD_NOT_ANSWER


def test_answer_done_frame_passes_seam_d_schema(app_with_versions) -> None:
    """The answer path's done frame (``kind: "answer"``) passes the SEAM
    D schema (``validate_frames_stream``) — the additive ``kind`` field
    does not break the wire contract."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        reply = '{"answerable": true, "answer": "It is 12 mm tall."}'
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What is the material?", "chat_history": []},
            answer_reply=reply,
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    # The SEAM D schema validates the frame stream (including the additive
    # kind field on the done frame).
    validated = validate_frames_stream(frames)
    assert len(validated) == 1
    assert validated[0][0] == "done"
    assert validated[0][1]["kind"] == ANSWER_DONE_KIND


def test_stage2_call_receives_state_block(app_with_versions) -> None:
    """The stage-2 LLM call receives the design-state block (with
    provenance and labels) as its prompt context — the captured entries
    match the latest version's state block."""

    captured_entries: list = []
    edge_called = [False]  # list so the closure can mutate it

    async def _capturing_edge(question: str, entries: list) -> str:
        edge_called[0] = True
        captured_entries.extend(entries)
        return '{"answerable": false, "answer": ""}'

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0, "W": 20.0},
            stated_dims={"H": 12.0},
        )
        # A stub design loop (a defensive fallback — this stub edge
        # answers, so the loop is never invoked for this question).
        def _loop(app, **kwargs):
            class _R:
                status = "pass"
                failure_reason = None
                best = None
            return _R()
        app_with_versions.state.run_design_loop = _loop
        # The capturing edge is set on app.state BEFORE the /chat call —
        # the route reads app.state.answer_question at request time.
        app_with_versions.state.answer_question = _capturing_edge
        r2 = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "What is the material?", "chat_history": []},
        )
        source = app_with_versions.state.event_sources.get(pid)
        frames = []
        if source:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return r2, list(captured_entries), edge_called[0]

    r, captured, was_called = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert was_called, "the stage-2 edge was never called"
    assert len(captured) > 0, "the stage-2 call did not receive the state block"
    # The entries include the H=12 axis row (stated).
    h_values = [e.get("value") for e in captured if e.get("name") == "H"]
    assert 12.0 in h_values, f"expected H=12 in the state block, got {h_values}"


# ---------------------------------------------------------------------------
# Review batch (issue #249): spelled-out number guard, concurrency,
# probe caching, request_logs observability, prompt-pair agreement
# ---------------------------------------------------------------------------


class TestWrittenNumberGuard:
    """``guard_answer_numbers`` — spelled-out numbers (zero–twenty, tens
    up to ninety, "a dozen") are held to the same presence rule as digit
    tokens (issue #249 review, item 7)."""

    def test_twelve_in_block_passes(self) -> None:
        # "twelve millimetres tall" with H=12 → allowed (the spelled-out
        # form of a number that IS in the block).
        entries = _entries(("H", 12.0))
        assert guard_answer_numbers("It is twelve millimetres tall.", entries)

    def test_fifteen_not_in_block_rejected(self) -> None:
        # "fifteen" with H=12 → rejected (the spelled-out form of a
        # number NOT in the block — a digit-only guard would have let
        # this through with no digit present).
        entries = _entries(("H", 12.0))
        assert not guard_answer_numbers("It is fifteen millimetres tall.", entries)

    def test_dozen_in_block_passes(self) -> None:
        entries = _entries(("H", 12.0))
        assert guard_answer_numbers("It is a dozen millimetres tall.", entries)

    def test_dozen_not_in_block_rejected(self) -> None:
        entries = _entries(("H", 15.0))
        assert not guard_answer_numbers("It is a dozen millimetres.", entries)

    def test_ten_in_block_passes(self) -> None:
        entries = _entries(("H", 10.0))
        assert guard_answer_numbers("It is ten millimetres tall.", entries)

    def test_tens_up_to_ninety(self) -> None:
        entries = _entries(("H", 20.0), ("W", 90.0))
        assert guard_answer_numbers("twenty by ninety millimetres", entries)
        assert not guard_answer_numbers("twenty by eighty millimetres", entries)

    def test_case_insensitive(self) -> None:
        entries = _entries(("H", 12.0))
        assert guard_answer_numbers("It is TWELVE millimetres tall.", entries)


def test_kind_request_goes_to_loop_exactly_as_non_question(app_with_versions) -> None:
    """#260: a stage-2 kind "request" reply runs the design loop exactly
    as a non-question message does (the model says the message asks for
    a change). The frame is the design loop's own (NO kind "answer"),
    and the stage-2 edge IS called (the pre-route classified it)."""

    loop_called = False
    edge_called = False

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called = True

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    async def _request_edge(question: str, entries: list) -> str:
        nonlocal edge_called
        edge_called = True
        return '{"kind": "request", "answer": ""}'

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.run_design_loop = _loop
        app_with_versions.state.answer_question = _request_edge
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "What is the material?", "chat_history": []},
            answer_edge=_request_edge,
        )
        versions = app_with_versions.state.versions.list_versions(pid)
        return r, frames, len(versions)

    r, frames, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert edge_called, "the stage-2 edge was NOT called for a request-kind question"
    assert loop_called, "the design loop was NOT called for a kind 'request' reply"
    # The design loop ran (the stub loop does not create a version, so
    # the count stays 1 — same as the non-question tests).
    assert version_count == 1, f"expected 1 version, got {version_count}"
    # The terminal frame is the design loop's own — no kind "answer".
    terminal = frames[-1]
    assert terminal[0] == "done"
    assert terminal[1].get("kind") != ANSWER_DONE_KIND


def test_stage2_exception_gets_no_run_reply_releases_flag(app_with_versions) -> None:
    """#260: a stage-2 edge that raises → the fixed no-run reply (not the
    design loop), the in-flight flag is released (the follow-up POST is
    not 409'd), and no version is created."""

    loop_called = 0

    def _loop(app, **kwargs):
        nonlocal loop_called
        loop_called += 1

        class _R:
            status = "pass"
            failure_reason = None
            best = None

        return _R()

    def _raise_edge(question: str, entries: list) -> str:
        raise RuntimeError("simulated stage-2 failure")

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await app_with_versions.state.versions.create_version(
            pid,
            {"H": 12.0},
            stated_dims={"H": 12.0},
        )
        app_with_versions.state.answer_question = _raise_edge
        # The failing POST: the edge raises → the fixed no-run reply.
        r_fail = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "How tall is it now?", "chat_history": []},
        )
        # The flag is released by the STREAM's finally — drain via the
        # real SSE endpoint (the sole driver of event sources, per
        # streaming.py's contract).
        async with client.stream("GET", f"/api/stream/{pid}") as resp:
            async for _chunk in resp.aiter_text():
                pass
        # The in-flight flag was released by the stream's finally: a
        # follow-up POST must NOT be 409'd. The follow-up is a
        # NON-QUESTION ("make it taller") → stage-1 rejects → the design
        # loop, whose stub (pass result) never reaches the real catalogue.
        app_with_versions.state.answer_question = None
        app_with_versions.state.run_design_loop = _loop
        r_ok = await client.post(
            f"/api/projects/{pid}/chat",
            json={"message": "make it taller", "chat_history": []},
        )
        source2 = app_with_versions.state.event_sources.get(pid)
        frames2 = []
        if source2 is not None:
            async for event, data in source2:
                frames2.append((event, data))
                if event in ("done", "error"):
                    break
        # The flag is released again by the stream's finally.
        async with client.stream("GET", f"/api/stream/{pid}") as resp:
            async for _chunk in resp.aiter_text():
                pass
        versions = app_with_versions.state.versions.list_versions(pid)
        return r_fail, r_ok, frames2, len(versions)

    r_fail, r_ok, frames2, version_count = run_async(app_with_versions, _call)
    assert r_fail.status_code == 202, r_fail.text
    # The failing question did NOT route to the design loop: the loop
    # ran exactly once, for the follow-up (non-question) POST — the
    # stage-2 exception got the fixed no-run reply, not a design run.
    assert loop_called == 1, (
        f"the design loop was called {loop_called} time(s); expected exactly "
        f"one call — for the follow-up non-question POST, not for the "
        f"failed-answer question (the design loop is never the fallback "
        f"for a failed answer)"
    )
    # The follow-up POST is not 409'd: the in-flight flag was released
    # on the no-run path (the SSE stream's finally cleared it).
    assert r_ok.status_code == 202, (
        f"POST after a stage-2 exception expected 202, got {r_ok.status_code} "
        f"(the in-flight flag was not released): {r_ok.text}"
    )
    # The follow-up went to the design loop (the stub did not create a
    # version — the count is unchanged either way). The loop's frame
    # stream is terminal with a done frame that carries NO kind "answer"
    # (the no-run reply's done frame is the one with kind "answer").
    assert frames2 and frames2[-1][0] == "done"
    assert frames2[-1][1].get("kind") != ANSWER_DONE_KIND
    assert version_count == 1, f"expected 1 version, got {version_count}"


class TestStage2OutcomeWarningLogs:
    """#260: every stage-2 outcome — the three kinds AND the four failure
    classes — emits exactly ONE WARNING record naming the outcome, with
    elapsed ms and message length, and NEVER the message or answer text
    (no PII in logs). The stage-1 short-circuits stay at INFO (no
    WARNING at all)."""

    def _latest(self, params: dict) -> dict:
        return {"params": params, "stated_dims": None, "bbox": None, "param_meta": None}

    def _outcome_warning(self, caplog) -> list[logging.LogRecord]:
        return [r for r in caplog.records if r.levelno == logging.WARNING]

    def test_answer_emits_one_warning_no_text(self, caplog) -> None:
        # Every stage-2 outcome — the three kinds AND the four failure
        # classes (timeout/exception/malformed/guard) — emits exactly one
        # WARNING naming the outcome plus elapsed ms and message length
        # (issue #260: "log every stage-2 outcome at WARNING"). The
        # successful answer is no exception: the warning carries only
        # lengths — never the message or the answer text (no PII).
        # Uses a non-axis question so the #263 deterministic stage does
        # not intercept it.
        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": "It is 12 mm tall."}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=answer" in warnings[0].getMessage()
        assert "elapsed_ms=" in warnings[0].getMessage()
        assert "len(message)=" in warnings[0].getMessage()
        # Never the message or answer text (no PII in logs).
        for record in warnings:
            assert "What is the material?" not in record.getMessage()
            assert "It is 12 mm tall." not in record.getMessage()
        # The answered path also keeps its INFO record (lengths only).
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1, f"expected 1 INFO, got {len(infos)}"
        assert "answering from the design-state block" in infos[0].getMessage()
        for record in infos:
            assert "What is the material?" not in record.getMessage()
            assert "It is 12 mm tall." not in record.getMessage()

    def test_unanswerable_emits_one_warning(self, caplog) -> None:
        async def _edge(q, e):
            return '{"kind": "unanswerable", "answer": ""}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What colour is it?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=unanswerable" in warnings[0].getMessage()

    def test_unanswerable_valid_missing_emits_one_warning_no_missing_text(
        self, caplog
    ) -> None:
        # issue #278: the unanswerable-with-missing path keeps the ONE
        # WARNING contract (outcome=unanswerable, elapsed ms, message
        # length) and NEVER logs the missing text (no PII in logs).
        async def _edge(q, e):
            return (
                '{"kind": "unanswerable", "answer": "", '
                '"missing": "the shelf\'s height"}'
            )

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "Is it taller than the shelf?",
                    self._latest({"H": 12.0}),
                    _edge,
                )
            )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": UNANSWERABLE_MISSING_TEMPLATE.format(
                missing="the shelf's height"
            ),
        }
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=unanswerable" in warnings[0].getMessage()
        assert "elapsed_ms=" in warnings[0].getMessage()
        assert "len(message)=" in warnings[0].getMessage()
        for record in warnings:
            assert "the shelf's height" not in record.getMessage()
            assert "Is it taller than the shelf?" not in record.getMessage()

    def test_request_emits_one_warning(self, caplog) -> None:
        async def _edge(q, e):
            return '{"kind": "request", "answer": ""}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result is None  # request → the design loop
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=request" in warnings[0].getMessage()

    def test_timeout_emits_one_warning(self, caplog) -> None:
        async def _edge(q, e):
            await asyncio.sleep(0.5)  # well past the 0.05 s bound
            return ""

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge,
                    timeout=0.05,
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=timeout" in warnings[0].getMessage()

    def test_exception_emits_one_warning(self, caplog) -> None:
        async def _edge(q, e):
            raise RuntimeError("boom")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=exception" in warnings[0].getMessage()

    def test_malformed_emits_one_warning_no_reply_text(self, caplog) -> None:
        # The old malformed path logged raw=%r (the reply text) at INFO —
        # the exact PII pattern this removes: the WARNING names the class
        # and never the text.
        async def _edge(q, e):
            return "not a json reply at all"

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=malformed" in warnings[0].getMessage()
        for record in warnings:
            assert "not a json reply at all" not in record.getMessage()

    def test_guard_failure_emits_one_warning(self, caplog) -> None:
        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 15 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=guard" in warnings[0].getMessage()
        for record in warnings:
            assert "It is 15 mm tall." not in record.getMessage()

    def test_stage1_short_circuit_emits_no_warning(self, caplog) -> None:
        # The stage-1 short-circuits (no versions / not a candidate /
        # no answer edge) stay at INFO: zero WARNING records.
        # Uses a non-axis question so the #263 deterministic stage does
        # not intercept it (an axis question with no version would be
        # "not established" via the deterministic stage, not a stage-1
        # short-circuit).
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            assert asyncio.run(route_chat_message("What is the material?", None)) is None
            assert (
                asyncio.run(
                    route_chat_message(
                        "make it taller", self._latest({"H": 12.0})
                    )
                )
                is None
            )
            assert (
                asyncio.run(
                    route_chat_message(
                        "What is the material?", self._latest({"H": 12.0})
                    )
                )
                is None
            )
        assert self._outcome_warning(caplog) == []

    def test_cancellation_propagates_not_no_run(self, caplog) -> None:
        """Cancellation of the stage-2 call propagates as
        ``CancelledError`` — it is NEVER swallowed into a no-run
        ``COULD_NOT_ANSWER`` reply (a no-run reply would hide the
        cancellation from the caller's ``except CancelledError``
        cleanup). Uses a non-axis question so the #263 deterministic
        stage does not intercept it."""

        async def _canceling_edge(q, e):
            raise asyncio.CancelledError()

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            coro = route_chat_message(
                "What is the material?", self._latest({"H": 12.0}), _canceling_edge
            )
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(coro)
        # Cancellation is NOT a stage-2 outcome: no outcome= record at all.
        assert self._outcome_warning(caplog) == []

    def test_httpx_timeout_emits_timeout_warning(self, caplog) -> None:
        """An ``httpx.TimeoutException`` (the per-request client bound in
        ``_http_request_factory``) is a ``timeout`` outcome, not an
        ``exception`` — the classification is by exception class, never
        by elapsed time. Uses a non-axis question so the #263
        deterministic stage does not intercept it."""
        import httpx

        async def _edge(q, e):
            raise httpx.ReadTimeout("read timed out")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=timeout" in warnings[0].getMessage()

    def test_httpx_non_timeout_emits_exception_warning(self, caplog) -> None:
        """An ``httpx`` error that is NOT a timeout (e.g. a connection
        reset) is an ``exception``, not a ``timeout`` — the
        httpx.TimeoutException check is specific to the timeout class.
        Uses a non-axis question so the #263 deterministic stage does
        not intercept it."""
        import httpx

        async def _edge(q, e):
            raise httpx.ConnectError("connection reset")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=exception" in warnings[0].getMessage()


class TestCopyDeckParity:
    """#260: the backend's two no-run reply strings are pinned against
    ``web/src/copy.ts``'s ``answerRoute`` deck, the same way the #250
    ``confirmOffer`` strings are — a deck edit without the backend (or
    vice versa) is a drift this catches."""

    def test_backend_no_run_strings_match_copy_ts_deck(self) -> None:
        from pathlib import Path

        copy_ts = (
            Path(__file__).resolve().parents[2]
            / "web" / "src" / "copy.ts"
        ).read_text()

        # Exact membership check (issue #260 review): each backend string,
        # quoted exactly as it appears in the TS source, must be present in
        # copy.ts — no line-by-line regex extraction (which is brittle to
        # deck reformatting and can pick up the wrong string). Both
        # directions are pinned: a backend rewrite or a deck rewrite breaks
        # the check.
        for backend_string in (COULD_NOT_ANSWER, NOT_ESTABLISHED):
            assert f'"{backend_string}"' in copy_ts, (
                f"backend string {backend_string!r} not found in copy.ts "
                f"(the copy.ts deck must carry it verbatim)"
            )
        # The two strings are distinct (a failure and an unanswerable
        # question are different honest statements).
        assert COULD_NOT_ANSWER != NOT_ESTABLISHED

    def test_unanswerable_missing_template_renders_acceptance_text(self) -> None:
        # issue #278: the parameterised template renders the exact
        # acceptance text for the shelf's height, and the template's
        # {missing} slot is the only interpolation point.
        rendered = UNANSWERABLE_MISSING_TEMPLATE.format(
            missing="the shelf's height"
        )
        assert rendered == (
            "I don't know the shelf's height. Tell me and I'll check — "
            "nothing was changed."
        )
        # The template closes with the same "nothing was changed" as the
        # two fixed no-run replies (the done frame carries the text in
        # the existing ``answer`` field — no new wire field).
        assert rendered.endswith("nothing was changed.")


class TestConcurrentInflightClaim:
    """The /chat route claims the in-flight flag BEFORE the pre-route
    (issue #249 review, item 1): two concurrent POSTs where the first is
    a slow stage-2 question — the second gets 409; a pre-route exception
    leaves the project un-stuck."""

    def test_second_concurrent_post_gets_409(self, app_with_versions) -> None:
        """Two concurrent POSTs; the first is a slow stage-2 question
        (the edge sleeps 1.5 s). The claim is set before the pre-route,
        so the second POST — arriving while the first is still in the
        pre-route — gets 409, not a duplicate design loop."""

        async def _slow_edge(question: str, entries: list) -> str:
            await asyncio.sleep(1.5)
            return '{"answerable": false, "answer": ""}'

        def _loop(app, **kwargs):
            class _R:
                status = "pass"
                failure_reason = None
                best = None

            return _R()

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            await app_with_versions.state.versions.create_version(
                pid, {"H": 12.0}, stated_dims={"H": 12.0}
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _slow_edge
            # Two concurrent POSTs, a gap large enough that the second
            # lands while the first is still in the pre-route (1.5 s
            # edge sleep; 0.3 s gap).
            first = client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "What is the material?", "chat_history": []},
            )
            await asyncio.sleep(0.3)
            second = client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "What is the material?", "chat_history": []},
            )
            r1, r2 = await asyncio.gather(first, second)
            # Drain the stream via the real SSE endpoint (the sole
            # driver of the generator — its ``finally`` clears the
            # inflight flag when the terminal frame is reached).
            async with client.stream("GET", f"/api/stream/{pid}") as resp:
                async for _chunk in resp.aiter_text():
                    pass
            return r1, r2

        r1, r2 = run_async(app_with_versions, _call)
        assert r1.status_code == 202, r1.text
        assert r2.status_code == 409, (
            f"second concurrent POST expected 409, got {r2.status_code}: {r2.text}"
        )

    def test_preroute_exception_leaves_project_unstuck(self, app_with_versions) -> None:
        """A pre-route failure (the stage-2 edge raises) → the in-flight
        claim is released (try/except in the route) → the next POST is
        NOT 409. The exception is caught by ``ask_answer_call`` (ANY
        failure degrades to the design loop — it never propagates out of
        the pre-route to the route handler), so the observable failure
        seam is the raising edge itself; the test's next-POST 202
        assertion pins the flag release."""

        def _loop(app, **kwargs):
            class _R:
                status = "pass"
                failure_reason = None
                best = None

            return _R()

        def _raise_edge(question: str, entries: list):
            async def _edge(q, e):
                raise RuntimeError("simulated pre-route failure")

            return _edge

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            await app_with_versions.state.versions.create_version(
                pid, {"H": 12.0}, stated_dims={"H": 12.0}
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _raise_edge("", [])
            # The failing POST: the pre-route's edge raises → the claim
            # is released → the design loop runs (the stub returns a
            # pass result). 202 either way — the flag is what matters.
            # Uses a non-axis question so the #263 deterministic stage
            # does not intercept it.
            r_fail = await client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "What is the material?", "chat_history": []},
            )
            # Drain via the real SSE endpoint (the sole driver of the
            # generator — its ``finally`` clears the inflight flag).
            async with client.stream("GET", f"/api/stream/{pid}") as resp:
                async for _chunk in resp.aiter_text():
                    pass
            # A healthy edge now: the next POST must NOT be 409 — the
            # claim was released when the pre-route failed.
            async def _ok_edge(question: str, entries: list) -> str:
                return '{"answerable": false, "answer": ""}'

            app_with_versions.state.answer_question = _ok_edge
            r_ok = await client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "make it taller", "chat_history": []},
            )
            async with client.stream("GET", f"/api/stream/{pid}") as resp:
                async for _chunk in resp.aiter_text():
                    pass
            return r_fail, r_ok

        r_fail, r_ok = run_async(app_with_versions, _call)
        assert r_fail.status_code == 202, r_fail.text
        assert r_ok.status_code == 202, (
            f"POST after a pre-route failure expected 202, got {r_ok.status_code} "
            f"(the in-flight flag was not released): {r_ok.text}"
        )


class TestCapabilityProbeCached:
    """The stage-2 capability probe runs ONCE per (base_url, model), not
    per question (issue #249 review, item 2)."""

    def test_two_questions_one_probe(self, app_with_versions, monkeypatch) -> None:
        """The stage-2 capability probe runs ONCE per (base_url, model),
        not per question. Call the production edge directly (twice) and
        assert the probe was invoked exactly once."""
        import types

        from d33d import design_llm as _design_llm_mod
        from d33d.config import probes as _probes_mod
        from d33d.config.probes import CapabilityResult as _Cap
        from d33d.app import _build_question_answer_call

        probe_count = {"n": 0}

        async def _fake_probe(base_url, model_id, api_key, request_factory):
            probe_count["n"] += 1
            return _Cap(
                tools=False, json_schema=False, vision=False, max_images=0, validated=True
            )

        class _CountingLLM:
            def __init__(self) -> None:
                self.calls = 0

            async def __call__(self, role, messages, system):
                self.calls += 1
                raise _design_llm_mod.SenderError("wire failure", status="error")

        from d33d.config import catalogue as _catalogue_mod
        from d33d.config import resolve as _resolve_mod

        _prov = types.SimpleNamespace(base="http://stub", key="stub", name="p")
        monkeypatch.setattr(
            _catalogue_mod,
            "load_catalogue",
            lambda p: types.SimpleNamespace(
                providers={"p": _prov},
                roles={"question": "q"},
            ),
        )
        monkeypatch.setattr(
            _resolve_mod,
            "resolve",
            lambda cat, role, unavailable=None: types.SimpleNamespace(
                entry=types.SimpleNamespace(model="stub", id="q"),
                provider=_prov,
            ),
        )
        monkeypatch.setattr(_probes_mod, "probe_capabilities", _fake_probe)
        llm = _CountingLLM()
        from d33d import design_loop as _dl_mod

        monkeypatch.setattr(_dl_mod, "make_llm_fn", lambda cat, f, c: llm)

        app_with_versions.state.question_capability_cache = _probes_mod.CapabilityCache()
        edge = _build_question_answer_call(
            app_with_versions.state.catalogue_path, app_with_versions
        )

        async def _call():
            for _ in range(2):
                try:
                    await edge("How tall is it now?", [{"name": "H", "value": 12.0}])
                except Exception:
                    pass
            return probe_count["n"], llm.calls

        probes, llm_calls = asyncio.run(_call())
        assert probes == 1, f"expected 1 probe for 2 questions, got {probes}"
        assert llm_calls == 2, f"expected 2 completions, got {llm_calls}"


class _LLMResultOk:
    """A minimal ``LLMResult``-shaped reply for the counting stub: the
    reply text is malformed (not JSON), so the pre-route degrades to the
    design loop (the loop is stubbed). Carries the fields the
    ``request_logs`` path reads (``prompt_hash``, ``status``,
    ``usage``)."""

    def __init__(self) -> None:
        self.content: str = "not a json reply"
        self.prompt_hash: str = "test-hash"
        self.status: str = "ok"
        self.usage: dict = {}


class TestQuestionRoleRequestLogs:
    """Question-role LLM calls go through the request_logs path
    (issue #249 review, item 4): ``Connection.log_request`` with
    role=``question`` and the ``send()`` ``prompt_hash``."""

    def test_question_call_writes_request_log_row(self, app_with_versions, monkeypatch) -> None:
        import types

        from d33d import app as _app_mod
        from d33d.config import catalogue as _catalogue_mod
        from d33d.config import probes as _probes_mod
        from d33d.config import resolve as _resolve_mod
        from d33d.config.probes import CapabilityResult as _Cap
        from d33d.design_llm import LLMResult
        from d33d.app import _build_question_answer_call

        _prov = types.SimpleNamespace(base="http://stub", key="stub", name="p")
        monkeypatch.setattr(
            _catalogue_mod,
            "load_catalogue",
            lambda p: types.SimpleNamespace(
                providers={"p": _prov},
                roles={"question": "q"},
            ),
        )
        monkeypatch.setattr(
            _resolve_mod,
            "resolve",
            lambda cat, role, unavailable=None: types.SimpleNamespace(
                entry=types.SimpleNamespace(model="stub", id="q"),
                provider=_prov,
            ),
        )

        async def _fake_probe(base_url, model_id, api_key, request_factory):
            return _Cap(
                tools=False, json_schema=False, vision=False, max_images=0, validated=True
            )

        monkeypatch.setattr(_probes_mod, "probe_capabilities", _fake_probe)

        def _fake_llm_fn(cat, f, c):
            async def llm_fn(role, messages, system):
                return LLMResult(
                    content='{"answerable": false, "answer": ""}',
                    tool_calls=(),
                    prompt_hash="pinned-hash-123",
                    tier="T0",
                    status="ok",
                    request_body={},
                    usage={"prompt_tokens": 10, "completion_tokens": 5},
                )

            return llm_fn

        from d33d import design_loop as _dl_mod

        monkeypatch.setattr(_dl_mod, "make_llm_fn", _fake_llm_fn)

        # Ensure a real DB connection (the edge's request_logs path
        # writes through ``app_state.state.conn``).
        if app_with_versions.state.conn is None:
            import d33d.db as _db
            import d33d.versions as _versions_mod

            conn = _db.connect(app_with_versions.state.db_path)
            _versions_mod.migrate(conn)
            app_with_versions.state.conn = conn

        edge = _build_question_answer_call(
            app_with_versions.state.catalogue_path, app_with_versions
        )

        async def _call():
            await edge("How tall is it now?", [{"name": "H", "value": 12.0}])
            rows = (
                app_with_versions.state.conn.raw
                .execute(
                    "SELECT role, prompt_hash, model_id, status FROM request_logs"
                )
                .fetchall()
            )
            return [dict(row) for row in rows]

        rows = asyncio.run(_call())
        question_rows = [r for r in rows if r.get("role") == "question"]
        assert len(question_rows) == 1, (
            f"expected exactly one question-role request_log row, got {rows}"
        )
        assert question_rows[0]["prompt_hash"] == "pinned-hash-123"
        assert question_rows[0]["model_id"] == "stub"
        assert question_rows[0]["status"] == "ok"


class TestPromptPairAgreement:
    """The production prompt (``build_answer_prompt``) and the evals
    prompt file (``evals/prompts/question_answer_v1.md``) state the same
    rules and reply format (issue #249 review, item 8) — a drift in
    either direction (a rule added to one but not the other, a reply
    format change) is a divergence the test catches."""

    def test_production_prompt_and_evals_md_state_same_rules_and_format(
        self, app_with_versions
    ) -> None:
        from pathlib import Path

        evals_md = (
            Path(__file__).resolve().parents[2]
            / "evals" / "prompts" / "question_answer_v1.md"
        ).read_text()
        entries = _entries(("H", 12.0), ("W", 20.0))
        prompt = build_answer_prompt("How tall is it?", entries)

        # The reply format: the same JSON object shape in both (the #260
        # three-way kind — the legacy {"answerable": true|false, …} shape
        # is retired from both prompts).
        assert '{"kind": "answer"|' in prompt
        assert '"unanswerable"|"request", "answer": "…"}' in prompt
        assert '{"kind": "answer"|' in evals_md
        assert '"unanswerable"|"request", "answer": "…"}' in evals_md
        # The legacy shape is gone from both.
        assert "\"answerable\": true|false" not in prompt
        assert "\"answerable\": true|false" not in evals_md

        # The value-integrity contract: both forbid inventing values —
        # every number must come from the block OR be a number the user's
        # own question stated (issue #278: question numbers are licensed;
        # all other numbers are forbidden). The production prompt's
        # invariant and the evals prompt's Rule 2 state this in
        # lockstep — a drift in either direction (the model withholding
        # question-quoted numbers, or inventing others) is a test failure.
        assert "the user's question" in prompt
        assert "a number the user's own question stated" in prompt
        assert "the user's question" in evals_md
        assert "may be quoted in the answer" in prompt
        assert "may be quoted in the answer" in evals_md
        # The provenance citations: the same provenance phrases.
        for phrase in (
            "you said that",
            "I measured",
            "I assumed",
            "not established",
            "you said X, I measured Y",
            "I set X, it measures Y",
        ):
            assert phrase in prompt, f"phrase {phrase!r} missing from production prompt"
            assert phrase in evals_md, f"phrase {phrase!r} missing from evals md"
        # The model-source disagreement wording (issue #264): the
        # evals prompt states BOTH disagreement wordings unconditionally
        # (its block is part of the file, not a rendered argument); the
        # production prompt states them on a block that carries a
        # model-source row — so check the phrase in the evals md and in
        # the production prompt built from a v25-shaped (model-source)
        # block.
        from d33d.design_state import state_block_for_version

        model_entries = state_block_for_version(
            {"spacer_width": 40.0, "spacer_depth": 40.0},
            {"x": 43.80, "y": 43.90, "z": 12.0},
            None,
            {
                "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
                "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
            },
        )
        model_prompt = build_answer_prompt("How wide is it?", model_entries)
        for phrase in ("I set X, it measures Y",):
            assert phrase in model_prompt, (
                f"phrase {phrase!r} missing from production prompt"
            )
            assert phrase in evals_md, f"phrase {phrase!r} missing from evals md"
        # The block mark the instruction keys on ("my value differs from
        # the measurement") is the DESIGN prompt's provenance suffix
        # (``_provenance_suffix`` in ``d33d.design_state``) — the question
        # router does not restate it in the instruction: the model reads
        # the mark from the block text itself (the state block rendered
        # into the prompt). The evals md references it verbatim inside
        # the instruction.
        assert "my value differs from the measurement" in model_prompt
        assert "my value differs from the measurement" in evals_md
        # The no-offer rule: both forbid offering to set/confirm.
        assert "do NOT offer to change, set, or confirm" in prompt
        assert "Do not offer to set, change" in evals_md

    def test_build_answer_prompt_distinguishes_model_source_disagreement(
        self, app_with_versions
    ) -> None:
        """Issue #264 ACCEPTANCE: the router's answer prompt names BOTH
        kinds of disagreement — unconditionally (the instruction is the
        same regardless of the block; the model picks the wording per
        row from the block's own marks): user-source "you said X, I
        measured Y" vs model-source "I set X, it measures Y" (the user
        never stated that value — never 'you said'). A block that
        carries a model-source disagrees row (``disagrees_source ==
        "model"`` — the v25 Shelf-spacer shape) renders the row's mark
        "(my value differs from the measurement)" so the model can tell
        the rows apart from user-source ones."""
        from d33d.question_answer import build_answer_prompt

        # v25-shaped: declared-axis params that contest the measurement
        # render disagrees_source "model".
        from d33d.design_state import state_block_for_version

        model_entries = state_block_for_version(
            {"spacer_width": 40.0, "spacer_depth": 40.0},
            {"x": 43.80, "y": 43.90, "z": 12.0},
            None,
            {
                "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
                "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
            },
        )
        assert any(
            e.get("provenance") == "disagrees"
            and e.get("disagrees_source") == "model"
            for e in model_entries
        ), model_entries
        model_prompt = build_answer_prompt("How wide is it?", model_entries)
        # The model-source instruction is present and pins the block's
        # own mark.
        assert "I set X, it measures Y" in model_prompt
        assert "my value differs from the measurement" in model_prompt
        # The block text carries the mark on the param rows.
        assert "Spacer width = 43.8 (my value differs from the measurement)" in model_prompt
        assert "Spacer depth = 43.9 (my value differs from the measurement)" in model_prompt
        # The user-source instruction remains (it applies to the axis
        # rows / any user-source disagreement in the same block).
        assert "you said X, I measured Y" in model_prompt

        # User-source only: a #137 W-named param row outside tolerance
        # (``disagrees_source`` ``"user"``) keeps today's user wording in
        # the block text (the plain "you stated this" mark) and the
        # instruction names the model-source wording too (it is
        # unconditional) — but the BLOCK itself carries no model-source
        # mark (the rows are user-source), so the model must answer
        # with the user wording for them.
        user_entries = state_block_for_version(
            {"W": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0}, None
        )
        assert all(
            e.get("disagrees_source") in (None, "user") for e in user_entries
        ), user_entries
        user_prompt = build_answer_prompt("How wide is it?", user_entries)
        assert "you said X, I measured Y" in user_prompt
        assert "I set X, it measures Y" in user_prompt
        # The block text carries the user-source mark, never the
        # model-source mark (no model-source row exists in this block).
        assert "you stated this; the measurement differs" in user_prompt
        assert "my value differs from the measurement" not in user_prompt
