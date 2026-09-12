"""Fast-layer test: ``d33d.render_worker.parse_defines`` — the authoritative
validation for ``params.json``.

Closes the MEDIUM findings from the six-lens code review of
``entrypoint.sh`` (findings 1, 2, 4, 5) by exercising the Python caller
where ``json`` is available:

- **Finding 1 (security):** malformed ``params.json`` (unclosed object,
  trailing comma, extra brace) is rejected with ``ValueError`` — the
  render caller maps this to ``container_error`` — rather than silently
  guessing intent and forwarding partial -D flags.
- **Finding 2 (architecture):** validation lives in Python (the single
  source of truth), not in a character-level awk state machine in the
  entrypoint.
- **Finding 4 (performance):** a defines value exceeding
  ``DEFINES_VALUE_MAX_CHARS`` is rejected, bounding the O(n²) awk
  string-concatenation cost.
- **Finding 5 (type safety):** a defines value containing a tab or other
  control character is rejected, preventing silent TSV column splitting
  in the entrypoint's ``read`` loop.
"""

from __future__ import annotations

import pytest

import d33d.render_worker as rw

# ── Valid inputs ───────────────────────────────────────────────────────────────


def test_valid_defines_returns_the_map() -> None:
    assert rw.parse_defines('{"defines": {"A": "1", "B": "2"}}') == {
        "A": "1",
        "B": "2",
    }


def test_empty_defines_object_returns_empty_dict() -> None:
    assert rw.parse_defines('{"defines": {}}') == {}


def test_no_defines_key_returns_empty_dict() -> None:
    assert rw.parse_defines('{"timeout_s": 60}') == {}


def test_empty_object_returns_empty_dict() -> None:
    assert rw.parse_defines("{}") == {}


# ── Finding 1: malformed JSON is rejected (security) ─────────────────────────


def test_unclosed_defines_object_is_rejected() -> None:
    """``{"defines": {"A": "1"}`` (the object is never closed) must raise
    rather than silently forwarding ``A=1`` as a -D flag."""
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "1"}')


def test_trailing_comma_in_defines_is_rejected() -> None:
    """``{"defines": {"A": "1",}}`` is not valid JSON; ``json.loads``
    rejects it, and ``parse_defines`` must propagate that as ``ValueError``."""
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "1",}}')


def test_extra_brace_is_rejected() -> None:
    """``{"defines": {"A": "1"}}}`` has a stray closing brace; it must be
    rejected rather than treated as a valid render."""
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "1"}}}')


def test_non_json_text_is_rejected() -> None:
    with pytest.raises(ValueError):
        rw.parse_defines("this is not json at all")


def test_non_object_top_level_is_rejected() -> None:
    """A JSON array at the top level is not a valid params.json."""
    with pytest.raises(TypeError):
        rw.parse_defines("[1, 2, 3]")


def test_defines_not_object_is_rejected() -> None:
    """``defines`` must be an object; a list or scalar is a caller bug."""
    with pytest.raises(TypeError):
        rw.parse_defines('{"defines": ["A", "1"]}')
    with pytest.raises(TypeError):
        rw.parse_defines('{"defines": "A=1"}')


# ── Finding 5: control characters in values are rejected (type safety) ───────


def test_tab_in_defines_value_is_rejected() -> None:
    """A literal tab in a defines value would cause the entrypoint's
    ``IFS=$'\\t' read -r name value`` to split on the second tab, silently
    corrupting the -D flag. ``parse_defines`` must reject it before the
    render runs."""
    with pytest.raises(ValueError, match="control character"):
        rw.parse_defines('{"defines": {"A": "a\\tb"}}')


def test_newline_in_defines_value_is_rejected() -> None:
    """A literal newline in a value (written raw, not escaped) is a control
    character and must be rejected."""
    # A raw newline inside a JSON string is invalid JSON; json.loads rejects
    # it before _CONTROL_CHAR_RE even runs — both paths raise ValueError.
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "a\nb"}}')


def test_carriage_return_in_defines_value_is_rejected() -> None:
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "a\\rb"}}')


def test_tab_in_defines_key_is_rejected() -> None:
    with pytest.raises(ValueError, match="control character"):
        rw.parse_defines('{"defines": {"A\\tB": "1"}}')


def test_null_byte_in_defines_value_is_rejected() -> None:
    # ``\x00`` is not a valid JSON escape; json.loads rejects it with a
    # ValueError before the control-character regex even runs.
    with pytest.raises(ValueError):
        rw.parse_defines('{"defines": {"A": "a\\x00b"}}')


# ── Finding 4: size cap is enforced (performance) ─────────────────────────────


def test_oversized_defines_value_is_rejected() -> None:
    """A defines value exceeding ``DEFINES_VALUE_MAX_CHARS`` is rejected.
    The caller only sets small macro values; a runaway LLM output that
    produces a multi-KB value is a caller bug, not a valid render input."""
    big_value = "x" * (rw.DEFINES_VALUE_MAX_CHARS + 1)
    with pytest.raises(ValueError, match="exceeding"):
        rw.parse_defines('{"defines": {"A": "' + big_value + '"}}')


def test_defines_value_at_cap_is_accepted() -> None:
    """The cap is inclusive: a value of exactly ``DEFINES_VALUE_MAX_CHARS``
    characters passes."""
    value_at_cap = "x" * rw.DEFINES_VALUE_MAX_CHARS
    assert rw.parse_defines('{"defines": {"A": "' + value_at_cap + '"}}') == {
        "A": value_at_cap
    }


# ── Non-string defines values are rejected ────────────────────────────────────


def test_non_string_defines_value_is_rejected() -> None:
    """A numeric (non-string) defines value is a caller bug; the old
    ``str(v)`` conversion in ``from_dict`` silently coerced it. Both
    ``parse_defines`` and ``RenderParams.from_dict`` must reject it."""
    with pytest.raises(TypeError):
        rw.parse_defines('{"defines": {"A": 1}}')
    with pytest.raises(TypeError):
        rw.RenderParams.from_dict({"defines": {"A": 1}})


def test_bool_defines_value_is_rejected() -> None:
    with pytest.raises(TypeError):
        rw.parse_defines('{"defines": {"A": true}}')


def test_null_defines_value_is_rejected() -> None:
    with pytest.raises(TypeError):
        rw.parse_defines('{"defines": {"A": null}}')


# ── RenderParams.from_dict in-memory path mirrors parse_defines ──────────────


def test_from_dict_rejects_control_chars_in_defines_value() -> None:
    """The in-memory ``from_dict`` path must enforce the same invariants as
    ``parse_defines`` — a tab in a value is rejected either way."""
    with pytest.raises(ValueError, match="control character"):
        rw.RenderParams.from_dict({"defines": {"A": "a\tb"}})


def test_from_dict_rejects_oversized_defines_value() -> None:
    with pytest.raises(ValueError, match="exceeding"):
        rw.RenderParams.from_dict(
            {"defines": {"A": "x" * (rw.DEFINES_VALUE_MAX_CHARS + 1)}}
        )


def test_from_dict_accepts_valid_defines() -> None:
    p = rw.RenderParams.from_dict({"defines": {"W": "20", "H": "10"}})
    assert p.defines == {"W": "20", "H": "10"}


def test_build_docker_argv_rejects_malformed_params_dict() -> None:
    """``build_docker_argv`` accepts a raw dict and routes it through
    ``RenderParams.from_dict`` — the control-character guard must fire
    through this path too."""
    with pytest.raises(ValueError, match="control character"):
        rw.build_docker_argv("img", "render-0123abcd", params={"defines": {"A": "a\tb"}})
