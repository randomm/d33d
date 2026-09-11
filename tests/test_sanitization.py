"""Unit tests: outgoing-request sanitizer for mid-conversation model switching.

Issue #3 acceptance #4: switching mid-conversation to a non-vision model
produces a valid outgoing request (image_url blocks stripped to a text
placeholder) and leaves the stored transcript byte-identical. Orphaned
``role:tool`` without a preceding ``tool_calls`` is the #1 mid-switch 400 —
the sanitizer must drop tool messages and ``tool_calls``/``tool_call_id``
together, never one side alone.

No Docker, no network — pure data transformation. The stored transcript is
always a separate object from the outgoing request.
"""

from __future__ import annotations

import json

from d33d.security import sanitize


def _transcript_with_tool_messages() -> list[dict]:
    """A realistic transcript containing tool_calls + a role:tool response.

    This is the shape that triggers the #1 mid-switch 400 if not sanitized:
    an assistant message with tool_calls, followed by a role:tool reply.
    """
    return [
        {"role": "system", "content": "You are a 3D design assistant."},
        {"role": "user", "content": "Make a gear in OpenSCAD."},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call_abc123",
                    "type": "function",
                    "function": {"name": "write_openscad", "arguments": "{}"},
                }
            ],
        },
        {"role": "tool", "tool_call_id": "call_abc123", "content": "gear.scad written"},
        {"role": "user", "content": "Now render it."},
    ]


def _transcript_with_images() -> list[dict]:
    return [
        {"role": "system", "content": "You are a 3D design assistant."},
        {"role": "user", "content": "Compare with this photo."},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Here is the reference photo:"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="},
                },
            ],
        },
        {"role": "assistant", "content": "It looks like a gear with 12 teeth."},
    ]


def _transcript_with_images_and_tool_messages() -> list[dict]:
    base = _transcript_with_tool_messages()
    base.append(
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Here is the reference:"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,AAAA"},
                },
            ],
        }
    )
    return base


def test_stored_transcript_is_byte_identical_after_sanitize() -> None:
    """Acceptance #4: stored transcript unchanged; outgoing is the mutated copy."""
    original = _transcript_with_tool_messages()
    snapshot = json.dumps(original)  # canonical snapshot
    result = sanitize.sanitize_outgoing(
        original, supports_tools=False, supports_vision=True, context_window=100000
    )
    # Original must be unchanged
    assert json.dumps(original) == snapshot, "stored transcript was mutated"
    # Result must not contain tool messages
    assert all(m["role"] != "tool" for m in result)


def test_non_tool_target_drops_tool_messages_and_tool_calls() -> None:
    msgs = _transcript_with_tool_messages()
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=False, supports_vision=True, context_window=100000
    )
    for m in result:
        assert m["role"] != "tool", f"orphaned role:tool leaked: {m}"
        assert "tool_calls" not in m, f"tool_calls field leaked on {m['role']}: {m}"
        assert "tool_call_id" not in m, f"tool_call_id field leaked on {m['role']}: {m}"


def test_non_vision_target_strips_image_blocks_leaving_placeholder() -> None:
    msgs = _transcript_with_images()
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=False, context_window=100000
    )
    # The message that carried images must now have a text placeholder
    for m in result:
        if isinstance(m.get("content"), list):
            for block in m["content"]:
                assert block.get("type") != "image_url", (
                    f"image_url block leaked: {block}"
                )
    # Ensure a text placeholder was inserted (check raw string, not JSON-escaped)
    all_text = "".join(
        b.get("text", "")
        for m in result
        if isinstance(m.get("content"), list)
        for b in m["content"]
        if isinstance(b, dict)
    )
    assert "[image removed" in all_text, (
        "no text placeholder found where image_url block was stripped"
    )


def test_combined_non_tool_non_vision_target() -> None:
    msgs = _transcript_with_images_and_tool_messages()
    snapshot = json.dumps(msgs)
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=False, supports_vision=False, context_window=100000
    )
    assert json.dumps(msgs) == snapshot
    for m in result:
        assert m["role"] != "tool"
        assert "tool_calls" not in m
        assert "tool_call_id" not in m
        if isinstance(m.get("content"), list):
            for b in m["content"]:
                assert b.get("type") != "image_url"


def test_full_capability_target_is_noop() -> None:
    msgs = _transcript_with_images_and_tool_messages()
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=1000000
    )
    # All messages preserved, structure intact
    assert len(result) == len(msgs)
    assert any(m["role"] == "tool" for m in result)


def test_smaller_context_truncates_oldest_with_marker() -> None:
    # Build a long transcript
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(50):
        msgs.append({"role": "user", "content": f"message {i}"})
        msgs.append({"role": "assistant", "content": f"response {i}"})
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=500
    )
    # Messages were truncated
    assert len(result) < len(msgs)
    # The first non-system message must be the explicit truncation marker
    non_system = [m for m in result if m["role"] != "system"]
    assert len(non_system) > 0
    marker_msg = non_system[0]
    assert "[older messages truncated]" in marker_msg["content"], (
        f"expected truncation marker, got: {marker_msg['content']!r}"
    )


def test_truncation_preserves_recent_messages() -> None:
    msgs = [{"role": "system", "content": "sys"}]
    for i in range(50):
        msgs.append({"role": "user", "content": f"message {i}"})
        msgs.append({"role": "assistant", "content": f"response {i}"})
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=True, supports_vision=True, context_window=500
    )
    # The last user message should be preserved
    last_user = next(m for m in reversed(result) if m["role"] == "user")
    assert last_user["content"] == "message 49"


def test_orphaned_tool_message_without_preceding_tool_calls_is_dropped() -> None:
    """An orphaned role:tool (no preceding tool_calls) must be dropped, not 400."""
    msgs = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "hi"},
        {"role": "tool", "tool_call_id": "orphaned_call", "content": "orphan reply"},
    ]
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=False, supports_vision=True, context_window=100000
    )
    assert all(m["role"] != "tool" for m in result)


def test_sanitize_returns_new_list_not_in_place_mutation() -> None:
    msgs = _transcript_with_tool_messages()
    result = sanitize.sanitize_outgoing(
        msgs, supports_tools=False, supports_vision=True, context_window=100000
    )
    assert result is not msgs
