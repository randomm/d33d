"""T1 constrained fenced-JSON tool-call protocol (built from day one).

Many local OpenAI-compatible servers lack native tool calling.  The T1
fallback lets the same logical "tool call" operation complete over plain
text: the model is instructed (in the system prompt) to emit exactly one
fenced JSON block, the response is parsed by regex, and a malformed block
triggers a corrective retry before giving up.

T0 and T1 are the same logical operation at the agent-loop boundary: the
agent loop never branches on tier, only the request/response codec differs.

This module owns the codec only (prompt fragment, response parse, corrective
retry); the HTTP edge is injected per call, mirroring ``probes.py``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

__all__ = [
    "MAX_CORRECTIVE_RETRIES",
    "T1ToolCall",
    "build_t1_system_fragment",
    "parse_t1_tool_call",
    "t1_invoke",
]

#: Number of corrective retries after a malformed fenced-JSON response.
MAX_CORRECTIVE_RETRIES = 1

_FENCE_RE = re.compile(
    r"```(?:json)?\s*(\{.*?\})\s*```",
    re.DOTALL,
)


@dataclass(frozen=True)
class T1ToolCall:
    """A completed T1 tool call, shaped like a native tool_calls[0] entry."""

    name: str
    arguments: dict[str, Any]
    raw: str


def build_t1_system_fragment(tool_names: list[str]) -> str:
    """System-prompt fragment constraining the model to one fenced JSON block."""
    names = ", ".join(tool_names) or "the provided tool"
    return (
        "When you need to call a tool, respond with EXACTLY ONE fenced JSON "
        f"block and nothing else (available tools: {names}). Format: "
        "```json\n"
        '{"tool": "<tool name>", "arguments": {"<param>": <value>}}\n'
        "```"
    )


def _extract_json_object(text: str) -> str | None:
    """Return the first parseable top-level JSON object, or None.

    Scans for balanced ``{...}`` spans (string-aware, so braces inside JSON
    string values are ignored) and validates each candidate with
    ``json.loads``.  A span that is balanced but not valid JSON (e.g. a
    code snippet embedded in prose) is skipped in favour of the next one.
    """
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            else:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start : i + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                else:
                    return candidate
    return None


def parse_t1_tool_call(text: str) -> T1ToolCall | None:
    """Parse a T1 response; return None (not an exception) on malformed JSON.

    Accepts a fenced block (```json ... ```) or bare JSON; both are extracted
    via regex first, then validated as a JSON object carrying ``tool`` and a
    JSON-object ``arguments``.
    """
    if not text:
        return None

    obj: Any = None
    for m in _FENCE_RE.finditer(text):
        try:
            parsed = json.loads(m.group(1))
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict) and "tool" in parsed:
            obj = parsed
            break
    if obj is None:
        obj_candidate = _extract_json_object(text)
        if obj_candidate is None:
            return None
        try:
            parsed = json.loads(obj_candidate)
        except json.JSONDecodeError:
            return None
        if not (isinstance(parsed, dict) and "tool" in parsed):
            return None
        obj = parsed

    assert isinstance(obj, dict)
    name = obj.get("tool")
    args = obj.get("arguments", {})
    if not isinstance(name, str) or not isinstance(args, dict):
        return None
    return T1ToolCall(name=name, arguments=args, raw=text)


def corrective_message(previous: str) -> str:
    """User message for the corrective retry after a malformed response."""
    return (
        "Your previous response was not valid fenced JSON. "
        "Respond again with EXACTLY ONE fenced JSON block of the form "
        '```json\n{"tool": "<name>", "arguments": {...}}\n``` '
        "and nothing else."
    )


AsyncFn = Callable[[dict[str, Any]], Awaitable[Any]]


async def t1_invoke(
    *,
    request_factory: AsyncFn,
    system_prompt: str,
    user_message: str,
    tool_names: list[str],
    corrective_retries: int = MAX_CORRECTIVE_RETRIES,
) -> T1ToolCall:
    """Complete one logical tool call over the T1 text protocol.

    ``request_factory(request) -> response`` mirrors the probe edge:
    ``response.ok`` and ``response.json()`` with an OpenAI-shaped body.
    Raises ``RuntimeError`` when the model still fails after all
    corrective retries (the agent loop handles that like any tool error).

    Every non-OK or malformed HTTP response (missing ``choices``, a
    non-list, a non-dict message, a null/non-string content) is a
    *protocol* failure, not a crash: it raises the documented
    ``RuntimeError`` here — never a raw ``KeyError``/``TypeError`` — so
    the sender can classify it as ``SenderError(status='error')`` and the
    request-log write for the failure still fires.
    """
    fragment = build_t1_system_fragment(tool_names)
    base_messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt + "\n\n" + fragment},
        {"role": "user", "content": user_message},
    ]
    last_text = ""
    for attempt in range(corrective_retries + 1):
        messages = list(base_messages)
        if attempt > 0:
            messages.append({"role": "user", "content": corrective_message(last_text)})
        resp = await request_factory({"messages": messages})
        last_text = _response_content(resp) or ""
        call = parse_t1_tool_call(last_text)
        if call is not None:
            return call
    raise RuntimeError(
        f"T1 tool call failed after {corrective_retries + 1} attempts: {last_text!r}"
    )


def _response_content(resp: Any) -> str | None:
    """The message content of an OpenAI-shaped response body, or None.

    Defensive shape validation: a non-OK response, a non-JSON body, a
    missing/empty ``choices`` list, a non-dict ``message`` or a
    null/non-string ``content`` all yield ``None`` (the caller raises the
    documented ``RuntimeError``) — a raw ``KeyError``/``TypeError`` never
    escapes this function.
    """
    if not getattr(resp, "ok", False):
        return None
    try:
        data = resp.json()
    except (ValueError, TypeError):
        return None
    if not isinstance(data, dict):
        return None
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return None
    first = choices[0]
    if not isinstance(first, dict):
        return None
    message = first.get("message")
    if not isinstance(message, dict):
        return None
    content = message.get("content")
    if isinstance(content, str):
        return content
    return None
