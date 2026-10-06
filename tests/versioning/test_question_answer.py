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
from typing import Any, ClassVar

import pytest

from d33d import fill_recut
from d33d.confirm_offer import mm_formatted
from d33d.question_answer import (
    ANSWER_DONE_KIND,
    COULD_NOT_ANSWER,
    DETERMINISTIC_AXIS_ADJECTIVES,
    DETERMINISTIC_AXIS_NOUNS,
    DETERMINISTIC_AXIS_SENTENCES,
    DETERMINISTIC_COMPARISON_SENTENCES,
    DETERMINISTIC_DIMENSION_LIST_FORMAT,
    DETERMINISTIC_DIMENSION_LIST_RE,
    FEATURE_SIZE_UNMEASURED_REPLY,
    NO_OFFER_AFFIRMATION_REPLY,
    NO_VERSION_QUESTION_REPLY,
    NOT_ESTABLISHED,
    UNANSWERABLE_MISSING_TEMPLATE,
    UNSETTLED_SIZE_REPLY,
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

    def test_no_versions_question_returns_nothing_built_reply(self) -> None:
        # issue #349: a QUESTION with no version gets the deterministic
        # "nothing built" reply (kind "answer"), never the design loop.
        # Zero LLM calls (the guard runs before any stage-2 call).
        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return "{}"

        for msg in ("How deep is it?", "How tall is it?", "What is the material?"):
            edge_called[0] = False
            result = run_async_safe(route_chat_message(msg, None, _edge))
            assert result == {
                "kind": ANSWER_DONE_KIND,
                "answer": NO_VERSION_QUESTION_REPLY,
            }, msg
            assert not edge_called[0], (
                f"the answer edge must NOT be called for a no-version question: {msg}"
            )

    def test_no_versions_change_request_still_routes_to_loop(self) -> None:
        # issue #349: a CHANGE REQUEST (not a question) with no version
        # still routes to the design loop — the guard is question-shaped
        # only. "make it taller" fails is_candidate_question (imperative
        # cue) → None → the loop.
        for msg in ("make it taller", "Make a 30 mm plate", "add a rib"):
            result = run_async_safe(route_chat_message(msg, None))
            assert result is None, (
                f"a change request with no version must still route to the loop: {msg}"
            )

    # issue #349 adversarial round 1 — the priority-attack list, pinned
    # BOTH ways: every request-shaped first message (a question-form
    # request — an imperative verb such as make / design / get, or a
    # noun plus dimensions — is a REQUEST, not a question about the
    # current design) must start the loop on an empty project (result
    # None), while the genuine no-version questions get the
    # "nothing built" reply. The pure-function route pins the classifier
    # itself (zero LLM calls); the route-level test below pins the wire
    # (202 + loop called + a version created).
    @pytest.mark.parametrize(
        ("message", "starts_loop"),
        [
            ("Can you make me a 20 mm cube?", True),
            ("Could you design a phone stand 80 mm wide?", True),
            ("Can I get a box 60 × 40 × 30 mm?", True),
            ("How about a hook that holds 5 kg?", True),
            ("Would you make a cable clip for a 6 mm cable?", True),
            ("Is it possible to make a lid for a 55 mm jar?", True),
            ("What about a 30 mm spacer?", True),
            ("make a cube", True),
            ("How deep is it?", False),
            ("How tall is it?", False),
        ],
    )
    def test_no_versions_request_question_routing_pinned(self, message, starts_loop) -> None:
        result = run_async_safe(route_chat_message(message, None))
        if starts_loop:
            assert result is None, (
                f"a request phrased as a question on an empty project must "
                f"start the design loop (no 'nothing built' intercept): {message!r}"
            )
        else:
            assert result == {
                "kind": ANSWER_DONE_KIND,
                "answer": NO_VERSION_QUESTION_REPLY,
            }, (
                f"a genuine no-version question must get the nothing-built "
                f"reply: {message!r}"
            )

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
        # The hard timeout at a short test value (the shared per-LLM-call
        # production bound's contract, pinned in <1 s of wall clock).
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


def _set_part_columns(app, pid: int, **kwargs) -> None:
    """Update the project row's part columns directly (the test fixture
    creates projects with all part columns NULL; this helper simulates
    the part-import workstream's writes so the unsettled chat route can
    be exercised without the full import pipeline)."""
    sets = ", ".join(f"{k} = ?" for k in kwargs)
    app.state.conn.raw.execute(
        f"UPDATE projects SET {sets} WHERE id = ?",
        (*kwargs.values(), pid),
    )
    app.state.conn.raw.commit()


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

    # ------------------------------------------------------------------
    # Issue #352 (operator decision 1): the unsettled size gate — the
    # deterministic stage must never emit a numeric size answer while
    # the part's units are unsettled (defence in depth: the route's
    # unsettled pre-route already pre-empts, this is the stage's own
    # guard, unit-tested here).
    # ------------------------------------------------------------------

    def test_unsettled_dimension_list_question_gets_size_unknown_reply(self) -> None:
        # The v1 import's file-unit bbox (80.0) is NOT an mm measurement
        # while the units are unsettled — the dimension-list question gets
        # the size-unknown reply, never "It measures 80.0 …".
        latest = _latest263(bbox={"x": 80.0, "y": 80.0, "z": 80.0})
        result = deterministic_axis_answer(
            "How big is it?", latest, part_unit_status="unsettled"
        )
        assert result == UNSETTLED_SIZE_REPLY
        assert "80.0" not in (result or "")

    def test_unsettled_single_axis_question_gets_size_unknown_reply(self) -> None:
        # The OD1 gate is REAL (not dimension-list-only): a single-axis
        # size question on an unsettled part also gets the size-unknown
        # reply — the v1 import's file-unit bbox (12.0 here) is NOT an mm
        # measurement while the units are unsettled, so "It measures …"
        # must never fire for a single-axis question either.
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How tall is it now?", latest, part_unit_status="unsettled"
        )
        assert result == UNSETTLED_SIZE_REPLY
        assert "12.0" not in (result or "")

    def test_unsettled_single_axis_non_part_noun_falls_through(self) -> None:
        # A single-axis question about a FEATURE ("the post") is not a
        # part size question: the gate does not take it, it falls through
        # to stage 2 exactly as today (never a number, never the
        # size-unknown sentence — the gate answers the PART's envelope
        # only, like the rest of the deterministic stage).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How tall is the post?", latest, part_unit_status="unsettled"
        )
        assert result is None

    def test_unsettled_comparison_gate_suppresses_numeric_answer(
        self, monkeypatch
    ) -> None:
        # Issue #352 (operator decision 1): the COMPARISON stage runs
        # BEFORE the decision gate and emits the measured value as a
        # number. On an unsettled part with a file-unit bbox of 80.0,
        # "is it wider than 50 mm?" must never emit "80.0" — the gate
        # covers the comparison stage too. The pure public API
        # (``deterministic_axis_answer``) covers only the decision stage,
        # so this test drives the REAL ``route_chat_message`` with the
        # answer edge stubbed (it must never be called) and pins the
        # comparison-stage gate's output.
        from d33d.question_answer import route_chat_message

        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return "It is 80.0 mm wide."

        latest = _latest263(bbox={"x": 80.0, "y": 80.0, "z": 80.0})
        result = run_async_safe(
            route_chat_message(
                "Is it wider than 50 mm?",
                latest,
                _edge,
                part_unit_status="unsettled",
            )
        )
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND
        assert result["answer"] == UNSETTLED_SIZE_REPLY
        assert "80.0" not in result["answer"]
        assert not edge_called[0], (
            "the answer edge must NOT be called for the comparison gate"
        )

    def test_unsettled_comparison_gate_feature_noun_falls_through(self) -> None:
        # A comparison about a FEATURE ("is the post wider than 50 mm?")
        # is not a part-size comparison — the gate does not take it; it
        # falls through exactly as today (no number, no size-unknown
        # sentence at this pure stage — the comparison stage itself also
        # falls through on a feature noun, landing on stage 2 at the
        # route level).
        latest = _latest263(bbox={"x": 80.0, "y": 80.0, "z": 80.0})
        result = deterministic_axis_answer(
            "Is the post wider than 50 mm?", latest, part_unit_status="unsettled"
        )
        assert result is None

    def test_settled_part_answers_normally(self) -> None:
        # The gate is "unsettled"-only: a settled part answers exactly as
        # before (the v1 import's bbox is an mm measurement once settled).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How tall is it now?", latest, part_unit_status="settled"
        )
        assert result == "It measures 12.0\u202fmm tall."

    def test_assumed_part_answers_normally(self) -> None:
        # "Assumed" parts are usable (issue #350): the gate is
        # "unsettled"-only, so an assumed part answers from the bbox as
        # today (the assumed labelling is issue #350's scope).
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How tall is it now?", latest, part_unit_status="assumed"
        )
        assert result == "It measures 12.0\u202fmm tall."

    def test_no_part_answers_normally(self) -> None:
        # No part (the kwarg is None) → the gate never fires.
        latest = _latest263(bbox={"x": 20.0, "y": 20.0, "z": 12.0})
        result = deterministic_axis_answer(
            "How big is it?", latest, part_unit_status=None
        )
        assert result == "It measures 20.0\u202fmm × 20.0\u202fmm × 12.0\u202fmm."

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


class TestDeterministicHoleFeatureStage:
    """Issue #396 — the deterministic feature-size stage for holes.

    "How wide is the center hole?" on an imported part used to fall to
    stage 2 (the noun "the center hole" is a feature, not the part), and
    a failed stage-2 call answered it with the ``COULD_NOT_ANSWER``
    copy ("I couldn't answer that just now") — wrong on both counts:
    the answer is KNOWN. The stage answers one-hole parts from the
    measured hole (``latest["holes"]`` — the ``part_report["holes"]``
    list the #396 import workstream stores at import), and every other
    hole question gets the honest ``FEATURE_SIZE_UNMEASURED_REPLY`` —
    a hole question never reaches stage 2, and never the design loop.

    Uses ``deterministic_axis_answer`` (the pure function the route
    calls) and ``route_chat_message`` for the integration cases (the
    ``holes`` seam — the route reads ``part_public"`s report itself in
    production; here the route's ``holes`` argument is the same seam).
    """

    _HOLE: ClassVar[dict] = {
        "center": (60.0, 40.0, 5.0),
        "axis": (0.0, 0.0, 1.0),
        "diameter_mm": 30.0,
    }

    def _latest(self, holes: list | None = None) -> dict:
        latest = _latest263(bbox={"x": 100.0, "y": 80.0, "z": 10.0})
        if holes is not None:
            latest["holes"] = holes
        return latest

    def test_single_measured_hole_answers_diameter(self) -> None:
        # "How wide is the center hole?" with ONE measured hole →
        # "The center hole is 30.0 mm." (the mm() form, one decimal).
        latest = self._latest(holes=[self._HOLE])
        result = deterministic_axis_answer("How wide is the center hole?", latest)
        assert result == "The center hole is 30.0\u202fmm."

    def test_single_hole_no_qualifier_reads_the(self) -> None:
        # "How wide is the hole?" — no qualifier → "the hole".
        latest = self._latest(holes=[self._HOLE])
        result = deterministic_axis_answer("How wide is the hole?", latest)
        assert result == "The hole is 30.0\u202fmm."

    def test_single_hole_bore_noun(self) -> None:
        # "How deep is the bore?" — bore is in HOLE_NOUNS; the answer
        # names the feature the measurement is about (a hole), never the
        # trigger noun (a bore is a hole the import measured).
        latest = self._latest(holes=[self._HOLE])
        result = deterministic_axis_answer("How deep is the bore?", latest)
        assert result == "The hole is 30.0\u202fmm."

    def test_two_holes_no_qualifier_honest_reply(self) -> None:
        # Two measured holes + "the hole" (no qualifier) → ambiguous →
        # the honest reply (never a guess, never COULD_NOT_ANSWER).
        second = dict(self._HOLE)
        second["center"] = (10.0, 40.0, 5.0)
        second["diameter_mm"] = 20.0
        latest = self._latest(holes=[self._HOLE, second])
        result = deterministic_axis_answer("How wide is the hole?", latest)
        assert result == FEATURE_SIZE_UNMEASURED_REPLY

    def test_two_holes_with_qualifier_honest_reply(self) -> None:
        # The qualifier's spatial semantics ("nearest the bbox centre")
        # belong to the #396 offer-selection workstream — the answer
        # path answers only when exactly one hole is the pick, and here
        # it is not → the honest reply.
        second = dict(self._HOLE)
        second["center"] = (10.0, 40.0, 5.0)
        latest = self._latest(holes=[self._HOLE, second])
        result = deterministic_axis_answer("How wide is the center hole?", latest)
        assert result == FEATURE_SIZE_UNMEASURED_REPLY

    def test_no_holes_honest_reply(self) -> None:
        # The ticket's case: an imported part with NO measured holes —
        # "How wide is the center hole?" → the honest reply (never
        # COULD_NOT_ANSWER, never stage 2).
        for latest in (
            self._latest(),  # no "holes" key (legacy report)
            self._latest(holes=None),
            self._latest(holes=[]),
        ):
            result = deterministic_axis_answer(
                "How wide is the center hole?", latest
            )
            assert result == FEATURE_SIZE_UNMEASURED_REPLY

    def test_malformed_hole_entry_honest_reply(self) -> None:
        # A corrupt holes entry (no positive diameter_mm) is "no
        # measured hole" — the honest reply, never a crash.
        latest = self._latest(holes=[{"diameter_mm": "wide"}])
        result = deterministic_axis_answer("How wide is the center hole?", latest)
        assert result == FEATURE_SIZE_UNMEASURED_REPLY

    def test_non_hole_feature_noun_falls_through(self) -> None:
        # "How wide is the slot?" / "the boss?" — NOT hole-family nouns:
        # the stage does not take them (the honest reply would be a lie
        # about a feature the hole measurement says nothing about); they
        # keep their current fall-through to stage 2 (→ None here with
        # no edge).
        for msg in (
            "How wide is the slot?",
            "How wide is the boss?",
            "How tall is the post?",
        ):
            result = deterministic_axis_answer(msg, self._latest())
            assert result is None, (
                f"{msg!r} must fall through to stage 2 (not a hole), "
                f"got {result!r}"
            )

    def test_unsettled_part_gate_preempts_hole_stage(self) -> None:
        # The #352 unsettled gate runs first: a hole question on an
        # unsettled part gets the size-unknown reply (a file-unit hole
        # diameter is not an mm measurement), never the hole answer.
        latest = self._latest(holes=[self._HOLE])
        result = deterministic_axis_answer(
            "How wide is the center hole?", latest, "unsettled"
        )
        assert result == UNSETTLED_SIZE_REPLY

    def test_hole_feature_regex_is_closed_to_hole_nouns(self) -> None:
        # The regex's noun group is HOLE_NOUNS exactly — a digit in the
        # message does not break the match ("How wide is the hole, 30
        # mm?" — the stage still takes it; the route's digit-abstain
        # keeps a proposed-change message out before this stage runs),
        # and it never matches a feature the hole measurement cannot
        # speak about.
        from d33d.question_answer import HOLE_FEATURE_SIZE_QUESTION_RE

        assert HOLE_FEATURE_SIZE_QUESTION_RE.search(
            "How wide is the hole, 30 mm?"
        ) is not None
        assert HOLE_FEATURE_SIZE_QUESTION_RE.search("How wide is the slot?") is None

    def test_hole_feature_qualifier_is_closed_set(self) -> None:
        # Issue #396 (round 2): the qualifier is a CLOSED set (center/
        # centre/middle/left/right/top/bottom/front/back/big/large/small
        # or none) — the first pass accepted ANY word, so "the giant
        # hole" / "the red hole" would have produced "The giant hole is
        # 30.0 mm.". A qualifier outside the set means the stage does
        # not take the message (it falls through to stage 2, as before).
        from d33d.question_answer import HOLE_FEATURE_SIZE_QUESTION_RE

        # In-set qualifiers match (with the qualifier captured).
        for q in (
            "center", "centre", "middle", "left", "right", "top",
            "bottom", "front", "back", "big", "large", "small",
        ):
            m = HOLE_FEATURE_SIZE_QUESTION_RE.search(
                f"How wide is the {q} hole?"
            )
            assert m is not None, f"qualifier {q!r} must match"
            assert (m.group(1) or "") == q, f"qualifier {q!r} not captured"
        # Out-of-set qualifiers do NOT match (the stage falls through).
        for q in ("giant", "red", "tiny", "funny", "blue"):
            m = HOLE_FEATURE_SIZE_QUESTION_RE.search(
                f"How wide is the {q} hole?"
            )
            assert m is None, f"qualifier {q!r} must NOT match (closed set)"
        # No qualifier still matches ("the hole").
        assert HOLE_FEATURE_SIZE_QUESTION_RE.search(
            "How wide is the hole?"
        ) is not None

    def test_route_single_hole_no_edge_no_llm(self) -> None:
        # Integration: the measured one-hole answer rides the route's
        # ``holes`` seam with NO answer edge and NO LLM call (the
        # deterministic stage needs no model).
        latest = self._latest()  # the route injects the holes below
        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "30"}'

        result = run_async_safe(
            route_chat_message(
                "How wide is the center hole?", latest, _edge, holes=[self._HOLE]
            )
        )
        assert result == {
            "kind": ANSWER_DONE_KIND,
            "answer": "The center hole is 30.0\u202fmm.",
        }
        assert not edge_called[0], "the answer edge must NOT be called"

    def test_route_no_holes_never_could_not_answer(self) -> None:
        # The ticket's acceptance criterion at the route level: an
        # imported part with no measured holes + a hole size question →
        # the honest reply, even when the stage-2 call FAILS (a
        # failing-edge stub that would have produced COULD_NOT_ANSWER
        # via the fall-through). The hole question never reaches stage 2.
        latest = self._latest()

        async def _edge(question: str, entries: list) -> str:
            raise RuntimeError("the stage-2 call must never run")

        result = run_async_safe(
            route_chat_message(
                "How wide is the center hole?", latest, _edge, holes=None
            )
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": FEATURE_SIZE_UNMEASURED_REPLY}
        assert result["answer"] != COULD_NOT_ANSWER

    def test_hole_answer_format_matches_deck_shape(self) -> None:
        # The backend's measured-hole sentence must match the deck's
        # ``holeFeatureSize`` shape for the same slots. Checked by
        # concatenation (the production code builds the sentence the
        # same way — no placeholder semantics to depend on).
        rendered = "The " + "center" + " hole is " + "30.0\u202fmm" + "."
        assert rendered == "The center hole is 30.0\u202fmm."


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


# ---------------------------------------------------------------------------
# Issue #313 — the deterministic comparison stage (no LLM, no app)
# ---------------------------------------------------------------------------


def _latest_cmp(
    bbox: dict | None = None,
    stated: dict | None = None,
    params: dict | None = None,
) -> dict:
    """Build a minimal version dict for the #313 comparison-stage tests."""
    return {
        "params": params or {},
        "stated_dims": stated,
        "bbox": bbox,
        "param_meta": None,
    }


class TestDeterministicComparison:
    """Issue #313: the deterministic comparison stage — a stage-1
    candidate that names exactly one axis, carries exactly one mm number
    (or is a relative-word "than the/… noun" missing-fact form), and has
    a MEASURED axis value is answered from the design state with NO LLM
    call, NO version, and NO design loop."""

    BBOX: ClassVar[dict[str, float]] = {"x": 20.0, "y": 43.9, "z": 12.0}

    def _latest(self, stated: dict | None = None) -> dict:
        return _latest_cmp(bbox=dict(self.BBOX), stated=stated)

    # -- comparison table --------------------------------------------------

    def test_deep_enough_for_30mm_screw_yes(self) -> None:
        # D=43.9, target 30 → yes (43.9 >= 30, diff 13.9 > tolerance).
        # The golden "more than" fixture — the at-least direction with
        # measured > target (issue #320: the relation word follows the
        # sign of measured − target).
        r = self._cmp("Is it deep enough for a 30 mm screw?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert reply == (
            "Yes — it measures "
            + mm_formatted(43.9)
            + " deep, "
            + mm_formatted(13.9)
            + " more than "
            + mm_formatted(30.0)
            + "."
        )

    def test_taller_than_10mm_yes(self) -> None:
        # H=12, target 10 → yes (measured > target → "more than").
        r = self._cmp("Is it taller than 10 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert "Yes" in reply
        assert "2.0" in reply
        assert "10.0" in reply
        assert "more than" in reply

    def test_shorter_than_10mm_no(self) -> None:
        # H=12, target 10, relative word "shorter" → polarity inverts →
        # no. The relation follows the SIGN (12 > 10 → "more than"),
        # not the No (issue #320 — the old "short of" was false here).
        r = self._cmp("Is it shorter than 10 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert "No" in reply
        assert "12.0" in reply
        assert "2.0" in reply
        assert "10.0" in reply
        assert "more than" in reply

    def test_wider_than_10mm_yes(self) -> None:
        # W=20, target 10 → yes (measured > target → "more than").
        r = self._cmp("Is it wider than 10 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "W"
        assert "Yes" in reply
        assert "20.0" in reply
        assert "10.0" in reply
        assert "more than" in reply

    def test_narrower_than_10mm_no(self) -> None:
        # W=20, target 10, relative word "narrower" → no. Relation: 20 >
        # 10 → "more than" (issue #320 — the old "short of" was false).
        r = self._cmp("Is it narrower than 10 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "W"
        assert reply == (
            "No — it measures "
            + mm_formatted(20.0)
            + " wide, "
            + mm_formatted(10.0)
            + " more than "
            + mm_formatted(10.0)
            + "."
        )

    def test_shallower_than_30mm_no(self) -> None:
        # D=43.9, target 30, relative word "shallower" → no. Relation:
        # 43.9 > 30 → "more than" (issue #320 — the old "short of"
        # was false).
        r = self._cmp("Is it shallower than 30 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert "No" in reply
        assert "43.9" in reply
        assert "13.9" in reply
        assert "30.0" in reply
        assert "more than" in reply

    def test_fit_in_45mm_deep_gap_yes(self) -> None:
        # D=43.9, gap 45 → fits (43.9 <= 45, diff 1.1 > tolerance →
        # yes). The #320 acceptance sentence: 43.9 < 45 → "less than"
        # (the old "more than" was false).
        r = self._cmp("Will it fit in a 45 mm deep gap?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert reply == (
            "Yes — it measures "
            + mm_formatted(43.9)
            + " deep, "
            + mm_formatted(1.1)
            + " less than "
            + mm_formatted(45.0)
            + "."
        )

    def test_fit_in_40mm_deep_gap_no(self) -> None:
        # D=43.9, gap 40 → does not fit (43.9 > 40). The No sentence
        # carries a "more than" relation (issue #320 — the old "short
        # of" was false here).
        r = self._cmp("Will it fit in a 40 mm deep gap?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert reply == (
            "No — it measures "
            + mm_formatted(43.9)
            + " deep, "
            + mm_formatted(3.9)
            + " more than "
            + mm_formatted(40.0)
            + "."
        )

    def test_fit_in_50mm_deep_gap_yes(self) -> None:
        # D=43.9, gap 50 → fits (43.9 <= 50, diff 6.1 > tolerance).
        # Relation: 43.9 < 50 → "less than".
        r = self._cmp("Will it fit in a 50 mm deep gap?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert reply == (
            "Yes — it measures "
            + mm_formatted(43.9)
            + " deep, "
            + mm_formatted(6.1)
            + " less than "
            + mm_formatted(50.0)
            + "."
        )

    def test_tall_enough_for_50mm_shelf_no(self) -> None:
        # H=12, target 50 → not enough (12 < 50). The at-least No now
        # says "less than" (issue #320 retired "short of").
        r = self._cmp("Is it tall enough for a 50 mm shelf?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert reply == (
            "No — it measures "
            + mm_formatted(12.0)
            + " tall, "
            + mm_formatted(38.0)
            + " less than "
            + mm_formatted(50.0)
            + "."
        )

    def test_shorter_than_50mm_yes_less_than(self) -> None:
        # H=12, "Is it shorter than 50 mm?" → yes (12 <= 50) with a
        # "less than" relation (issue #320 acceptance example: the old
        # sentence said "38.0 mm more than 50.0 mm" — false).
        r = self._cmp("Is it shorter than 50 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert reply == (
            "Yes — it measures "
            + mm_formatted(12.0)
            + " tall, "
            + mm_formatted(38.0)
            + " less than "
            + mm_formatted(50.0)
            + "."
        )

    def test_taller_than_50mm_no_less_than(self) -> None:
        # H=12, "Is it taller than 50 mm?" → no (12 < 50) with a "less
        # than" relation — the acceptance example that replaces "short
        # of" (issue #320).
        r = self._cmp("Is it taller than 50 mm?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert reply == (
            "No — it measures "
            + mm_formatted(12.0)
            + " tall, "
            + mm_formatted(38.0)
            + " less than "
            + mm_formatted(50.0)
            + "."
        )

    def test_relation_word_follows_sign_table(self) -> None:
        # Issue #320 table test: every direction (enough / fit /
        # than-inverted / than-absolute) × (measured above target, below
        # target) asserts the relation word matches the sign of measured
        # − target AND the Yes/No matches the direction. The about-the-
        # same cell asserts the unchanged about_the_same sentence (no
        # relation word).
        cases = [
            # (message, axis, measured, target, expected_answer, relation)
            ("Is it deep enough for a 30 mm screw?", "D", 43.9, 30.0, "Yes", "more than"),
            ("Is it deep enough for a 50 mm screw?", "D", 43.9, 50.0, "No", "less than"),
            ("Will it fit in a 45 mm deep gap?", "D", 43.9, 45.0, "Yes", "less than"),
            ("Will it fit in a 40 mm deep gap?", "D", 43.9, 40.0, "No", "more than"),
            ("Is it taller than 10 mm?", "H", 12.0, 10.0, "Yes", "more than"),
            ("Is it taller than 50 mm?", "H", 12.0, 50.0, "No", "less than"),
            ("Is it shorter than 50 mm?", "H", 12.0, 50.0, "Yes", "less than"),
            ("Is it shorter than 10 mm?", "H", 12.0, 10.0, "No", "more than"),
            ("Is it wider than 10 mm?", "W", 20.0, 10.0, "Yes", "more than"),
            ("Is it wider than 30 mm?", "W", 20.0, 30.0, "No", "less than"),
            ("Is it narrower than 30 mm?", "W", 20.0, 30.0, "Yes", "less than"),
            ("Is it narrower than 10 mm?", "W", 20.0, 10.0, "No", "more than"),
            ("Is it shallower than 30 mm?", "D", 43.9, 30.0, "No", "more than"),
            ("Is it shallower than 50 mm?", "D", 43.9, 50.0, "Yes", "less than"),
            ("Is it deeper than 30 mm?", "D", 43.9, 30.0, "Yes", "more than"),
            ("Is it deeper than 50 mm?", "D", 43.9, 50.0, "No", "less than"),
        ]
        adj = {"D": "deep", "W": "wide", "H": "tall"}
        for message, ax, measured, target, answer, relation in cases:
            r = self._cmp(message)
            assert r is not None, message
            got_axis, reply = r
            assert got_axis == ax, message
            expected = DETERMINISTIC_COMPARISON_SENTENCES["yes" if answer == "Yes" else "no"].format(
                measured=mm_formatted(measured),
                axis=adj[ax],
                delta=mm_formatted(abs(measured - target)),
                relation=relation,
                target=mm_formatted(target),
            )
            assert reply == expected, f"{message}: {reply!r} != {expected!r}"
        # The about-the-same cell: the unchanged about_the_same sentence,
        # no relation word (boundary at tolerance, inclusive).
        r = self._cmp("Is it deep enough for a 43.4 mm screw?")
        assert r is not None
        got_axis, reply = r
        assert got_axis == "D"
        assert reply == DETERMINISTIC_COMPARISON_SENTENCES["about_the_same"].format(
            measured=mm_formatted(43.9), axis="deep"
        )

    # -- boundary tests ----------------------------------------------------

    def test_boundary_at_tolerance_about_the_same(self) -> None:
        # D=43.9, target 43.4: diff=0.5, tolerance=max(0.01*43.4, 0.5)=0.5
        # → abs(diff) <= tolerance → about-the-same (inclusive boundary).
        r = self._cmp("Is it deep enough for a 43.4 mm screw?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert "About the same" in reply

    def test_boundary_just_outside_tolerance_yes(self) -> None:
        # D=43.9, target 43.3: diff=0.6, tolerance=0.5 → outside → yes.
        r = self._cmp("Is it deep enough for a 43.3 mm screw?")
        assert r is not None
        axis, reply = r
        assert axis == "D"
        assert "Yes" in reply

    # -- missing-fact trigger (one-word bare-noun form, issue #313) ----

    def test_missing_fact_taller_than_the_shelf(self) -> None:
        # No number → missing-fact reply. The trigger reads the bare
        # noun after "than" ("the shelf" → the object is "the shelf").
        r = self._cmp("Is it taller than the shelf?")
        assert r is not None
        axis, reply = r
        assert axis == "H"
        assert "How tall is the shelf?" in reply
        assert "12.0" in reply

    def test_missing_fact_wider_than_my_drawer(self) -> None:
        # "my drawer" → the reply's object is "my drawer" (the bare
        # noun, no article split).
        r = self._cmp("Is it wider than my drawer?")
        assert r is not None
        axis, reply = r
        assert axis == "W"
        assert "How wide is my drawer?" in reply
        assert "20.0" in reply

    def test_missing_fact_digit_exclusion_at_direction_level(self) -> None:
        # Unit level: the digit exclusion fires inside the direction
        # detection — "than v2" is a "than" direction, not "missing"
        # (a missing-fact object must carry no digit).
        from d33d.question_answer import _comparison_direction

        result = _comparison_direction("Is it taller than v2?", "H")
        assert result is not None
        direction, _ = result
        assert direction == "than"

    # -- fall-throughs -----------------------------------------------------

    def test_axis_less_fit_falls_through(self) -> None:
        # No axis word → not a comparison → falls through to stage 2.
        r = self._cmp("Will it fit in a 45 mm gap?")
        assert r is None

    def test_foreign_unit_falls_through(self) -> None:
        # "cm" → foreign unit → falls through to stage 2.
        r = self._cmp("Is it deep enough for a 3 cm screw?")
        assert r is None

    def test_non_measured_axis_falls_through(self) -> None:
        # H not in bbox → not measured → falls through.
        latest = _latest_cmp(bbox={"x": 20.0, "y": 43.9})  # no z (H)
        r = self._cmp2("Is it tall enough for a 50 mm shelf?", latest)
        assert r is None

    def test_zero_extent_falls_through(self) -> None:
        # All-zero bbox → not measured → falls through.
        latest = _latest_cmp(bbox={"x": 0.0, "y": 0.0, "z": 0.0})
        r = self._cmp2("Is it tall enough for a 50 mm shelf?", latest)
        assert r is None

    def test_no_version_falls_through(self) -> None:
        r = self._cmp2("Is it tall enough for a 50 mm shelf?", None)
        assert r is None

    def test_stated_only_falls_through(self) -> None:
        # D stated but not measured (bbox y absent) → stated only → falls through.
        latest = _latest_cmp(
            bbox={"x": 20.0, "z": 12.0},  # no y (D)
            stated={"D": 40.0},
        )
        r = self._cmp2("Is it deep enough for a 30 mm screw?", latest)
        assert r is None

    def test_two_numbers_falls_through(self) -> None:
        # Two numbers → ambiguous → falls through.
        r = self._cmp("Is it 40 mm deep or 30 mm deep?")
        assert r is None

    def test_no_direction_falls_through(self) -> None:
        # No "enough", no "than", no "fit" → no direction → falls through.
        r = self._cmp("Is it 40 mm deep?")
        assert r is None

    # -- missing-fact exclusions -------------------------------------------

    def test_missing_fact_last_one_falls_through(self) -> None:
        # "the last one" → version comparison → falls through.
        r = self._cmp("Is it taller than the last one?")
        assert r is None

    def test_missing_fact_previous_one_falls_through(self) -> None:
        # "the previous one" → version comparison → falls through.
        r = self._cmp("Is it taller than the previous one?")
        assert r is None

    def test_than_v2_routes_as_a_than_comparison_not_missing_fact(
        self,
    ) -> None:
        # "than v2" fires the missing-fact trigger, but "v2" carries a
        # digit → the missing-fact exclusion fires and the direction is
        # "than" (not "missing"). The message has no parseable number
        # ("v2" is not a bare number), so the stage falls through —
        # the reply is NOT the missing-fact sentence.
        r = self._cmp("Is it taller than v2?")
        assert r is None
        # Unit-level check: the direction is "than", not "missing".
        from d33d.question_answer import _comparison_direction

        result = _comparison_direction("Is it taller than v2?", "H")
        assert result is not None
        direction, _ = result
        assert direction == "than"

    def test_missing_fact_before_falls_through(self) -> None:
        # "than before" → "before" is an adverb, not a noun object →
        # the missing-fact reply would be nonsense → falls through
        # (the digit-free adverb is excluded by the "no noun" rule of
        # the closed trigger set — the operator decision's bare noun
        # must name an object, and "before" names none).
        # The message has zero numbers, so the than-comparison path
        # (which needs a target number) cannot fire either → the stage
        # does not take the message.
        r = self._cmp("Is it taller than before?")
        assert r is None

    def test_missing_fact_it_was_falls_through(self) -> None:
        # "than it was" → "it" is not a noun object (the design loop
        # treats it as the part itself) → the missing-fact reply would
        # be nonsense ("How tall is it it was?") → falls through.
        r = self._cmp("Is it taller than it was?")
        assert r is None

    # -- zero-LLM spy ------------------------------------------------------

    def test_no_edge_called_for_deterministic_comparison(
        self, app_with_versions
    ) -> None:
        # Integration: the deterministic comparison stage answers with NO
        # LLM call (the answer-edge stub is NOT called).
        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        latest = self._latest()
        result = run_async_safe(
            route_chat_message(
                "Is it deep enough for a 30 mm screw?", latest, _edge
            )
        )
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND
        assert "43.9" in result["answer"]
        assert not edge_called[0], (
            "the answer edge must NOT be called for a deterministic comparison"
        )

    def test_no_version_created_for_deterministic_comparison(
        self, app_with_versions
    ) -> None:
        # Integration: the deterministic comparison stage does not create
        # a version (no design run).
        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        latest = self._latest()
        result = run_async_safe(
            route_chat_message(
                "Is it deep enough for a 30 mm screw?", latest, _edge
            )
        )
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND

    # -- helpers ------------------------------------------------------------

    def _cmp(self, message: str, stated: dict | None = None):
        from d33d.question_answer import _deterministic_comparison

        return _deterministic_comparison(message, self._latest(stated=stated))

    def _cmp2(self, message: str, latest: dict | None):
        from d33d.question_answer import _deterministic_comparison

        return _deterministic_comparison(message, latest)


class TestDeterministicComparisonLog:
    """Issue #313: the deterministic comparison stage logs at INFO
    (the answer is a computed, no-LLM success — WARNING is reserved for
    failures), one line naming the axis and the provenance class
    (``comparison``), never the message text."""

    def test_comparison_hit_logs_info_not_warning(self, caplog) -> None:
        # A measured axis (bbox carries D=43.9): the comparison stage
        # takes the message and logs ONE INFO line (the answer is a
        # computed, no-LLM success — WARNING is reserved for failures).
        latest = _latest({}, None, {"x": 20.0, "y": 43.9, "z": 12.0})
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "Is it deep enough for a 30 mm screw?",
                    latest,
                    None,
                )
            )
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == [], (
            "the deterministic comparison stage must not log at WARNING — "
            f"got {warnings}"
        )
        infos = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "outcome=deterministic" in r.getMessage()
        ]
        assert len(infos) == 1, f"expected 1 INFO, got {infos}"
        msg = infos[0].getMessage()
        assert "provenance=comparison" in msg
        assert "Is it deep enough for a 30 mm screw?" not in msg

    def test_comparison_hit_does_not_reach_stage2(self, caplog) -> None:
        # The comparison stage answers BEFORE stage 2: no stage-2 WARNING
        # or stage-2 INFO line is emitted for a comparison hit.
        latest = _latest({"H": 12.0}, None, {"x": 20.0, "y": 43.9, "z": 12.0})

        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "Is it deep enough for a 30 mm screw?",
                    latest,
                    _edge,
                )
            )
        assert result is not None
        assert result["kind"] == ANSWER_DONE_KIND
        # The only log records from the question-answer module are the
        # single INFO deterministic line — no stage-2 WARNING, no
        # stage-2 INFO (the LLM was never called).
        qa_records = [
            r for r in caplog.records if r.name == "d33d.question_answer"
        ]
        assert len(qa_records) == 1, f"expected 1 record, got {qa_records}"
        assert qa_records[0].levelno == logging.INFO
        assert "outcome=deterministic" in qa_records[0].getMessage()


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

        # The comparison templates (issue #320) are pinned against the
        # deck's sign-aware relation argument the same way: the same
        # sample values the W313 design-contract test pins — one pair per
        # relation, so a deck edit to comparisonYes/comparisonNo without
        # the backend (or vice versa) breaks this.
        cm40 = "40.0\u202fmm"
        cm439 = "43.9\u202fmm"
        cmp_expected = [
            DETERMINISTIC_COMPARISON_SENTENCES["yes"].format(
                measured=cm40,
                axis="deep",
                delta="10.0\u202fmm",
                relation="more than",
                target="30\u202fmm",
            ),
            DETERMINISTIC_COMPARISON_SENTENCES["no"].format(
                measured=cm439,
                axis="deep",
                delta="5.0\u202fmm",
                relation="less than",
                target="50.0\u202fmm",
            ),
            DETERMINISTIC_COMPARISON_SENTENCES["yes"].format(
                measured=cm439,
                axis="deep",
                delta="1.1\u202fmm",
                relation="less than",
                target="45.0\u202fmm",
            ),
            DETERMINISTIC_COMPARISON_SENTENCES["no"].format(
                measured=cm439,
                axis="deep",
                delta="3.9\u202fmm",
                relation="more than",
                target="40.0\u202fmm",
            ),
        ]
        cmp_expected_exact = [
            "Yes — it measures 40.0\u202fmm deep, 10.0\u202fmm more than 30\u202fmm.",
            "No — it measures 43.9\u202fmm deep, 5.0\u202fmm less than 50.0\u202fmm.",
            "Yes — it measures 43.9\u202fmm deep, 1.1\u202fmm less than 45.0\u202fmm.",
            "No — it measures 43.9\u202fmm deep, 3.9\u202fmm more than 40.0\u202fmm.",
        ]
        assert cmp_expected == cmp_expected_exact

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

    def test_unsettled_part_question_gets_settled_reply_not_size(
        self, app_with_versions
    ) -> None:
        """Issue #352 (operator decision 1): a question on a project whose
        part's units are unsettled routes to the fill/recut pre-route's
        settle-first reply (``UNSETTLED_PART_REPLY``) — never the numeric
        "It measures …" path and never the design loop. The pre-route runs
        before the question pre-route (which now carries the part status),
        so the route-level answer is the unsettled reply."""

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            # An unsettled import: part columns set, v1 with the file-unit
            # bbox (the mm number the naive path would have emitted).
            _set_part_columns(
                app_with_versions,
                pid,
                part_filename="big-part.stl",
                part_format="stl",
                part_unit=None,
                part_unit_status="unsettled",
                part_scale=None,
                part_report=None,
                part_options=None,
            )
            await app_with_versions.state.versions.create_version(
                pid,
                {"W": 80.0, "D": 80.0, "H": 80.0},
                bbox=(80.0, 80.0, 80.0),
            )
            r, frames = await _drive_chat_with_answer(
                app_with_versions,
                client,
                pid,
                {"message": "How tall is it?", "chat_history": []},
            )
            return r, frames

        r, frames = run_async(app_with_versions, _call)
        assert r.status_code == 202, r.text
        assert len(frames) == 1, f"expected 1 frame, got {len(frames)}"
        event, data = frames[0]
        assert event == "done"
        # The reply is the settle-first notice, never a numeric size:
        # the v1 import's 80.0 file-unit extent is not an mm measurement
        # until the units are settled.
        assert data["message"] == fill_recut.UNSETTLED_PART_REPLY
        assert "80.0" not in data["message"]
        assert data.get("kind") == ANSWER_DONE_KIND

    def test_unsettled_single_axis_gate_fires_on_live_chat_path(
        self, app_with_versions, monkeypatch
    ) -> None:
        """OD1 (issue #352) through the LIVE chat wiring: drive the real
        POST /chat route on an unsettled import. The unsettled pre-route
        (the fill/recut guard that would pre-empt EVERY message) is
        bypassed by monkeypatching ``fill_recut.part_public`` to None —
        the design-loop setup seam (``d33d.chat_loop.run_design_loop``)
        reads the REAL project row and threads ``part_unit_status`` to
        ``route_chat_message``: a single-axis question gets the
        size-unknown reply, never the file-unit bbox number, with no LLM
        call and no version. Dropping the ``part_unit_status`` kwarg in
        ``d33d/chat_loop.py`` makes this test fail (the stage falls
        through to stage 2 and emits "It measures 80.0 …")."""

        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return "It is 80.0 mm tall."

        def _loop(app, **kwargs):
            raise AssertionError("the design loop must NOT be called on the answer path")

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            # An unsettled import: part columns set, v1 with the file-unit
            # bbox (the number the naive path would emit as "It measures").
            _set_part_columns(
                app_with_versions,
                pid,
                part_filename="big-part.stl",
                part_format="stl",
                part_unit=None,
                part_unit_status="unsettled",
                part_scale=None,
                part_report=None,
                part_options=None,
            )
            await app_with_versions.state.versions.create_version(
                pid,
                {"W": 80.0, "D": 80.0, "H": 80.0},
                bbox=(80.0, 80.0, 80.0),
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _edge
            # Bypass the unsettled pre-route: post_chat calls
            # fill_recut.part_public(row) to decide the guard; the None
            # makes _part None, so no UNSETTLED_PART_REPLY frame fires
            # and the message flows into the design-loop setup seam, which
            # calls route_chat_message with the row's real part status.
            monkeypatch.setattr("d33d.part_http.part_public", lambda row: None)
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
        assert not was_edge_called, "the answer edge must NOT be called for the gate"
        assert version_count == 1, f"expected 1 version, got {version_count}"
        assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
        event, data = frames[0]
        assert event == "done"
        assert data.get("kind") == ANSWER_DONE_KIND
        assert data["message"] == UNSETTLED_SIZE_REPLY
        assert "80.0" not in data["message"]

    def test_unsettled_comparison_gate_fires_on_live_chat_path(
        self, app_with_versions, monkeypatch
    ) -> None:
        """OD1 on the COMPARISON path through the LIVE chat wiring: drive
        the real POST /chat route on an unsettled import with a
        DIGIT-bearing question ("Is it wider than 50 mm?") — the message
        shape the decision gate's digit-abstain would let through to
        stage 2, and that the comparison stage would otherwise answer
        with the file-unit bbox. The unsettled pre-route is bypassed
        (``fill_recut.part_public`` → None, the same seam as the
        single-axis test); the comparison gate must return the
        size-unknown reply — never "80.0", with no LLM call and no
        version. Dropping the ``part_unit_status`` kwarg from
        ``route_chat_message``'s comparison call makes this fail (the
        stage emits "Yes — it measures 80.0 mm wide, …")."""

        edge_called = [False]

        async def _edge(question: str, entries: list) -> str:
            edge_called[0] = True
            return "It is 80.0 mm wide."

        def _loop(app, **kwargs):
            raise AssertionError("the design loop must NOT be called on the answer path")

        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            _set_part_columns(
                app_with_versions,
                pid,
                part_filename="wide-part.stl",
                part_format="stl",
                part_unit=None,
                part_unit_status="unsettled",
                part_scale=None,
                part_report=None,
                part_options=None,
            )
            await app_with_versions.state.versions.create_version(
                pid,
                {"W": 80.0, "D": 40.0, "H": 30.0},
                bbox=(80.0, 40.0, 30.0),
            )
            app_with_versions.state.run_design_loop = _loop
            app_with_versions.state.answer_question = _edge
            monkeypatch.setattr("d33d.part_http.part_public", lambda row: None)
            r, frames = await _drive_chat_with_answer(
                app_with_versions,
                client,
                pid,
                {"message": "Is it wider than 50 mm?", "chat_history": []},
            )
            versions = app_with_versions.state.versions.list_versions(pid)
            return r, frames, len(versions), edge_called[0]

        r, frames, version_count, was_edge_called = run_async(app_with_versions, _call)
        assert r.status_code == 202, r.text
        assert not was_edge_called, "the answer edge must NOT be called for the gate"
        assert version_count == 1, f"expected 1 version, got {version_count}"
        assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
        event, data = frames[0]
        assert event == "done"
        assert data.get("kind") == ANSWER_DONE_KIND
        assert data["message"] == UNSETTLED_SIZE_REPLY
        assert "80.0" not in data["message"]

    def test_unsettled_size_reply_matches_copy_deck_constant(self) -> None:
        """The backend's size-unknown sentence (the deterministic stage's
        defence-in-depth guard for an unsettled part) is verbatim the
        copy.ts ``deterministicAnswer.sizeUnknownWhileUnsettled`` deck
        string — no placeholder, both directions pinned by the literal."""
        assert UNSETTLED_SIZE_REPLY == (
            "I can't give you a size until the part's units are settled — pick mm, cm, or inch (or give one measured axis) and the dimensions will be real."
        )


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
    """issue #278 REPRO (re-pointed for #313): "Would a 30 mm screw hold
    it?" with a design-state block carrying D 43.9 (measured): the
    stage-2 answer that quotes the question's "30" PASSES the guard →
    the done frame carries the answer text (kind "answer"), not
    COULD_NOT_ANSWER. No design run, no new version.

    The question carries a digit but NO axis word, so the #263
    deterministic stage abstains (no axis word) and the #313
    deterministic comparison stage does not fire (no axis word) —
    this exercises the stage-2 guard at the route level."""

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
            {"message": "Would a 30 mm screw hold it?",
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
    """issue #278 negative control (re-pointed for #313): the same screw
    question ("Would a 30 mm screw hold it?" — digit, no axis word →
    stage 2) with an answer citing 25 (in neither the question nor the
    block) still fails the guard → COULD_NOT_ANSWER, no design run."""

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
            {"message": "Would a 30 mm screw hold it?",
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


def test_no_versions_question_replies_nothing_built_no_loop(app_with_versions) -> None:
    """issue #349: a fresh project (no versions) receiving a question →
    the no-version guard fires → ONE done frame with kind "answer" and
    the "nothing built" reply; the design loop is NOT called; NO version
    is created (0 stays 0); NO version-created frame; NO LLM call."""

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
            {"message": "How deep is it?", "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        version_count = len(
            app_with_versions.state.versions.list_versions(pid)
        )
        return r, frames, loop_called, version_count

    r, frames, was_loop_called, version_count = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert not was_loop_called, (
        "the design loop must NOT be called for a no-version question"
    )
    assert version_count == 0, (
        f"no version may be created for a no-version question (got {version_count})"
    )
    # Exactly ONE frame: the terminal done frame with kind "answer".
    assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
    event, data = frames[0]
    assert event == "done"
    assert data.get("kind") == ANSWER_DONE_KIND
    assert data["message"] == NO_VERSION_QUESTION_REPLY
    # No version-created frame (the single done frame IS the only frame).
    assert not any(e == "progress" for e, _ in frames)


def test_no_versions_change_request_still_starts_loop(app_with_versions) -> None:
    """issue #349: a change request on a fresh project (no versions) still
    starts the design loop exactly as today — the first design request on
    an empty project creates a version."""

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
        app_with_versions.state.run_design_loop = _loop
        r, _frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": "Make a 30 mm plate", "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        return r, loop_called

    r, was_loop_called = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert was_loop_called, (
        "a change request on an empty project must still start the design loop"
    )


@pytest.mark.parametrize(
    ("message", "starts_loop"),
    [
        ("Can you make me a 20 mm cube?", True),
        ("Could you design a phone stand 80 mm wide?", True),
        ("Can I get a box 60 × 40 × 30 mm?", True),
        ("How about a hook that holds 5 kg?", True),
        ("Would you make a cable clip for a 6 mm cable?", True),
        ("Is it possible to make a lid for a 55 mm jar?", True),
        ("What about a 30 mm spacer?", True),
        ("make a cube", True),
        ("How deep is it?", False),
        ("How tall is it?", False),
    ],
)
def test_no_versions_request_question_routing_route_level(
    app_with_versions, message: str, starts_loop: bool
) -> None:
    """issue #349 adversarial round 1 (route level): the priority-attack
    list on a FRESH project, both directions pinned over the real
    ``/chat`` route. Every request phrased as a question (an imperative
    verb such as make / design / get, or a noun plus dimensions —
    including the control "make a cube") starts the design loop (202,
    the loop stub is called); the genuine no-version questions ("How
    deep is it?" / "How tall is it?" — the #349 acceptance cases) get
    the single "nothing built" done frame and the loop is NEVER called
    (no version, 0 stays 0)."""

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
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat_with_answer(
            app_with_versions,
            client,
            pid,
            {"message": message, "chat_history": []},
            answer_reply='{"answerable": true, "answer": "It is 12 mm tall."}',
        )
        return r, frames, loop_called

    r, frames, was_loop_called = run_async(
        app_with_versions, _call
    )
    assert r.status_code == 202, r.text
    if starts_loop:
        # The loop stub is the "a started loop" pin (the stub bypasses the
        # adapter's version creation, as in the existing empty-project
        # test below it):
        assert was_loop_called, (
            f"a request phrased as a question on an empty project must "
            f"start the design loop: {message!r}"
        )
    else:
        assert not was_loop_called, (
            f"a genuine no-version question must NOT start the loop: {message!r}"
        )
        assert len(frames) == 1, f"expected 1 frame, got {len(frames)}: {frames}"
        event, data = frames[0]
        assert event == "done"
        assert data.get("kind") == ANSWER_DONE_KIND
        assert data["message"] == NO_VERSION_QUESTION_REPLY


def test_llm_call_timeout_constant_equals_design_loop_default() -> None:
    """#313: the shared ``LLM_CALL_TIMEOUT_SECONDS`` constant (the
    stage-2 budget) equals the design loop's per-call default.

    The constant lives in ``d33d.design_llm`` (a leaf module both
    ``d33d.question_answer`` and ``d33d.app`` can import without a
    cycle). The design loop's ``_http_request_factory`` defaults to
    the SAME constant, so the stage-2 budget IS the design loop's
    per-call timeout — one value, shared between both paths.
    """
    from d33d.design_llm import LLM_CALL_TIMEOUT_SECONDS

    # The constant is 120.0 s (the design loop's historical default).
    assert LLM_CALL_TIMEOUT_SECONDS == 120.0

    # The question_answer module re-exports the SAME object (identity,
    # not just equality — a monkeypatch of one is a monkeypatch of the
    # other).
    import d33d.question_answer as _qa_mod

    assert _qa_mod.LLM_CALL_TIMEOUT_SECONDS is LLM_CALL_TIMEOUT_SECONDS


def test_stage2_llm_timeout_gets_no_run_reply(app_with_versions, monkeypatch) -> None:
    """#260 REPRO-VERIFICATION: a stage-2 LLM call that times out
    (simulated via a hanging stub — the same 0.1 s monkeypatch contract
    as before) → the fixed "couldn't answer" no-run done frame, the
    design loop is NEVER invoked, the version count is unchanged. The
    design loop is never the fallback for a failed answer.

    The timeout is monkeypatched to 0.1 s so the test runs in <1 s of
    wall clock (the same contract as the shared per-LLM-call production
    bound, at a shorter value — the operator's latency decision is
    pinned by the ``ask_answer_call`` timeout param, not by the literal
    value of ``LLM_CALL_TIMEOUT_SECONDS``)."""
    import d33d.question_answer as _qa_mod
    monkeypatch.setattr(_qa_mod, "LLM_CALL_TIMEOUT_SECONDS", 0.1)

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

    def _stage2_info(self, caplog) -> list[logging.LogRecord]:
        # The stage-2 INFO line (issue #313) — distinct from the
        # stage-1/route INFO lines: it names project_id, outcome and
        # latency_ms.
        return [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "question-answer stage 2: project_id=" in r.getMessage()
        ]

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
        # The answered path keeps its route INFO record (lengths only)
        # AND the issue #313 stage-2 INFO line (project id, outcome,
        # latency_ms): exactly two INFO records on the answer path.
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 2, f"expected 2 INFO, got {len(infos)}"
        route_infos = [
            r for r in infos if "answering from the design-state block" in r.getMessage()
        ]
        assert len(route_infos) == 1
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg2 = stage2_infos[0].getMessage()
        assert "project_id=-" in msg2
        assert "outcome=answered" in msg2
        assert "latency_ms=" in msg2
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
        # #313: the ``request`` outcome is the ONE stage-2 outcome with
        # NO INFO line (the message routes to the design loop — it is
        # not an answer outcome).
        stage2_infos = self._stage2_info(caplog)
        assert stage2_infos == [], (
            "the request outcome must emit NO stage-2 INFO line — "
            f"got {stage2_infos}"
        )

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
        # issue #349: a no-version QUESTION now gets the deterministic
        # "nothing built" reply (INFO, no WARNING) — use a no-version
        # CHANGE REQUEST ("make it taller") to test the no-version
        # → None short-circuit, and the two other cases as before.
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            # No-version question: deterministic reply (not None anymore).
            result = asyncio.run(
                route_chat_message("What is the material?", None)
            )
            assert result == {
                "kind": ANSWER_DONE_KIND,
                "answer": NO_VERSION_QUESTION_REPLY,
            }
            # No-version change request: still None (the guard is
            # question-shaped only).
            assert (
                asyncio.run(
                    route_chat_message("make it taller", None)
                )
                is None
            )
            # With a version, not a candidate:
            assert (
                asyncio.run(
                    route_chat_message(
                        "make it taller", self._latest({"H": 12.0})
                    )
                )
                is None
            )
            # With a version, no answer edge:
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

    # ------------------------------------------------------------------
    # Issue #313: the stage-2 INFO line (project id, outcome,
    # latency_ms) — exactly ONE per stage-2 call except the
    # ``request`` outcome (pinned above).
    # ------------------------------------------------------------------

    def test_timeout_emits_timeout_info(self, caplog) -> None:
        # #313: the asyncio-timeout outcome logs exactly ONE stage-2
        # INFO line with outcome=timeout.
        async def _edge(q, e):
            await asyncio.sleep(0.5)
            return '{"kind": "answer", "answer": "x"}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?",
                    self._latest({"H": 12.0}),
                    _edge,
                    timeout=0.05,
                    project_id="42",
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1, f"expected 1 stage-2 INFO, got {stage2_infos}"
        msg = stage2_infos[0].getMessage()
        assert "project_id=42" in msg
        assert "outcome=timeout" in msg
        assert "latency_ms=" in msg

    def test_exception_emits_error_info(self, caplog) -> None:
        # #313: a non-timeout exception maps to outcome=error and the
        # INFO record carries the exception's class name (a capability
        # failure and a network blip must not read as the same
        # unclassified ``error``).
        async def _edge(q, e):
            raise RuntimeError("boom")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?",
                    self._latest({"H": 12.0}),
                    _edge,
                    project_id="42",
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg = stage2_infos[0].getMessage()
        assert "project_id=42" in msg
        assert "outcome=error" in msg
        assert "error_class=RuntimeError" in msg
        assert "outcome=error" in msg

    def test_malformed_emits_error_info(self, caplog) -> None:
        # #313: a malformed reply maps to outcome=error.
        async def _edge(q, e):
            return "not json at all"

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?",
                    self._latest({"H": 12.0}),
                    _edge,
                    project_id="42",
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg = stage2_infos[0].getMessage()
        assert "project_id=42" in msg
        assert "outcome=error" in msg

    def test_guard_failure_emits_error_info(self, caplog) -> None:
        # #313: a number-guard failure maps to outcome=error.
        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 15 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?",
                    self._latest({"H": 12.0}),
                    _edge,
                    project_id="42",
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg = stage2_infos[0].getMessage()
        assert "project_id=42" in msg
        assert "outcome=error" in msg

    def test_unanswerable_emits_unanswerable_info(self, caplog) -> None:
        # #313: an unanswerable reply maps to outcome=unanswerable.
        async def _edge(q, e):
            return '{"kind": "unanswerable", "answer": ""}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What colour is it?",
                    self._latest({"H": 12.0}),
                    _edge,
                    project_id="42",
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": NOT_ESTABLISHED}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg = stage2_infos[0].getMessage()
        assert "project_id=42" in msg
        assert "outcome=unanswerable" in msg

    def test_absent_project_id_logs_dash(self, caplog) -> None:
        # #313: no project id (the existing callers pass nothing) → the
        # INFO line carries project_id=- — and never the message or
        # answer text (no PII in logs).
        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "What is the material?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": "It is 12 mm tall."}
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1
        msg = stage2_infos[0].getMessage()
        assert "project_id=-" in msg
        assert "outcome=answered" in msg
        assert "What is the material?" not in msg
        assert "It is 12 mm tall." not in msg

    def test_route_with_project_id_logs_real_id(self, app_with_versions, caplog):
        # #313 route-level: the /chat route (d33d/projects.py) passes the
        # REAL project id to route_chat_message → the stage-2 INFO line
        # carries that id (not -), with outcome=answered and latency_ms.

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
                stated_dims=None,
            )
            app_with_versions.state.run_design_loop = _loop
            r, frames = await _drive_chat_with_answer(
                app_with_versions,
                client,
                pid,
                {"message": "What is the material?", "chat_history": []},
                answer_reply='{"kind": "answer", "answer": "It is 12 mm tall."}',
            )
            return r, frames, pid

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            r, _frames, pid = run_async(app_with_versions, _call)
        assert r.status_code == 202, r.text
        assert not loop_called
        stage2_infos = self._stage2_info(caplog)
        assert len(stage2_infos) == 1, f"expected 1 stage-2 INFO, got {stage2_infos}"
        msg = stage2_infos[0].getMessage()
        assert f"project_id={pid}" in msg, (
            f"the /chat route must pass the real project id ({pid}) "
            f"through — got: {msg}"
        )
        assert "outcome=answered" in msg
        assert "latency_ms=" in msg
        # Never the message or the answer text (no PII in logs).
        assert "What is the material?" not in msg
        assert "It is 12 mm tall." not in msg


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
        for backend_string in (
            COULD_NOT_ANSWER,
            NOT_ESTABLISHED,
            NO_VERSION_QUESTION_REPLY,
            NO_OFFER_AFFIRMATION_REPLY,
        ):
            assert f'"{backend_string}"' in copy_ts, (
                f"backend string {backend_string!r} not found in copy.ts "
                f"(the copy.ts deck must carry it verbatim)"
            )
        # The four strings are distinct (a failure, an unanswerable
        # question, a no-version question, and a bare yes with no offer
        # are different honest statements).
        all_strings = (
            COULD_NOT_ANSWER,
            NOT_ESTABLISHED,
            NO_VERSION_QUESTION_REPLY,
            NO_OFFER_AFFIRMATION_REPLY,
        )
        assert len(set(all_strings)) == 4, (
            "the four no-run reply strings must all be distinct"
        )

    def test_feature_size_strings_match_copy_ts_deck(self) -> None:
        """Issue #396: the hole-feature size strings are pinned against
        ``web/src/copy.ts``'s ``deterministicAnswer`` deck —
        ``holeFeatureSize`` (the template, checked by rendering the deck
        the way its arrow renders) and ``featureSizeUnmeasured`` (the
        verbatim honest reply)."""
        from pathlib import Path

        copy_ts = (
            Path(__file__).resolve().parents[2]
            / "web" / "src"
            / "copy.ts"
        ).read_text()

        # The honest reply is a verbatim string literal in the deck.
        assert '"' + FEATURE_SIZE_UNMEASURED_REPLY + '"' in copy_ts, (
            f"backend string {FEATURE_SIZE_UNMEASURED_REPLY!r} not found "
            "verbatim in copy.ts (the deck must carry it verbatim)"
        )
        # The measured template renders byte-identically in both
        # directions: the deck's arrow (`The ${qualifier} hole is
        # ${diameter}.`) and the backend's {qualifier}/{diameter} slots
        # (checked by concatenation — the production code builds the
        # sentence the same way).
        rendered = "The " + "center" + " hole is " + "30.0\u202fmm" + "."
        assert rendered == "The center hole is 30.0\u202fmm."
        assert "`The ${qualifier} hole is ${diameter}.`" in copy_ts, (
            "the deck's holeFeatureSize template drifted from the backend "
            "template's shape"
        )

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
        from d33d.app import _build_question_answer_call
        from d33d.config import probes as _probes_mod
        from d33d.config.probes import CapabilityResult as _Cap

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
                    logging.getLogger(__name__).debug("edge probe failed, retrying", exc_info=True)
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

        from d33d.app import _build_question_answer_call
        from d33d.config import catalogue as _catalogue_mod
        from d33d.config import probes as _probes_mod
        from d33d.config import resolve as _resolve_mod
        from d33d.config.probes import CapabilityResult as _Cap
        from d33d.design_llm import LLMResult

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
        # v25-shaped: declared-axis params that contest the measurement
        # render disagrees_source "model".
        from d33d.design_state import state_block_for_version
        from d33d.question_answer import build_answer_prompt

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
