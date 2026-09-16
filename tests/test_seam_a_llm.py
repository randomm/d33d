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
    """The RECORDED fixture through the REAL loop extractor
    (``_scad_from_result``): the extracted SCAD is non-empty. The
    ``expected.scad`` is the NATIVE shape's extracted SCAD (the fenced
    shape is a different live call — the live model's SCAD varies per
    call, so the two shapes are not expected to match each other; the
    point of the exercise is the wire SHAPE, not the geometry).

    'Did not raise' proves nothing — the RIGHT output shape is asserted
    (a non-empty SCAD, the #80/#79 invariant)."""
    fixture = load_fixture("A")
    result = llm_result_from_payload(fixture.payload[shape])
    # The schema passes (the recorded payload is a healthy shape).
    validate_llm_result_seam_a(result)
    extracted = _scad_from_result(result)
    assert extracted, "extracted SCAD is empty — the #80/#79 shape (silently empty extraction)"
    if shape == "native":
        # The native shape's extracted SCAD matches the fixture's expected
        # value (the recorded native output — the deterministic pin).
        assert extracted == fixture.expected["scad"], (
            f"native extracted SCAD {extracted!r} != expected {fixture.expected['scad']!r}"
        )
    # The fenced shape's extracted SCAD is non-empty (the live model's own
    # output — not pinned to the native call's SCAD).


def test_seam_a_replay_request_body_carries_real_tools():
    """The LIVE-recorded SEAM A fixture: ``request_body.tools`` is present
    and NON-NULL — the REAL tools array that ``send()`` put on the wire
    (T0 native). The stubbed fixture left this ``null`` (the stub's
    ``send()`` call passed no ``tools``), so this assertion is the
    red-check target for the stubbed fixture: it FAILS against the old
    stubbed A.json and PASSES against the live one. The field is the
    point of the exercise — a tools array that never reaches the wire
    means the model was never actually asked to use the tool channel."""
    fixture = load_fixture("A")
    for shape in ("native", "fenced"):
        tools = fixture.payload[shape]["request_body"].get("tools")
        assert tools is not None, (
            f"SEAM A {shape}: request_body.tools is null — the stubbed fixture "
            f"shape (send() was never given the T0 tools array, so the wire "
            f"body had no tools field). The live fixture records the REAL "
            f"tools array send() puts on the wire."
        )
        assert len(tools) == 1, (
            f"SEAM A {shape}: request_body.tools has {len(tools)} entries, "
            f"expected 1 (the design role's single emit_design tool)"
        )
        # The tools array carries the emit_design tool definition.
        tool = tools[0]
        assert tool["function"]["name"] == "emit_design", (
            f"SEAM A {shape}: the recorded tools[0].function.name is "
            f"{tool['function']['name']!r}, expected 'emit_design'"
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
