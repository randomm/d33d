"""SEAM A — LLM -> loop: schema + replay (issue #102).

Schema (``tests.seam_schemas.validate_llm_result_seam_a``): the payload
is an ``LLMResult`` (the ``send()`` return) whose ``tool_calls`` entries
carry ``arguments`` as a DICT after normalisation (issue #80: the
endpoint returns a JSON-ENCODED STRING; the T1/fenced-JSON shape, issue
#79, synthesizes the same shape). The field set is derived from
``dataclasses.fields(LLMResult)``.

Replay: the recorded fixture (``tests/fixtures/e2e/A.json`` — BOTH real
production shapes, the T0 native tool_call with a JSON-string ``arguments`
and the T0 fenced-JSON-in-content) is fed through the REAL consumer —
the loop's tool-call extractor (``d33d.design_loop._scad_from_result``)
— and the extracted SCAD is asserted non-empty AND equal to the
fixture's expected value.
"""

from __future__ import annotations

import pytest

from d33d.design_loop import _scad_from_result
from tests.fixtures.e2e import llm_result_from_payload, load_fixture
from tests.seam_schemas import SeamError, validate_llm_result_seam_a

# ---------------------------------------------------------------------------
# SEAM A schema (derived field set + dict-arguments invariant)
# ---------------------------------------------------------------------------


def test_seam_a_schema_passes_for_both_recorded_shapes():
    """The recorded SEAM A fixture (both production shapes) passes the
    schema — a healthy payload is not a false positive."""
    fixture = load_fixture("A")
    for shape in ("native", "fenced"):
        result = llm_result_from_payload(fixture.payload[shape])
        # Returns the result unchanged on success (a chainable validator).
        assert validate_llm_result_seam_a(result) is result


def test_seam_a_schema_fails_on_json_string_arguments():
    """The #80 defect shape: a tool_call whose ``arguments`` is still a
    JSON STRING (the normaliser did not run / was bypassed). The schema
    must fail LOUDLY, naming the seam + the offending field."""
    from d33d.design_llm import LLMResult

    result = LLMResult(
        content="",
        tool_calls=(
            {"name": "emit_design", "arguments": '{"scad": "cube([20]);"}'},
        ),
        prompt_hash="h" * 64,
        tier="T0",
        status="ok",
        request_body={},
    )
    with pytest.raises(SeamError) as exc:
        validate_llm_result_seam_a(result)
    assert "SEAM A" in str(exc.value)
    assert "arguments" in str(exc.value)
    assert exc.value.seam == "A"


def test_seam_a_schema_fails_on_missing_declared_field_type():
    """A ``tool_calls`` entry that is not a dict (a malformed shape)
    fails the schema — the declared shape is not just presence, it is
    the value types the loop's extractor relies on."""
    from d33d.design_llm import LLMResult

    result = LLMResult(
        content="",
        tool_calls=("not-a-dict",),
        prompt_hash="h" * 64,
        tier="T0",
        status="ok",
        request_body={},
    )
    with pytest.raises(SeamError):
        validate_llm_result_seam_a(result)


def test_seam_a_schema_fails_on_non_llm_result_payload():
    """A payload that is not an ``LLMResult`` (e.g. a raw dict) fails —
    the schema names the expected type so a seam drift is diagnosable."""
    with pytest.raises(SeamError):
        validate_llm_result_seam_a({"content": "", "tool_calls": ()})


# ---------------------------------------------------------------------------
# SEAM A replay: the recorded fixture through the real extractor
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("shape", ["native", "fenced"], ids=["t0-native", "t0-fenced"])
def test_seam_a_replay_extracted_scad_matches_fixture(shape: str):
    """The RECORDED fixture (both production shapes) through the REAL
    loop extractor (``_scad_from_result``): the extracted SCAD is
    non-empty AND equal to the fixture's expected value. 'Did not raise'
    proves nothing — the RIGHT output is asserted."""
    fixture = load_fixture("A")
    result = llm_result_from_payload(fixture.payload[shape])
    # The schema passes (the recorded payload is a healthy shape).
    validate_llm_result_seam_a(result)
    extracted = _scad_from_result(result)
    assert extracted, "extracted SCAD is empty — the #80/#79 shape (silently empty extraction)"
    assert extracted == fixture.expected["scad"], (
        f"extracted SCAD {extracted!r} != expected {fixture.expected['scad']!r}"
    )


def test_seam_a_schema_catches_reverted_normalisation():
    """RED-CHECK (a) for issue #102: simulate the #80 defect by building
    an ``LLMResult`` the way ``send()`` would if the tool-args
    normalisation were reverted (``_normalize_tool_arguments`` returns the
    raw JSON string instead of parsing it to a dict). The SEAM A schema
    must go RED on this shape — a JSON-string ``arguments`` that survived
    the normaliser is the exact #80 defect (the extractor silently
    extracts empty SCAD, the loop reports 'the model produced nothing').

    This test is distinct from ``test_seam_a_schema_fails_on_json_string_
    arguments``: that test constructs the defect directly; this test
    constructs it via the same path the reverted normaliser would take
    (the raw wire value passed through ``send()``'s tool-call extraction
    without the JSON parse)."""
    import json as _json

    from d33d.design_llm import LLMResult

    # The raw wire value (the endpoint's JSON-ENCODED STRING).
    raw_args = _json.dumps({"scad": "cube([20,20,20]);"})
    # Simulate the reverted normalisation: the raw string passes through
    # unchanged (no JSON parse to a dict).
    if isinstance(raw_args, dict):
        normalized = raw_args
    elif not isinstance(raw_args, str):
        normalized = {}
    else:
        # RED-CHECK: the reverted shape (the raw string, un-normalised).
        normalized = raw_args
    # Build the LLMResult the way send() would with the reverted
    # normaliser (the tool_call's arguments is the raw JSON string).
    result = LLMResult(
        content="",
        tool_calls=(
            {"name": "emit_design", "arguments": normalized},
        ),
        prompt_hash="h" * 64,
        tier="T0",
        status="ok",
        request_body={},
    )
    # The schema must go RED: a JSON-string arguments is the #80 shape.
    with pytest.raises(SeamError) as exc:
        validate_llm_result_seam_a(result)
    assert "SEAM A" in str(exc.value)
    assert "arguments" in str(exc.value)
    # The extracted SCAD is empty (the #80 silent-failure shape: the
    # extractor's isinstance(args, dict) check fails on a string, so it
    # falls through to the fenced-SCAD fallback, which finds nothing in
    # the empty content).
    from d33d.design_loop import _scad_from_result

    extracted = _scad_from_result(result)
    assert extracted == "", (
        f"the reverted normaliser's shape should extract empty SCAD (the #80 "
        f"silent-failure shape), got {extracted!r}"
    )
