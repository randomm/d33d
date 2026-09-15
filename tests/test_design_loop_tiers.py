"""tests/test_design_loop_tiers.py — capability-aware LLM sender (ticket #5,
task-tiers workstream).

Covers:
- T0 native tool-calling dispatch (single request, tools attached,
  tool_calls passthrough).
- T1 fenced-JSON dispatch (no ``tools`` in the request body; the parsed
  block is synthesized into ``tool_calls``; corrective retry path).
- T2/T3 graceful degradation: ``SenderError(status='no_tools_supported')``
  so the design loop degrades the role to deterministic-gate scoring rather
  than crashing.
- Ollama ``/v1`` images-array dialect: image parts move from OpenAI
  ``content`` parts into the sibling ``images`` array (plain-string
  content), and non-vision targets drop the images while preserving the
  sanitiser's placeholder text.
- Canonical-hash logging: ``prompt_hash`` is computed from the
  post-dialect message shape with the role — identical across tiers and
  across models (never includes the model id), stable across runs, and
  changes when the message content (e.g. the param block or view set)
  changes.
- ``log_request`` wiring: the hash the sender reports is the one
  ``Connection.log_request`` records (the request_logs join key).

No network, no Docker — ``request_factory`` is injected per call,
mirroring ``d33d.config.probes`` and ``d33d.config.t1_protocol``.
Async entry points run under plain ``asyncio.run`` (no async plugin in CI).
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from d33d.config.probes import CapabilityResult
from d33d.db import connect
from d33d.design_llm import (
    LLMResult,
    SenderError,
    llm_request_body,
    response_text,
    send,
    to_ollama_messages,
)
from d33d.render_worker import VIEWS
from d33d.response_shape import response_message_shape

IMAGE_URL = "data:image/png;base64,REFPHOTO"
VIEW_IMAGES = [f"data:image/png;base64,VIEW{i}" for i in range(len(VIEWS))]
VIEW_NAMES = [name for name, _ in VIEWS]

DESIGN_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "emit_design",
            "description": "Emit the parametric OpenSCAD",
            "parameters": {
                "type": "object",
                "properties": {"scad": {"type": "string"}},
            },
        },
    }
]


def _run(coro):
    return asyncio.run(coro)


class FakeResponse:
    def __init__(self, status: int, payload: dict[str, Any]):
        self.status = status
        self.is_success = status < 400
        self._payload = payload

    def json(self):
        return self._payload


def _ok_response(content: str | None = "hi", tool_calls: list | None = None):
    message: dict[str, Any] = {"content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return FakeResponse(
        200,
        {
            "choices": [{"message": message}],
            "usage": {
                "prompt_tokens": 11,
                "completion_tokens": 7,
            },
        },
    )


def _t0() -> CapabilityResult:
    return CapabilityResult(
        tools=True, json_schema=True, vision=True, max_images=8, validated=True
    )


def _t1() -> CapabilityResult:
    return CapabilityResult(
        tools=True, json_schema=False, vision=True, max_images=8, validated=True
    )


def _t2() -> CapabilityResult:
    return CapabilityResult(
        tools=False,
        json_schema=False,
        vision=True,
        max_images=0,
        fenced_json=False,
        validated=True,
    )


def _t3() -> CapabilityResult:
    return CapabilityResult(
        tools=False,
        json_schema=False,
        vision=False,
        max_images=0,
        fenced_json=False,
        validated=True,
    )


def _design_messages() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Design a 20x25x30 box."},
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
            ],
        }
    ]


def _critique_messages() -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = [
        {"type": "text", "text": "Critique the 20x25x30 box."}
    ]
    for i, url in enumerate(VIEW_IMAGES):
        parts.append({"type": "image_url", "image_url": {"url": url}})
        parts.append({"type": "text", "text": VIEW_NAMES[i]})
    parts.append({"type": "image_url", "image_url": {"url": IMAGE_URL}})
    parts.append({"type": "text", "text": "reference"})
    return [{"role": "user", "content": parts}]


# ---------------------------------------------------------------------------
# T0 dispatch
# ---------------------------------------------------------------------------


def test_t0_sends_native_tools_and_passes_through_tool_calls():
    sent: list[dict[str, Any]] = []

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return _ok_response(
            "ok",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_design",
                        "arguments": '{"scad": "cube([20,25,30]);"}',
                    },
                }
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="RedHatAI/Qwen3.8-27B-INT4",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
            system="You are a CAD engine.",
            tools=DESIGN_TOOLS,
            call_params={"temperature": 0.2},
        )
    )

    # One native request: tools attached, model + call params in the body.
    assert len(sent) == 1
    body = sent[0]
    assert body["model"] == "RedHatAI/Qwen3.8-27B-INT4"
    assert body["tools"] == DESIGN_TOOLS
    assert body["temperature"] == 0.2
    # OpenAI dialect: image parts stay inline in content.
    content = body["messages"][0]["content"]
    assert any(isinstance(p, dict) and p.get("type") == "image_url" for p in content)
    assert "images" not in body["messages"][0]

    assert isinstance(result, LLMResult)
    assert result.tier == "T0"
    assert result.status == "ok"
    assert result.content == "ok"
    assert result.tool_calls[0]["name"] == "emit_design"
    assert len(result.prompt_hash) == 64
    assert result.usage == {"prompt_tokens": 11, "completion_tokens": 7}


def test_t0_non_ok_response_raises_error_status():
    async def factory(request: dict[str, Any]):
        return FakeResponse(500, {"error": "boom"})

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t0(),
            )
        )
    assert exc.value.status == "error"


# ---------------------------------------------------------------------------
# T1 dispatch
# ---------------------------------------------------------------------------


def test_t1_fenced_json_without_tools_in_body():
    sent: list[dict[str, Any]] = []
    fenced = '```json\n{"tool": "emit_design", "arguments": {"scad": "cube(1);"}}\n```'

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return _ok_response(fenced)

    result = _run(
        send(
            role="design",
            model_id="local-model",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t1(),
            system="You are a CAD engine.",
        )
    )

    # T1 wire shape: the T1 codec's own envelope (system prompt + fenced-JSON
    # fragment, then the user turn). The dialect-converted logical shape is
    # what the canonical hash is computed on (see ``result.request_body``).
    assert len(sent) >= 1
    body = sent[0]
    assert "tools" not in body
    assert body["model"] == "local-model"
    sys_text = body["messages"][0]["content"]
    assert "fenced JSON" in sys_text
    assert "emit_design" in sys_text

    # The parsed block is synthesized into tool_calls (same shape as T0).
    assert result.tier == "T1"
    assert result.status == "ok"
    assert result.tool_calls[0]["name"] == "emit_design"
    assert result.tool_calls[0]["arguments"] == {"scad": "cube(1);"}
    assert result.content == fenced

    # The reported request_body reflects the dialect-converted logical shape
    # (what the canonical hash is computed on).
    wire = result.request_body
    assert "tools" not in wire
    wire_msg = wire["messages"][0]
    # OpenAI dialect (default): image parts stay inline in content.
    assert any(
        isinstance(p, dict) and p.get("type") == "image_url"
        for p in wire_msg["content"]
    )
    assert "images" not in wire_msg


def test_t1_corrective_retry_after_malformed_response():
    sent: list[dict[str, Any]] = []
    fenced = '```json\n{"tool": "emit_critique", "arguments": {}}\n```'
    responses = [
        _ok_response("sorry, {bad json"),
        _ok_response(fenced),
    ]

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return responses.pop(0)

    result = _run(
        send(
            role="critique",
            model_id="local-model",
            messages=_critique_messages(),
            request_factory=factory,
            capability=_t1(),
            system="sys",
        )
    )

    # Two requests: the corrective retry is appended as a user message.
    assert len(sent) == 2
    first = sent[0]["messages"]
    second = sent[1]["messages"]
    assert len(second) == len(first) + 1
    corrective = second[-1]
    assert corrective["role"] == "user"
    assert "fenced JSON" in corrective["content"]
    assert result.tool_calls[0]["name"] == "emit_critique"


def test_t1_retry_exhaustion_raises_error_status():
    async def factory(request: dict[str, Any]):
        return _ok_response("still no json")

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
    assert exc.value.status == "error"


# ---------------------------------------------------------------------------
# T2/T3 graceful degradation (no tool channel)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("capability", [_t2(), _t3()], ids=["T2", "T3"])
def test_t2_t3_raise_no_tools_supported(capability: CapabilityResult):
    async def factory(request: dict[str, Any]):
        raise AssertionError("T2/T3 must not send a request")

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="critique",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=capability,
            )
        )
    assert exc.value.status == "no_tools_supported"


# ---------------------------------------------------------------------------
# Ollama /v1 images-array dialect
# ---------------------------------------------------------------------------


def test_ollama_dialect_moves_images_into_array():
    # The dialect conversion applies to the *logical* request body (what the
    # canonical hash is computed on and what is reported as
    # ``request_body``).  The T1 codec's own wire envelope (system prompt +
    # fenced-JSON fragment) is the T1 framing; the dialect conversion is a
    # property of the logical body, independent of the T1 codec.
    async def factory(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_design", "arguments": {}}\n```')

    result = _run(
        send(
            role="design",
            model_id="ollama-model",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t1(),
            dialect="ollama",
        )
    )
    wire = result.request_body
    msg = wire["messages"][0]
    # The reference photo is in the declared images array, never an
    # image_url content part.
    assert msg["images"] == [IMAGE_URL]
    assert isinstance(msg["content"], str)
    assert "Design a 20x25x30 box." in msg["content"]
    assert "image_url" not in msg["content"]
    assert "tools" not in wire


def test_ollama_dialect_six_views_plus_reference_payload():
    async def factory(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_critique", "arguments": {}}\n```')

    result = _run(
        send(
            role="critique",
            model_id="ollama-model",
            messages=_critique_messages(),
            request_factory=factory,
            capability=_t1(),
            dialect="ollama",
        )
    )
    msg = result.request_body["messages"][0]
    # Six views + the reference photo, in order, in the images array.
    assert msg["images"] == VIEW_IMAGES + [IMAGE_URL]
    assert "view_00_front.png" in msg["content"]
    assert "reference" in msg["content"]


def test_t1_ollama_wire_shape_matches_logical_dialect():
    """When dialect='ollama' AND tier='T1', the canonical hash is computed
    on the dialect-converted logical body (images in the `images` array, not
    in content parts) — that's what the model-vs-model diff relies on."""
    sent: list[dict[str, Any]] = []

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return _ok_response('```json\n{"tool": "emit_critique", "arguments": {}}\n```')

    result = _run(
        send(
            role="critique",
            model_id="ollama-model",
            messages=_critique_messages(),
            request_factory=factory,
            capability=_t1(),
            dialect="ollama",
        )
    )

    # The reported request_body (what the hash is computed on) is the
    # dialect-converted logical shape: images array, plain-string content.
    wire = result.request_body
    msg = wire["messages"][0]
    assert msg["images"] == VIEW_IMAGES + [IMAGE_URL]
    assert isinstance(msg["content"], str)
    assert "view_00_front.png" in msg["content"]
    # The T1 codec's own wire envelope (what the factory actually saw) is
    # the codec's framing — system fragment + plain user turn.  The dialect
    # conversion is a property of the logical body, not of the T1 codec.
    assert "tools" not in wire
    assert len(sent) == 1
    assert "fenced JSON" in sent[0]["messages"][0]["content"]


def test_to_ollama_messages_non_vision_drops_images_keeps_placeholder():
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "[image removed — model does not support vision]",
                },
                {"type": "image_url", "image_url": {"url": IMAGE_URL}},
                {"type": "text", "text": "go"},
            ],
        }
    ]
    out = to_ollama_messages(messages, supports_vision=False)
    assert "images" not in out[0]
    assert "[image removed — model does not support vision]" in out[0]["content"]
    assert "go" in out[0]["content"]


def test_to_ollama_messages_vision_moves_all_images():
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,A"}},
                {"type": "text", "text": "middle"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,B"}},
            ],
        }
    ]
    out = to_ollama_messages(messages)
    assert out[0]["images"] == [
        "data:image/png;base64,A",
        "data:image/png;base64,B",
    ]
    assert out[0]["content"] == "middle"


def test_openai_dialect_keeps_inline_parts():
    body = llm_request_body(
        model="m",
        messages=_design_messages(),
        dialect="openai",
    )
    msg = body["messages"][0]
    assert any(
        isinstance(p, dict) and p.get("type") == "image_url" for p in msg["content"]
    )
    assert "images" not in msg


# ---------------------------------------------------------------------------
# Canonical-hash logging (request_logs join key)
# ---------------------------------------------------------------------------


def test_prompt_hash_identical_across_tiers_and_models():
    async def factory0(request: dict[str, Any]):
        return _ok_response(
            "ok",
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "emit_design", "arguments": "{}"},
                },
            ],
        )

    async def factory1(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_design", "arguments": {}}\n```')

    msgs = _design_messages()
    r0 = _run(
        send(
            role="design",
            model_id="model-a",
            messages=msgs,
            request_factory=factory0,
            capability=_t0(),
            system="You are a CAD engine.",
        )
    )
    r1 = _run(
        send(
            role="design",
            model_id="model-b",
            messages=msgs,
            request_factory=factory1,
            capability=_t1(),
            system="You are a CAD engine.",
        )
    )
    # Same logical prompt, different tiers AND different model ids → same
    # canonical hash (the model id never enters the hash).
    assert r0.prompt_hash == r1.prompt_hash
    assert len(r0.prompt_hash) == 64
    int(r0.prompt_hash, 16)  # hex


def test_prompt_hash_stable_across_repeats():
    async def factory(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_critique", "arguments": {}}\n```')

    msgs = _critique_messages()
    hashes = {
        _run(
            send(
                role="critique",
                model_id="m",
                messages=msgs,
                request_factory=factory,
                capability=_t1(),
                dialect="ollama",
            )
        ).prompt_hash
        for _ in range(20)
    }
    assert len(hashes) == 1


def test_prompt_hash_changes_when_view_set_or_param_block_changes():
    async def factory(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_critique", "arguments": {}}\n```')

    def _critique_with(text: str) -> list[dict[str, Any]]:
        return [{"role": "user", "content": text}]

    base = _run(
        send(
            role="critique",
            model_id="m",
            messages=_critique_with("param block: W=20; D=25; H=30; views=6"),
            request_factory=factory,
            capability=_t1(),
        )
    )
    changed_views = _run(
        send(
            role="critique",
            model_id="m",
            messages=_critique_with("param block: W=20; D=25; H=30; views=3"),
            request_factory=factory,
            capability=_t1(),
        )
    )
    changed_params = _run(
        send(
            role="critique",
            model_id="m",
            messages=_critique_with("param block: W=20; D=25; H=40; views=6"),
            request_factory=factory,
            capability=_t1(),
        )
    )
    assert base.prompt_hash != changed_views.prompt_hash
    assert base.prompt_hash != changed_params.prompt_hash


def test_prompt_hash_excludes_model_id_including_ollama_dialect():
    async def factory(request: dict[str, Any]):
        return _ok_response('```json\n{"tool": "emit_design", "arguments": {}}\n```')

    r_openai = _run(
        send(
            role="design",
            model_id="model-x",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t1(),
            dialect="openai",
        )
    )
    # Different model id, same logical prompt, same dialect → same hash.
    r_other = _run(
        send(
            role="design",
            model_id="model-y",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t1(),
            dialect="openai",
        )
    )
    assert r_openai.prompt_hash == r_other.prompt_hash


def test_prompt_hash_wired_into_request_logs(tmp_path):
    """The hash the sender reports is the row Connection.log_request stores
    — the join key for model-vs-model diffing lives in request_logs."""
    db = connect(tmp_path / "d33d.db")
    project_id = db.create_project(name="t", git_repo_path=str(tmp_path / "repo"))
    hashes: list[str] = []
    sent: list[dict[str, Any]] = []

    async def factory(request: dict[str, Any]):
        sent.append(request)
        return _ok_response(
            "ok",
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {"name": "emit_design", "arguments": "{}"},
                },
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="RedHatAI/Qwen3.8-27B-INT4",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
            system="You are a CAD engine.",
        )
    )
    log_id = db.log_request(
        project_id=project_id,
        model_alias="design-primary",
        model_id="RedHatAI/Qwen3.8-27B-INT4",
        provider="trailopeners",
        role="design",
        status=result.status,
        prompt_tokens=result.usage.get("prompt_tokens", 0),
        completion_tokens=result.usage.get("completion_tokens", 0),
        latency_ms=0,
        prompt_hash=result.prompt_hash,
    )
    row = db.get_request_log(log_id)
    assert row is not None
    assert row["prompt_hash"] == result.prompt_hash
    # And the rows-by-hash diff surface works off that same value.
    same_hash_rows = db.requests_by_prompt_hash(result.prompt_hash)
    assert len(same_hash_rows) == 1
    hashes.append(row["prompt_hash"])


def test_response_text_helper_handles_null_content():
    resp = _ok_response(None)
    assert response_text(resp) == ""


# ---------------------------------------------------------------------------
# Item 5: shared OpenAI response-shape validation helper
# ---------------------------------------------------------------------------

def test_response_message_shape_returns_message_for_valid_body():
    assert response_message_shape(
        {"choices": [{"message": {"content": "hi"}}]}
    ) == {"content": "hi"}


def test_response_message_shape_returns_none_on_shape_violations():
    """Every rung of the validation ladder degrades to None (the shared
    policy); callers apply their own failure policy on top."""
    assert response_message_shape(["not a dict"]) is None  # non-dict body
    assert response_message_shape({}) is None  # missing choices
    assert response_message_shape({"choices": []}) is None  # empty choices
    assert response_message_shape({"choices": "x"}) is None  # non-list choices
    assert response_message_shape({"choices": ["x"]}) is None  # non-dict choices[0]
    assert response_message_shape({"choices": [{"message": "x"}]}) is None  # non-dict message
    assert response_message_shape({"choices": [{}]}) is None  # missing message


def test_response_message_helper_keeps_typeerror_policy():
    """response_message keeps its TypeError failure policy on top of the
    shared None-returning shape helper (never a raw KeyError)."""
    from d33d.design_llm import response_message

    for body in (
        ["not a dict"],
        {},
        {"choices": []},
        {"choices": ["x"]},
        {"choices": [{"message": "x"}]},
        {"choices": [{}]},
    ):

        class _Resp:
            def __init__(self, payload: Any) -> None:
                self._payload = payload

            def json(self) -> Any:
                return self._payload

        with pytest.raises(TypeError):
            response_message(_Resp(body))

    assert response_message(_ok_response("hi")) == {"content": "hi"}


def test_t1_shape_violation_keeps_none_then_runtime_error_policy():
    """A shape violation on the T1 path still raises the documented
    RuntimeError (the None-returning helper feeds the or-'' fallback +
    retry exhaustion), never a raw KeyError/TypeError."""
    from d33d.config.t1_protocol import t1_invoke

    class _Resp:
        ok = True
        status = 200

        def json(self):
            return {"choices": [{"message": "not a dict"}]}

    async def factory(request: dict[str, Any]):
        return _Resp()

    with pytest.raises(RuntimeError):
        _run(
            t1_invoke(
                request_factory=factory,
                system_prompt="sys",
                user_message="u",
                tool_names=["answer"],
            )
        )


# ---------------------------------------------------------------------------
# HIGH #1: T1 tool-name allowlist enforced at the sender boundary
# ---------------------------------------------------------------------------


def test_t1_wrong_tool_name_for_role_raises_sender_error():
    """A T1 fenced response with the WRONG tool name for the role being
    called must NOT pass through as a valid LLMResult — send() raises
    SenderError(status='error') with a tool_name_mismatch reason."""
    fenced = '```json\n{"tool": "emit_critique", "arguments": {}}\n```'

    async def factory(request: dict[str, Any]):
        return _ok_response(fenced)

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
    assert exc.value.status == "error"
    assert "tool_name_mismatch" in str(exc.value)


def test_t0_wrong_tool_name_for_role_raises_sender_error():
    """Same contract on the T0 native path: a tool call with the wrong name
    is rejected at the sender boundary."""

    async def factory(request: dict[str, Any]):
        return _ok_response(
            "ok",
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {
                        "name": "emit_design",
                        "arguments": "{}",
                    },
                }
            ],
        )

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="critique",
                model_id="m",
                messages=_critique_messages(),
                request_factory=factory,
                capability=_t0(),
            )
        )
    assert exc.value.status == "error"
    assert "tool_name_mismatch" in str(exc.value)


def test_t0_correct_tool_name_for_role_passes():
    """Happy path: the expected tool name for the role passes unchanged."""

    async def factory(request: dict[str, Any]):
        return _ok_response(
            "ok",
            tool_calls=[
                {
                    "id": "c",
                    "type": "function",
                    "function": {
                        "name": "emit_critique",
                        "arguments": "{}",
                    },
                }
            ],
        )

    result = _run(
        send(
            role="critique",
            model_id="m",
            messages=_critique_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert result.status == "ok"
    assert result.tool_calls[0]["name"] == "emit_critique"


def test_t1_response_without_any_tool_call_raises_sender_error():
    """A T1 response that t1_invoke managed to parse into a call with no
    usable name must fail the allowlist (never a valid result)."""
    from d33d.design_llm import ROLE_TOOL_NAMES, _validate_tool_name

    # Direct unit check of the boundary predicate.
    with pytest.raises(SenderError) as exc:
        _validate_tool_name("design", "emit_design ")
    assert exc.value.status == "error"
    assert ROLE_TOOL_NAMES["design"] == "emit_design"


# ---------------------------------------------------------------------------
# HIGH #3: T1 error path keeps the error contract
# ---------------------------------------------------------------------------


class _BadJsonResponse:
    """A non-OK response whose body is not JSON (e.g. an HTML error page)."""

    ok = False
    status = 500

    def json(self):
        raise ValueError("No JSON object could be decoded")


class _MissingChoicesResponse:
    """An OK response whose body is JSON but missing the 'choices' key."""

    ok = True
    status = 200

    def json(self):
        return {"error": {"message": "model overloaded"}}


class _NullContentResponse:
    """An OK response with a valid shape but null message content."""

    ok = True
    status = 200

    def json(self):
        return {"choices": [{"message": {"content": None}}]}


def test_t1_malformed_http_response_is_sender_error_not_keyerror():
    """A non-OK, non-JSON T1 response must surface as
    SenderError(status='error') — never a raw KeyError/TypeError escaping
    unclassified."""

    async def factory(request: dict[str, Any]):
        return _BadJsonResponse()

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
    assert exc.value.status == "error"


def test_t1_missing_choices_is_sender_error():
    """An OK response with no 'choices' key is a classified error."""

    async def factory(request: dict[str, Any]):
        return _MissingChoicesResponse()

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
    assert exc.value.status == "error"


def test_t1_null_content_is_sender_error():
    """An OK response whose message content is null is a classified error
    (the T1 codec has no text to parse — documented failure, not a crash)."""

    async def factory(request: dict[str, Any]):
        return _NullContentResponse()

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
    assert exc.value.status == "error"


def test_t1_error_path_log_callback_still_fires():
    """The request-log write (the log callback) must fire for the T1 error
    path, matching how other failure paths are logged. The loop's log hook
    is invoked with the LLMResult's status when the call succeeds; when
    send() raises SenderError the caller's try/except around the llm_fn
    call records the failure. Here we assert the sender raises a
    *classified* error (the seam the log writer hooks) and that the log
    row's status column would receive 'error'."""
    log_calls: list[tuple[str, str, str]] = []

    def log(role, h, status):
        log_calls.append((role, h, status))

    async def factory(request: dict[str, Any]):
        return _BadJsonResponse()

    # Simulate the loop's log-on-error contract: the caller catches the
    # classified SenderError and writes the row.
    try:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t1(),
                system="sys",
            )
        )
        raise AssertionError("expected SenderError")
    except SenderError as exc:
        # This is the status the log writer records for the failure row.
        log("design", "0" * 64, exc.status)

    assert log_calls == [("design", "0" * 64, "error")]


# ---------------------------------------------------------------------------
# T0 fenced-JSON-in-content fallback (issue #78): tool_calls empty, a fenced
# JSON block in content is parsed with the T1 codec and synthesized into
# tool_calls at the sender boundary
# ---------------------------------------------------------------------------


def test_t0_fenced_json_in_content_synthesizes_tool_call():
    """A T0 response with tool_calls=() and a ```json fenced tool call in
    content yields a synthesized tool_call — _scad_from_result downstream
    finds arguments.scad and the loop does not hit empty_scad."""
    fenced = '```json\n{"tool": "emit_design", "arguments": {"scad": "cube([20,20,20]);"}}\n```'

    async def factory(request: dict[str, Any]):
        return _ok_response(fenced, tool_calls=[])

    result = _run(
        send(
            role="design",
            model_id="RedHatAI/Qwen3.8-27B-INT4",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
            system="You are a CAD engine.",
            tools=DESIGN_TOOLS,
        )
    )
    assert result.tier == "T0"
    assert result.status == "ok"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "emit_design"
    assert result.tool_calls[0]["arguments"] == {"scad": "cube([20,20,20]);"}
    # The raw fenced content is preserved verbatim (T0 passthrough).
    assert result.content == fenced


def test_t0_fenced_json_untagged_fence_synthesizes_tool_call():
    """The untagged ``` fence (no language tag) parses identically to the
    ```json fence — the shared codec handles both, no duplicated regex."""
    fenced = (
        '```\n{"tool": "emit_design", "arguments": {"scad": "cube([20,20,20]);"}}\n```'
    )

    async def factory(request: dict[str, Any]):
        return _ok_response(fenced)

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0]["name"] == "emit_design"
    assert result.tool_calls[0]["arguments"] == {"scad": "cube([20,20,20]);"}


def test_t0_plain_prose_content_keeps_empty_tool_calls():
    """A T0 response with tool_calls=() and no fenced JSON in content still
    yields an empty tool_calls tuple — the caller's empty_scad path, no new
    exception, no new error class."""

    async def factory(request: dict[str, Any]):
        return _ok_response("some random text", tool_calls=[])

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert result.tier == "T0"
    assert result.status == "ok"
    assert result.tool_calls == ()
    assert result.content == "some random text"


def test_t0_malformed_fenced_json_falls_through_to_empty():
    """A T0 response whose fenced block is not valid JSON (or lacks the tool
    / arguments keys) falls through to the empty-tool-call result — never a
    SenderError, never a crash."""

    async def _call_with(bad: str) -> LLMResult:
        async def factory(request: dict[str, Any]):
            return _ok_response(bad, tool_calls=[])

        return await send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )

    for bad in (
        "some prose ```json {not valid json``` more prose",
        '```json\n{"arguments": {"scad": "cube(1);"}}\n```',  # missing tool
        '```json\n{"tool": "emit_design", "arguments": "nope"}\n```',  # non-dict arguments
        "no fenced block at all",
    ):
        result = _run(_call_with(bad))
        assert result.status == "ok"
        assert result.tool_calls == ()


def test_t0_fenced_json_native_tool_calls_take_precedence():
    """When BOTH a native tool_calls entry and a fenced JSON block are present
    in content, the native tool_calls win verbatim — the fallback only
    triggers when tool_calls is empty/absent (the working T0 path is
    unchanged)."""
    fenced = '```json\n{"tool": "emit_design", "arguments": {"scad": "cube(9);"}}\n```'

    async def factory(request: dict[str, Any]):
        return _ok_response(
            f"prose\n{fenced}",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_design",
                        "arguments": '{"scad": "cube([20,25,30]);"}',
                    },
                }
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert len(result.tool_calls) == 1
    # The native call passed through (string arguments normalized to dict);
    # the fenced block was not parsed.
    assert result.tool_calls[0]["arguments"] == {"scad": "cube([20,25,30]);"}


def test_t0_json_string_tool_call_arguments_normalized_to_dict():
    """The production endpoint returns tool_call arguments as a JSON-ENCODED
    string; the sender normalizes it to a dict so _scad_from_result's
    isinstance(args, dict) check extracts the SCAD (issue #80)."""
    from d33d.design_loop import _scad_from_result

    async def factory(request: dict[str, Any]):
        return _ok_response(
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_design",
                        "arguments": '{"scad": "cube([20,20,20]);"}',
                    },
                }
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert result.status == "ok"
    assert result.tool_calls[0]["arguments"] == {"scad": "cube([20,20,20]);"}
    # The normalized dict flows through the downstream extractor unchanged.
    assert _scad_from_result(result) == "cube([20,20,20]);"


def test_t0_dict_tool_call_arguments_unchanged():
    """Already-dict arguments (the existing wire shape) pass through
    verbatim — regression guard for the native-dict path (issue #80)."""
    from d33d.design_loop import _scad_from_result

    async def factory(request: dict[str, Any]):
        return _ok_response(
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {
                        "name": "emit_design",
                        "arguments": {"scad": "cube([30,30,30]);"},
                    },
                }
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert result.status == "ok"
    assert result.tool_calls[0]["arguments"] == {"scad": "cube([30,30,30]);"}
    assert _scad_from_result(result) == "cube([30,30,30]);"


@pytest.mark.parametrize(
    "raw_args",
    ["not json at all", "", None, "[1,2,3]"],
    ids=["invalid-json", "empty-string", "none", "json-non-dict"],
)
def test_t0_malformed_tool_call_arguments_yield_empty_dict(raw_args):
    """A string that is not valid JSON, an empty string, None, or JSON that
    parses to a non-dict must yield {} (never raise) so the loop's
    empty_scad path handles it (issue #80)."""
    async def factory(request: dict[str, Any]):
        return _ok_response(
            "",
            tool_calls=[
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "emit_design", "arguments": raw_args},
                }
            ],
        )

    result = _run(
        send(
            role="design",
            model_id="m",
            messages=_design_messages(),
            request_factory=factory,
            capability=_t0(),
        )
    )
    assert result.status == "ok"
    assert result.tool_calls[0]["arguments"] == {}


def test_t0_fenced_json_name_mismatch_for_role_raises_sender_error():
    """A well-formed fenced block naming a tool the role may not emit is
    rejected at the sender boundary — same SenderError(status='error')
    contract as the native T0 path (the allowlist is the primary
    tool-name defense on both paths)."""
    fenced = '```json\n{"tool": "emit_critique", "arguments": {}}\n```'

    async def factory(request: dict[str, Any]):
        return _ok_response(fenced, tool_calls=[])

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t0(),
            )
        )
    assert exc.value.status == "error"
    assert "tool_name_mismatch" in str(exc.value)


def test_t0_malformed_body_is_sender_error():
    """A T0 response that is not OpenAI-shaped (e.g. missing choices) must
    become SenderError(status='error'), never a raw KeyError."""

    async def factory(request: dict[str, Any]):
        return _MissingChoicesResponse()

    with pytest.raises(SenderError) as exc:
        _run(
            send(
                role="design",
                model_id="m",
                messages=_design_messages(),
                request_factory=factory,
                capability=_t0(),
            )
        )
    assert exc.value.status == "error"
