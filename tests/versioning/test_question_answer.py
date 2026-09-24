"""Issue #249: the chat pre-route — "can the Brief answer this question?"

A question in the chat ("How tall is it now?") used to fall straight into
the design loop, which produced a wrong new version that discarded the
current part and named it after the question ("v22 — how tall is it now").
The pre-route sits in the /chat path BEFORE the design loop: if the
message is a question AND the project's design state can answer it, the
answer is emitted on the chat stream as an assistant message — no design
run, no render, no version, no filmstrip entry. Everything else, including
anything ambiguous, goes to the design loop exactly as today.

This file covers the Python side of the seam (task-a scope):

* stage 1 — the deterministic question detector (no LLM, no app);
* stage 2 — the cheap single LLM call + the number guard (stub answer_fn);
* the /chat route wiring — the answer path emits ONE terminal done frame
  (``kind: "answer"``) and NO version is created; the design-loop path
  is unchanged for every non-answer message.

All fast (no LLM, no Docker).
"""

from __future__ import annotations

import asyncio
from typing import Any

from d33d.question_answer import (
    ANSWER_DONE_KIND,
    build_answer_prompt,
    extract_answer_numbers,
    guard_answer_numbers,
    is_candidate_question,
    parse_answer_reply,
    route_chat_message,
    state_block_for_chat,
    state_block_numbers,
)
from tests.seam_schemas_d import validate_frame, validate_frames_stream
from tests.versioning.helpers import create_project, create_version, run_async


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
    """``parse_answer_reply`` — the stage-2 reply codec."""

    def test_valid_json(self) -> None:
        assert parse_answer_reply(
            '{"answerable": true, "answer": "It is 12 mm tall."}'
        ) == (True, "It is 12 mm tall.")

    def test_answerable_false(self) -> None:
        assert parse_answer_reply(
            '{"answerable": false, "answer": ""}'
        ) == (False, "")

    def test_malformed_returns_none(self) -> None:
        assert parse_answer_reply("not json") is None
        assert parse_answer_reply("") is None
        assert parse_answer_reply('{"answerable": "yes"}') is None
        assert parse_answer_reply('{"answerable": true}') is None  # no answer key

    def test_json_in_surrounding_prose(self) -> None:
        assert parse_answer_reply(
            'Here is the answer: {"answerable": true, "answer": "12 mm"}'
        ) == (True, "12 mm")


# ---------------------------------------------------------------------------
# Stage 2 — the prompt (the block must be present; no confirmation offer)
# ---------------------------------------------------------------------------


class TestBuildAnswerPrompt:
    """``build_answer_prompt`` — the stage-2 user message."""

    def test_prompt_contains_block_values(self) -> None:
        entries = _entries(("H", 12.0), ("W", 20.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        assert "12" in prompt
        assert "20" in prompt
        assert "How tall is it?" in prompt

    def test_prompt_forbids_offering_to_set(self) -> None:
        entries = _entries(("H", 12.0))
        prompt = build_answer_prompt("How tall is it?", entries)
        # The operator's decision: the prompt must NOT ask the model to
        # offer to set/confirm values.
        assert "do NOT offer to change, set, or confirm" in prompt


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

    def test_unanswerable_returns_none(self) -> None:
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"answerable": false, "answer": ""}')
        result = run_async_safe(route_chat_message("What colour is it?", latest, edge))
        assert result is None

    def test_invented_number_guard_fails(self) -> None:
        latest = _latest({"H": 12.0})
        edge = self._answer_edge('{"answerable": true, "answer": "It is 15 mm tall."}')
        result = run_async_safe(route_chat_message("How tall is it now?", latest, edge))
        assert result is None

    def test_malformed_reply_returns_none(self) -> None:
        latest = _latest({"H": 12.0})
        edge = self._answer_edge("not json at all")
        result = run_async_safe(route_chat_message("How tall is it now?", latest, edge))
        assert result is None

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


def test_unanswerable_question_goes_to_loop(app_with_versions) -> None:
    """A question the block cannot answer ("What colour is it?") →
    answerable: false → design loop, not the answer path."""

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
            answer_reply='{"answerable": false, "answer": ""}',
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called for an unanswerable question"


def test_invented_number_goes_to_loop(app_with_versions) -> None:
    """An LLM answer containing a number not in the block → guard fails
    → design loop, not the answer path."""

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
            answer_reply='{"answerable": true, "answer": "It is 15 mm tall."}',
        )
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called for an invented-number answer"


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


def test_stage2_llm_timeout_goes_to_loop(app_with_versions, monkeypatch) -> None:
    """A stage-2 LLM call that times out (simulated via a hanging stub)
    → design loop, exactly as today. The timeout is monkeypatched to 0.1 s
    so the test runs in <1 s of wall clock (the same contract as the 10 s
    production bound, at a shorter value — the operator's latency decision
    is pinned by the ``ask_answer_call`` timeout param, not by the literal
    value of ``ANSWER_CALL_TIMEOUT_SECONDS``)."""
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
        return r, frames

    r, frames = run_async(app_with_versions, _call)
    assert r.status_code == 202, r.text
    assert loop_called, "the design loop was NOT called after a stage-2 timeout"
    # The terminal frame is from the design loop (no kind "answer").
    terminal = frames[-1]
    assert terminal[1].get("kind") != ANSWER_DONE_KIND


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
        # A stub design loop (so the design-loop path doesn't try to load
        # a real catalogue — the answer path calls _capturing_edge, which
        # returns answerable:false → the design loop is invoked).
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
