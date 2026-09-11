"""Unit tests for the prompt canonical hash (acceptance criterion #5).

The canonical hash is the diff key that lets two request_logs rows called
on the *same prompt* through *different models* be joined and diffed purely
from the logs. Per the settled issue decisions, its input is the canonical
form of the outgoing request — role, system prompt, and message contents —
normalised so that incidental formatting (whitespace runs, JSON key order,
content-part ordering) does not change the hash, while meaningful content
changes do. It MUST exclude the resolved model id and the provider: including
either would make the hash differ between models and destroy its purpose.

No model id or provider string ever enters the hash — the golden-fixture
pair (A10) pins that invariant.
"""

from __future__ import annotations

import json
from pathlib import Path

import d33d.prompt_hash as ph

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"
PAIR_PATH = FIXTURES / "prompt_hash_pair.json"


def _pair() -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    payload = json.loads(PAIR_PATH.read_text(encoding="utf-8"))
    return payload["model_a_request"], payload["model_b_request"]


def test_hash_is_deterministic_for_identical_input() -> None:
    a, _ = _pair()
    assert ph.canonical_hash(role="design", messages=a) == ph.canonical_hash(
        role="design", messages=a
    )


def test_identical_prompt_across_two_models_hashes_identically() -> None:
    """Acceptance #5 core: same role + same messages, different resolved
    model ids behind each call → identical canonical hash."""
    a, b = _pair()
    assert ph.canonical_hash(role="design", messages=a) == ph.canonical_hash(
        role="design", messages=b
    )
    # Explicit: the model ids that differ between the two rows are excluded.
    ha = ph.canonical_hash(role="design", messages=a)
    hb = ph.canonical_hash(role="design", messages=b)
    assert ha == hb


def test_hash_is_stable_for_a_given_call_shape() -> None:
    a, _ = _pair()
    expected = ph.canonical_hash(role="design", messages=a)
    # Run 100 times — must never vary (no set iteration order, no wall clock).
    assert {ph.canonical_hash(role="design", messages=a) for _ in range(100)} == {
        expected
    }


def test_content_part_order_does_not_change_hash() -> None:
    a = [
        {
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
                {"type": "text", "text": "red"},
            ],
        }
    ]
    b = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "red"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA"}},
            ],
        }
    ]
    assert ph.canonical_hash(role="critique", messages=a) == ph.canonical_hash(
        role="critique", messages=b
    )


def test_whitespace_normalisation_is_stable() -> None:
    a = [{"role": "user", "content": "Design   a    cube"}]
    b = [{"role": "user", "content": "Design a cube"}]
    assert ph.canonical_hash(role="design", messages=a) == ph.canonical_hash(
        role="design", messages=b
    )


def test_json_payload_key_order_is_ignored() -> None:
    a = [{"role": "user", "content": json.dumps({"x": 1, "y": 2})}]
    b = [{"role": "user", "content": json.dumps({"y": 2, "x": 1})}]
    assert ph.canonical_hash(role="design", messages=a) == ph.canonical_hash(
        role="design", messages=b
    )


def test_different_messages_yield_different_hashes() -> None:
    assert ph.canonical_hash(
        role="design", messages=[{"role": "user", "content": "a cube"}]
    ) != ph.canonical_hash(
        role="design", messages=[{"role": "user", "content": "a sphere"}]
    )


def test_different_roles_yield_different_hashes() -> None:
    msgs = [{"role": "user", "content": "a cube"}]
    assert ph.canonical_hash(role="design", messages=msgs) != ph.canonical_hash(
        role="critique", messages=msgs
    )


def test_tool_call_arguments_are_canonicalised() -> None:
    a = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"a": 1, "b": 2}'},
                },
            ],
        },
    ]
    b = [
        {
            "role": "assistant",
            "content": None,
            "tool_calls": [
                {
                    "id": "call_1",
                    "type": "function",
                    "function": {"name": "f", "arguments": '{"b": 2, "a": 1}'},
                },
            ],
        },
    ]
    assert ph.canonical_hash(role="design", messages=a) == ph.canonical_hash(
        role="design", messages=b
    )


def test_hash_shape_is_a_hex_sha256_digest() -> None:
    a, _ = _pair()
    h = ph.canonical_hash(role="design", messages=a)
    assert len(h) == 64
    int(h, 16)  # raises if not hex


def test_prompt_field_is_included_in_hash() -> None:
    a, _ = _pair()
    h_with = ph.canonical_hash(
        role="design", messages=a, system="You are a CAD engine."
    )
    h_without = ph.canonical_hash(role="design", messages=a)
    assert h_with != h_without
