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
    NOT_ESTABLISHED,
    build_answer_prompt,
    guard_answer_numbers,
    is_candidate_question,
    parse_answer_reply,
    route_chat_message,
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
    """``guard_answer_numbers`` — the deterministic presence-only check."""

    def test_number_in_block_passes(self) -> None:
        entries = _entries(("H", 12.0), ("W", 20.0))
        assert guard_answer_numbers("It is 12 mm tall.", entries)

    def test_invented_number_fails(self) -> None:
        entries = _entries(("H", 12.0), ("W", 20.0))
        assert not guard_answer_numbers("It is 15 mm tall.", entries)

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


class TestParseAnswerReply:
    """``parse_answer_reply`` — the stage-2 reply codec (the #260
    three-way ``kind`` shape, plus the legacy ``answerable`` bool for
    backward compatibility)."""

    def test_valid_json_kind_answer(self) -> None:
        assert parse_answer_reply(
            '{"kind": "answer", "answer": "It is 12 mm tall."}'
        ) == ("answer", "It is 12 mm tall.")

    def test_kind_unanswerable(self) -> None:
        assert parse_answer_reply(
            '{"kind": "unanswerable", "answer": ""}'
        ) == ("unanswerable", "")

    def test_kind_request(self) -> None:
        assert parse_answer_reply(
            '{"kind": "request", "answer": ""}'
        ) == ("request", "")

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
        ) == ("answer", "It is 12 mm tall.")

    def test_legacy_answerable_false_maps_to_request(self) -> None:
        # Preserving today's routing: a legacy false reply is a change
        # REQUEST to the design loop, never a false "unanswerable".
        assert parse_answer_reply(
            '{"answerable": false, "answer": ""}'
        ) == ("request", "")

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
        ) == ("answer", "12 mm")
        assert parse_answer_reply(
            'Sure: {"kind": "request", "answer": ""}'
        ) == ("request", "")


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
        assert "colour, material or finish" in prompt
        assert "\"Can it be 20 mm wider?\"" in prompt

    def test_prompt_no_longer_speaks_the_answerable_bool(self) -> None:
        # The legacy boolean schema is retired from the production prompt
        # (parsing still accepts it for old-style model replies).
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert '"answerable"' not in prompt
        assert "answerable: true" not in prompt
        assert "answerable: false" not in prompt


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
        latest = _latest({"H": 12.0, "W": 20.0})
        edge = self._answer_edge('{"answerable": true, "answer": "It is 12 mm tall."}')
        result = run_async_safe(route_chat_message("How tall is it now?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": "It is 12 mm tall."}

    def test_no_versions_returns_none(self) -> None:
        result = run_async_safe(route_chat_message("How tall is it now?", None))
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
        # reply, not a design run.
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"answerable": true, "answer": "It is 15 mm tall."}')
        result = run_async_safe(route_chat_message("How tall is it now?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_malformed_reply_returns_could_not_answer(self) -> None:
        latest = _latest({"H": 12.0})
        edge = self._answer_edge("not json at all")
        result = run_async_safe(route_chat_message("How tall is it now?", latest, edge))
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_exception_from_edge_returns_could_not_answer(self) -> None:
        latest = _latest({"H": 12.0})

        async def _raise_edge(question: str, entries: list) -> str:
            raise RuntimeError("simulated stage-2 failure")

        result = run_async_safe(
            route_chat_message("How tall is it now?", latest, _raise_edge)
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
                "How tall is it now?", latest, _hanging_edge, timeout=0.05
            )
        )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}

    def test_no_answer_edge_returns_none(self) -> None:
        latest = _latest({"H": 12.0})
        result = run_async_safe(route_chat_message("How tall is it now?", latest))
        assert result is None


def run_async_safe(coro) -> Any:
    """Drive a coroutine under a fresh event loop (no app, no lifespan)."""
    return asyncio.run(coro)


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
    """DECISIVE (issue #249): "How tall is it now?" against a project
    whose latest version has H stated 12 → answered via ONE terminal
    done frame (``kind: "answer"``), NO version created, NO
    version-created frame, NO token frame. The design loop is never
    called."""

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
            {"message": "How tall is it now?", "chat_history": []},
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


def test_invented_number_guard_failure_gets_no_run_reply_not_loop(
    app_with_versions,
) -> None:
    """#260: an LLM answer containing a number not in the block → guard
    fails → the fixed no-run copy. No design run, no version — the
    design loop is never the fallback for a failed answer."""

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
            {"message": "How tall is it now?", "chat_history": []},
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


def test_no_versions_goes_to_loop(app_with_versions) -> None:
    """A fresh project (no versions) receiving "How tall is it now?" →
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
            {"message": "How tall is it now?", "chat_history": []},
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
            json={"message": "How tall is it now?", "chat_history": []},
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
            {"message": "How tall is it now?", "chat_history": []},
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
            json={"message": "How tall is it now?", "chat_history": []},
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
        async def _edge(q, e):
            return '{"kind": "answer", "answer": "It is 12 mm tall."}'

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
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
            assert "How tall is it now?" not in record.getMessage()
            assert "It is 12 mm tall." not in record.getMessage()
        # The answered path also keeps its INFO record (lengths only).
        infos = [r for r in caplog.records if r.levelno == logging.INFO]
        assert len(infos) == 1, f"expected 1 INFO, got {len(infos)}"
        assert "answering from the design-state block" in infos[0].getMessage()
        for record in infos:
            assert "How tall is it now?" not in record.getMessage()
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
                    "How tall is it now?", self._latest({"H": 12.0}), _edge,
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
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
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
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
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
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
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
        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            assert asyncio.run(route_chat_message("How tall is it now?", None)) is None
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
                        "How tall is it now?", self._latest({"H": 12.0})
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
        cleanup)."""

        async def _canceling_edge(q, e):
            raise asyncio.CancelledError()

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            coro = route_chat_message(
                "How tall is it now?", self._latest({"H": 12.0}), _canceling_edge
            )
            with pytest.raises(asyncio.CancelledError):
                asyncio.run(coro)
        # Cancellation is NOT a stage-2 outcome: no outcome= record at all.
        assert self._outcome_warning(caplog) == []

    def test_httpx_timeout_emits_timeout_warning(self, caplog) -> None:
        """An ``httpx.TimeoutException`` (the per-request client bound in
        ``_http_request_factory``) is a ``timeout`` outcome, not an
        ``exception`` — the classification is by exception class, never
        by elapsed time."""
        import httpx

        async def _edge(q, e):
            raise httpx.ReadTimeout("read timed out")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
                )
            )
        assert result == {"kind": ANSWER_DONE_KIND, "answer": COULD_NOT_ANSWER}
        warnings = self._outcome_warning(caplog)
        assert len(warnings) == 1, f"expected 1 WARNING, got {len(warnings)}"
        assert "outcome=timeout" in warnings[0].getMessage()

    def test_httpx_non_timeout_emits_exception_warning(self, caplog) -> None:
        """An ``httpx`` error that is NOT a timeout (e.g. a connection
        reset) is an ``exception``, not a ``timeout`` — the
        httpx.TimeoutException check is specific to the timeout class."""
        import httpx

        async def _edge(q, e):
            raise httpx.ConnectError("connection reset")

        with caplog.at_level(logging.INFO, "d33d.question_answer"):
            result = asyncio.run(
                route_chat_message(
                    "How tall is it now?", self._latest({"H": 12.0}), _edge
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
                json={"message": "How tall is it now?", "chat_history": []},
            )
            await asyncio.sleep(0.3)
            second = client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "How tall is it now?", "chat_history": []},
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
            r_fail = await client.post(
                f"/api/projects/{pid}/chat",
                json={"message": "How tall is it now?", "chat_history": []},
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

        # The value-integrity contract: both forbid inventing values and
        # require every number to come from the block.
        assert "must come from the block" in prompt
        assert "Never invent" in evals_md
        # The provenance citations: the same five provenance phrases.
        for phrase in (
            "you said that",
            "I measured",
            "I assumed",
            "not established",
            "you said X, I measured Y",
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
        """Issue #264 ACCEPTANCE: the router's answer prompt distinguishes
        the two kinds of disagreement. A block that carries a
        model-source disagrees row (``disagrees_source == "model"`` —
        the v25 Shelf-spacer shape) gets the "I set X, it measures Y"
        instruction — never 'you said' for a value the user never
        stated; a block with only a user-source disagrees row keeps
        today's "you said X, I measured Y" instruction. The block text
        rendered in the prompt also marks the model-source row
        "(my value differs from the measurement)" so the model can tell
        the rows apart."""
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
        # (``disagrees_source`` absent) keeps today's instruction and
        # renders the plain disagrees row — no model wording anywhere.
        user_entries = state_block_for_version(
            {"W": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0}, None
        )
        assert all(
            "disagrees_source" not in e for e in user_entries
        ), user_entries
        user_prompt = build_answer_prompt("How wide is it?", user_entries)
        assert "you said X, I measured Y" in user_prompt
        assert "I set X, it measures Y" not in user_prompt
        assert "my value differs" not in user_prompt
