"""Outgoing-request sanitiser for mid-conversation model switching.

Issue #3 acceptance #4: "Switching mid-conversation to a non-vision model
produces a valid request and leaves the stored transcript byte-identical."

The sanitiser is a **pure function**: it takes a list of message dicts (the
canonical stored transcript) plus the target model's capabilities, and returns
a **new list** — the outgoing request. The input is never mutated.

Security invariant: the sanitiser introduces no new data. It only removes,
transforms, or truncates. No key material, no I/O, no logging.

Transformations:
- ``supports_tools=False`` → drop all ``role:tool`` messages, and strip
  ``tool_calls`` / ``tool_call_id`` fields from all messages. Orphaned
  ``role:tool`` without a preceding ``tool_calls`` is the #1 mid-switch 400.
- ``supports_vision=False`` → replace ``image_url`` content blocks with a
  text placeholder ``{"type": "text", "text": "[image removed — model does
  not support vision]"}``.
- ``context_window`` smaller than the transcript's token estimate → truncate
  oldest messages (after system) and insert an explicit marker at the
  truncation point. The marker is a user-role message with content
  ``"[older messages truncated]"``.
"""

from __future__ import annotations

import copy
from typing import Any

TRUNCATION_MARKER = "[older messages truncated]"
IMAGE_PLACEHOLDER = "[image removed — model does not support vision]"


def sanitize_outgoing(
    messages: list[dict[str, Any]],
    *,
    supports_tools: bool,
    supports_vision: bool,
    context_window: int,
) -> list[dict[str, Any]]:
    """Sanitise a stored transcript for an outgoing request to a target model.

    Args:
        messages: The canonical stored transcript (list of message dicts).
            This list is **never mutated** — a new list is returned.
        supports_tools: Whether the target model supports native tool calling.
            ``False`` → drop ``role:tool`` messages and strip
            ``tool_calls``/``tool_call_id`` from all messages.
        supports_vision: Whether the target model supports image content.
            ``False`` → replace ``image_url`` blocks with a text placeholder.
        context_window: The target model's context window in tokens (rough
            estimate — character count / 4). If the transcript exceeds this,
            oldest messages (after system) are truncated with a marker.

    Returns:
        A new list of message dicts, safe to send to the target model.
    """
    # Deep-copy so the input is never mutated
    result: list[dict[str, Any]] = copy.deepcopy(messages)

    # --- 1. Tool-message sanitisation ---
    if not supports_tools:
        result = _drop_tool_messages(result)
        result = _strip_tool_fields(result)

    # --- 2. Image stripping ---
    if not supports_vision:
        result = _strip_image_blocks(result)

    # --- 3. Context-window truncation ---
    result = _truncate_to_context_window(result, context_window)

    return result


def _drop_tool_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove all ``role:tool`` messages."""
    return [m for m in messages if m.get("role") != "tool"]


def _strip_tool_fields(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Remove ``tool_calls`` and ``tool_call_id`` fields from all messages."""
    for m in messages:
        m.pop("tool_calls", None)
        m.pop("tool_call_id", None)
    return messages


def _strip_image_blocks(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Replace ``image_url`` content blocks with a text placeholder.

    A message's ``content`` field can be either a string or a list of
    content-part dicts (OpenAI's multipart format). Only the list form
    contains ``image_url`` blocks.
    """
    for m in messages:
        content = m.get("content")
        if not isinstance(content, list):
            continue
        new_content: list[dict[str, Any]] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "image_url":
                new_content.append({"type": "text", "text": IMAGE_PLACEHOLDER})
            else:
                new_content.append(block)
        m["content"] = new_content
    return messages


def _estimate_tokens(messages: list[dict[str, Any]]) -> int:
    """Rough token estimate: character count / 4, plus per-message overhead.

    Each message dict contributes ~4 tokens of overhead (role, separators,
    JSON structure) beyond its content. This prevents under-estimation when
    the transcript is full of short messages.
    """
    total = 0
    for m in messages:
        total += 4  # per-message overhead (role, JSON structure)
        content = m.get("content", "")
        if isinstance(content, str):
            total += max(1, len(content) // 4)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict):
                    text = block.get("text", "")
                    if isinstance(text, str):
                        total += max(1, len(text) // 4)
        # Account for tool_calls if present (rare after sanitisation)
        tool_calls = m.get("tool_calls")
        if isinstance(tool_calls, list):
            for tc in tool_calls:
                if isinstance(tc, dict):
                    fn = tc.get("function", {})
                    args = fn.get("arguments", "")
                    if isinstance(args, str):
                        total += max(1, len(args) // 4)
    return total


def _truncate_to_context_window(
    messages: list[dict[str, Any]],
    context_window: int,
) -> list[dict[str, Any]]:
    """Truncate oldest messages (after system) if the transcript exceeds
    ``context_window`` tokens. Inserts an explicit truncation marker at the
    truncation point.
    """
    if _estimate_tokens(messages) <= context_window:
        return messages

    # Separate system message (index 0) from the rest
    system = messages[0] if messages and messages[0].get("role") == "system" else None
    rest = messages[1:] if system is not None else messages

    # Walk from the end, keeping messages until we fit in the context window
    marker_msg = _marker_msg()
    marker_tokens = _estimate_tokens([marker_msg])
    system_tokens = _estimate_tokens([system]) if system is not None else 0
    available = context_window - system_tokens - marker_tokens

    # Find the earliest index in `rest` we can keep
    kept: list[dict[str, Any]] = []
    running_tokens = 0
    truncated = False
    for i in range(len(rest) - 1, -1, -1):
        msg_tokens = _estimate_tokens([rest[i]])
        if running_tokens + msg_tokens > available and i > 0:
            truncated = True
            break
        kept.insert(0, rest[i])
        running_tokens += msg_tokens

    # If we kept all messages but still over budget, force-truncate
    if not truncated and running_tokens > available:
        truncated = True
        # Pop from the front until we fit
        while kept:
            first_tokens = _estimate_tokens([kept[0]])
            if running_tokens - first_tokens <= available:
                break
            kept.pop(0)
            running_tokens -= first_tokens

    if truncated:
        if system is not None:
            return [system, marker_msg, *kept]
        return [marker_msg, *kept]

    if system is not None:
        return [system, *kept]
    return kept


def _marker_msg() -> dict[str, Any]:
    """The explicit truncation marker message."""
    return {"role": "user", "content": TRUNCATION_MARKER}
