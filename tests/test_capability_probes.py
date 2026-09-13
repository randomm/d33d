"""Capability probes + T1 fenced-JSON protocol (issue #3, task-c).

Covers acceptance #2: the three startup probes drive tier selection, a
tool-less endpoint lands on T1 and completes a tool call via the fenced-JSON
text protocol with a corrective retry, and probe results are cached by
base_url + model hash (re-probe only on change).

All tests mock the OpenAI-compatible HTTP edge — no real provider is dialed.
Async entry points run under plain ``asyncio.run`` (no async plugin in CI).
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from d33d.config.probes import (
    CapabilityCache,
    CapabilityResult,
    Tier,
    probe_capabilities,
    select_tier,
    tier_from_declared,
)
from d33d.config.t1_protocol import (
    MAX_CORRECTIVE_RETRIES,
    T1ToolCall,
    build_t1_system_fragment,
    parse_t1_tool_call,
    t1_invoke,
)
from d33d.response_shape import response_message_shape


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


@dataclass
class FakeResponse:
    status_code: int
    body: dict[str, Any]

    @property
    def is_success(self) -> bool:
        return 200 <= self.status_code < 300

    def json(self) -> dict[str, Any]:
        return self.body


def chat_response(
    *, content: Any = "ok", tool_calls: list | None = None
) -> FakeResponse:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return FakeResponse(200, {"choices": [{"message": message}]})


@dataclass
class ScriptedTransport:
    """request_factory that answers by keyword and records every request."""

    script: dict[str, Any] = field(default_factory=dict)
    requests: list[dict[str, Any]] = field(default_factory=list)

    async def __call__(self, request: dict[str, Any]) -> FakeResponse:
        self.requests.append(request)
        if "raise" in self.script:
            raise ConnectionError("endpoint down")
        if "no_tools" in self.script:
            return chat_response(content=json.dumps(self.script["no_tools"]))
        if request.get("json", {}).get("tools"):
            if "tools_ok" in self.script:
                return chat_response(
                    content=None,
                    tool_calls=[
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "answer",
                                "arguments": json.dumps(
                                    self.script.get("tools_ok", {})
                                ),
                            },
                        }
                    ],
                )
            return chat_response(content="I cannot call tools.")
        content = self.script.get("content", "a small square")
        return chat_response(content=content)


# --------------------------------------------------------------------------
# Tier mapping
# --------------------------------------------------------------------------


def test_tier_from_declared_full_t0():
    assert tier_from_declared(tools=True, json_schema=True, vision=True) == Tier.T0


def test_tier_without_json_schema_drops_to_t1():
    assert tier_from_declared(tools=True, json_schema=False, vision=True) == Tier.T1


def test_tier_without_tools_is_t1():
    assert tier_from_declared(tools=False, json_schema=True, vision=True) == Tier.T1


def test_tier_without_vision_and_no_fenced_is_t3():
    assert tier_from_declared(tools=False, json_schema=False, vision=False) == Tier.T3


def test_tier_t2_when_vision_but_no_tools():
    assert (
        tier_from_declared(
            tools=False, json_schema=False, vision=True, fenced_json=False
        )
        == Tier.T2
    )


# --------------------------------------------------------------------------
# The three probes
# --------------------------------------------------------------------------


def test_probes_against_toolless_endpoint_land_on_t1():
    transport = ScriptedTransport(script={"no_tools": {"tool": "answer"}})
    result = _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="local/model",
            request_factory=transport,
        )
    )
    assert result.tools is False
    assert result.vision is True
    assert result.max_images == 2
    assert select_tier(result) == Tier.T1
    # One image probe + two-image probe + tool probe = three calls.
    assert len(transport.requests) == 3
    # The tool probe carried exactly one tool definition.
    tool_req = [r for r in transport.requests if r["json"].get("tools")]
    assert len(tool_req) == 1
    assert len(tool_req[0]["json"]["tools"]) == 1


def test_probes_against_full_t0_endpoint():
    transport = ScriptedTransport(script={"tools_ok": {"text": "hi"}})
    result = _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="full/model",
            request_factory=transport,
        )
    )
    assert result.tools is True
    assert result.json_schema is True
    assert select_tier(result) == Tier.T0


def test_first_image_probe_uses_one_32x32_data_uri():
    import base64
    import struct

    transport = ScriptedTransport()
    _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="m",
            request_factory=transport,
        )
    )
    first = transport.requests[0]["json"]["messages"][0]["content"]
    image_parts = [p for p in first if p.get("type") == "image_url"]
    assert len(image_parts) == 1
    url = image_parts[0]["image_url"]["url"]
    assert url.startswith("data:image/png;base64,")
    raw_png = base64.b64decode(url.split(",", 1)[1])
    assert raw_png[:8] == b"\x89PNG\r\n\x1a\n"
    w, h = struct.unpack(">II", raw_png[16:24])
    assert (w, h) == (32, 32)


def test_second_probe_sends_two_images():
    transport = ScriptedTransport()
    _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="m",
            request_factory=transport,
        )
    )
    second = transport.requests[1]["json"]["messages"][0]["content"]
    image_parts = [p for p in second if p.get("type") == "image_url"]
    assert len(image_parts) == 2


def test_single_image_cap_degrades_max_images_to_1():
    """A server that rejects two images but accepts one caps max_images at 1."""
    transport = ScriptedTransport()

    async def factory(request: dict[str, Any]) -> FakeResponse:
        content = request["json"]["messages"][0]["content"]
        images = [
            p for p in content if isinstance(p, dict) and p.get("type") == "image_url"
        ]
        if len(images) == 2:
            return FakeResponse(400, {"error": "max 1 image"})
        return await transport(request)

    result = _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="m",
            request_factory=factory,
        )
    )
    assert result.max_images == 1


def test_probe_failure_degrades_to_t3_without_raising():
    transport = ScriptedTransport(script={"raise": True})
    result = _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="m",
            request_factory=transport,
        )
    )
    assert result.vision is False
    assert result.tools is False
    assert result.max_images == 0
    # Total failure: lowest viable tier, never raise.
    assert select_tier(result) == Tier.T3


def test_declared_caps_probed_capabilities():
    declared = CapabilityResult(
        tools=False, json_schema=False, vision=True, max_images=1
    )
    transport = ScriptedTransport(script={"tools_ok": {}})
    result = _run(
        probe_capabilities(
            base_url="https://example.invalid/v1",
            model_id="m",
            declared=declared,
            request_factory=transport,
        )
    )
    # Probe says tools work, declaration says the operator disallowed them.
    assert result.tools is False
    assert result.max_images == 1
    assert select_tier(result) == Tier.T1


# --------------------------------------------------------------------------
# Cache: keyed by base_url + model hash
# --------------------------------------------------------------------------


def test_cache_hit_same_base_and_model():
    cache = CapabilityCache()
    r = CapabilityResult(tools=True, json_schema=True, vision=True, max_images=2)
    cache.put("https://a/v1", "model-x", r)
    assert cache.get("https://a/v1", "model-x") is r
    assert cache.get("https://a/v1", "model-y") is None


def test_cache_miss_on_model_change_same_base():
    cache = CapabilityCache()
    r = CapabilityResult(tools=True, json_schema=True, vision=True, max_images=2)
    cache.put("https://a/v1", "model-x", r)
    assert cache.get("https://a/v1", "model-x-renamed") is None


def test_cache_miss_on_base_change_same_model():
    cache = CapabilityCache()
    r = CapabilityResult(tools=True, json_schema=True, vision=True, max_images=2)
    cache.put("https://a/v1", "model-x", r)
    assert cache.get("https://b/v1", "model-x") is None


def test_cache_clear_forces_reprobe():
    cache = CapabilityCache()
    cache.put("https://a/v1", "m", CapabilityResult(True, True, True, 2))
    cache.clear()
    assert cache.get("https://a/v1", "m") is None


def test_probe_only_runs_on_cache_miss():
    cache = CapabilityCache()
    transport = ScriptedTransport(script={"no_tools": {}})

    async def cached_probe() -> CapabilityResult:
        hit = cache.get("https://a/v1", "model-x")
        if hit is not None:
            return hit
        result = await probe_capabilities(
            base_url="https://a/v1",
            model_id="model-x",
            request_factory=transport,
        )
        cache.put("https://a/v1", "model-x", result)
        return result

    first = _run(cached_probe())
    assert len(transport.requests) == 3  # three probes on first run
    second = _run(cached_probe())
    assert len(transport.requests) == 3  # cache hit: no new probes
    assert second is first

    # Model swap under the same base must re-probe.
    transport2 = ScriptedTransport(script={"no_tools": {}})
    cache.clear()
    _run(
        probe_capabilities(
            base_url="https://a/v1",
            model_id="model-x",
            request_factory=transport2,
        )
    )
    assert len(transport2.requests) == 3


# --------------------------------------------------------------------------
# T1 fenced-JSON protocol
# --------------------------------------------------------------------------


def test_parse_t1_fenced_json():
    text = 'Sure:\n```json\n{"tool": "answer", "arguments": {"text": "hi"}}\n```\n'
    call = parse_t1_tool_call(text)
    assert call is not None
    assert call.name == "answer"
    assert call.arguments == {"text": "hi"}


def test_parse_t1_bare_json():
    call = parse_t1_tool_call('{"tool": "answer", "arguments": {"n": 1}}')
    assert call is not None
    assert call.name == "answer"


def test_parse_t1_rejects_malformed_json():
    assert parse_t1_tool_call("```json\n{not json}\n```") is None


def test_parse_t1_rejects_missing_tool_key():
    assert parse_t1_tool_call('{"arguments": {}}') is None


def test_parse_t1_rejects_non_object_arguments():
    assert parse_t1_tool_call('{"tool": "answer", "arguments": "nope"}') is None


def test_parse_t1_ignores_nested_braces_in_strings():
    text = '{"tool": "answer", "arguments": {"text": "{ { } }"}}'
    call = parse_t1_tool_call(text)
    assert call is not None
    assert call.arguments["text"] == "{ { } }"


def test_build_t1_system_fragment_names_tools():
    frag = build_t1_system_fragment(["render", "measure"])
    assert "render" in frag and "measure" in frag
    assert "json" in frag.lower()


def test_t1toolcall_shape():
    call = T1ToolCall(name="answer", arguments={"a": 1}, raw="x")
    assert call.name == "answer"
    assert call.arguments == {"a": 1}


def test_t1_invoke_completes_tool_call_with_corrective_retry():
    """A tool-less endpoint lands on T1 and still completes a tool call:
    first response is malformed, the corrective retry succeeds."""
    responses = [
        FakeResponse(200, {"choices": [{"message": {"content": "sorry, {bad json"}}]}),
        FakeResponse(
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": '```json\n{"tool": "answer", "arguments": {"text": "hi"}}\n```'
                        }
                    }
                ]
            },
        ),
    ]

    async def factory(request: dict[str, Any]) -> FakeResponse:
        return responses.pop(0)

    call = _run(
        t1_invoke(
            request_factory=factory,
            system_prompt="You are d33d.",
            user_message="Answer: 2+2",
            tool_names=["answer"],
        )
    )
    assert call.name == "answer"
    assert call.arguments == {"text": "hi"}


def test_t1_invoke_raises_after_exhausting_retries():
    async def factory(request: dict[str, Any]) -> FakeResponse:
        return FakeResponse(
            200, {"choices": [{"message": {"content": "still no json"}}]}
        )

    with pytest.raises(RuntimeError):
        _run(
            t1_invoke(
                request_factory=factory,
                system_prompt="sys",
                user_message="user",
                tool_names=["answer"],
            )
        )


def test_t1_invoke_sends_corrective_message_on_retry():
    calls: list[list[dict[str, Any]]] = []
    responses = [
        FakeResponse(200, {"choices": [{"message": {"content": "bad"}}]}),
        FakeResponse(
            200,
            {
                "choices": [
                    {"message": {"content": '{"tool": "answer", "arguments": {}}'}}
                ]
            },
        ),
    ]

    async def factory(request: dict[str, Any]) -> FakeResponse:
        calls.append(request["messages"])
        return responses.pop(0)

    _run(
        t1_invoke(
            request_factory=factory,
            system_prompt="sys",
            user_message="u",
            tool_names=["answer"],
        )
    )
    # First request: system + user. Second: + corrective user message.
    assert len(calls[0]) == 2
    assert len(calls[1]) == 3
    assert "fenced JSON" in calls[1][2]["content"]
    assert calls[1][2]["role"] == "user"


def test_max_corrective_retries_is_one():
    assert MAX_CORRECTIVE_RETRIES == 1


def test_response_message_shape_helper_valid_and_violating_shapes():
    """The shared OpenAI response-shape helper returns the message dict
    for a valid body and None on any shape violation (non-dict body,
    empty or non-list choices, non-dict choices[0], non-dict message).
    Covers the t1 half of the design_llm/t1_protocol dedup."""""
    assert response_message_shape(
        {"choices": [{"message": {"content": None}}]}
    ) == {"content": None}
    assert response_message_shape({"choices": []}) is None  # empty choices
    assert response_message_shape({"choices": ["x"]}) is None  # non-dict choices[0]
    assert response_message_shape({"choices": [{}]}) is None  # missing message
