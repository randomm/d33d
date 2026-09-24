"""SEAM D — SSE adapter -> browser: schema + replay (issue #102).

Schema (``tests.seam_schemas_d.validate_frame`` /
``validate_frames_stream``): the frame kind is one of the four
(``progress`` / ``token`` / ``done`` / ``error``); the version-created
frame's field set is DERIVED from the live
``run_design_loop_with_events`` construction (``step`` / ``version_id`` /
``bbox_abstained`` always present; ``stl_data_uri`` / ``views`` present
iff the durable bytes were readable — the omit-not-null policy). The
browser is NOT drivable from a test: replay asserts frame SHAPE (kind
tag + data-field presence/absence), not rendering.

Replay: the recorded fixture (``tests/fixtures/e2e/D.json`` — a full
frame stream from the REAL ``run_design_loop_with_events`` adapter,
with the committed ``box_20mm.stl`` bytes as data URIs) is validated
against the schema (every frame valid, the stream terminates with a
terminal ``done`` frame).
"""

from __future__ import annotations

import pytest

from tests.fixtures.e2e import load_fixture
from tests.seam_schemas import SeamError
from tests.seam_schemas_d import (
    validate_frame,
    validate_frames_stream,
    version_created_frame_fields,
)

# ---------------------------------------------------------------------------
# SEAM D schema (derived field set + omit policy)
# ---------------------------------------------------------------------------


def test_seam_d_schema_passes_for_recorded_fixture():
    """The recorded SEAM D fixture (a full frame stream from the real
    adapter) passes the schema — a healthy stream is not a false
    positive."""
    fixture = load_fixture("D")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    assert validate_frames_stream(frames)


def test_seam_d_schema_fails_on_bad_kind():
    """A frame kind outside the closed four-set fails — the schema names
    the offending kind."""
    with pytest.raises(SeamError) as exc:
        validate_frame(("bogus", {}))
    assert "SEAM D" in str(exc.value)
    assert "bogus" in str(exc.value)


def test_seam_d_schema_fails_on_null_stl_data_uri():
    """The omit-not-null policy: a version-created frame with
    ``stl_data_uri=None`` (a null instead of an omit) fails — the
    adapter's contract is to OMIT the field, never emit a null."""
    with pytest.raises(SeamError) as exc:
        validate_frame(
            (
                "progress",
                {
                    "step": "version-created",
                    "version_id": 1,
                    "bbox_abstained": False,
                    "stl_data_uri": None,
                },
            )
        )
    assert "stl_data_uri" in str(exc.value)
    assert "null" in str(exc.value)


def test_seam_d_schema_fails_on_missing_version_id():
    """A version-created frame missing ``version_id`` fails — the derived
    field set requires it (the version was created, the id is the
    consumer's join key). ``version_id`` is in the derived set (the
    ``vc_frame`` dict-literal key), and ``validate_frame`` asserts it is
    present + an int — a missing key fails the ``isinstance`` check."""
    with pytest.raises(SeamError):
        validate_frame(
            (
                "progress",
                {
                    "step": "version-created",
                    "bbox_abstained": False,
                    # version_id is MISSING — the derived set requires it.
                },
            )
        )


def test_seam_d_schema_fails_on_non_terminal_stream():
    """A frame stream that does not terminate with a ``done``/``error``
    frame fails — the client resolves only on a terminal frame (the
    adapter's contract)."""
    with pytest.raises(SeamError):
        validate_frames_stream([("progress", {"step": "design-loop-start"})])


def test_seam_d_derived_field_set_includes_step_and_bbox():
    """The derived version-created field set (from the live
    ``run_design_loop_with_events`` construction) includes ``step`` and
    ``bbox_abstained`` — the two fields the stale spec inventory omitted
    (the ticket's own subject matter)."""
    fields = version_created_frame_fields()
    assert "step" in fields, "step is the frame's discriminator — always present"
    assert "bbox_abstained" in fields, (
        "bbox_abstained is the ticket #91 flag — always present on pass frames; "
        "the stale spec inventory omitted it"
    )
    assert "version_id" in fields
    assert "stl_data_uri" in fields
    assert "views" in fields


# ---------------------------------------------------------------------------
# SEAM D replay: the answer-path fixture (E.json, issue #249)
#
# E.json is the answer-path counterpart to D.json: a full recorded frame
# stream for the question-answer done path (the ticket #249 pre-route
# "the Brief can answer it" branch). It is a SINGLE terminal done frame
# carrying the answer text as ``message`` plus the additive ``kind: "answer"
# `` discriminator — NO token frames, NO version-created progress frame,
# NO version. D.json (the pass-with-version stream) stays the design-loop
# reference; E.json pins the answer-path wire shape against the same
# schema.
# ---------------------------------------------------------------------------


def test_seam_d_schema_passes_for_answer_path_fixture():
    """The recorded SEAM E fixture (the answer-path frame stream) passes
    the schema — the additive ``kind: "answer"`` field on the done frame
    does not break the wire contract, and the single-frame stream
    terminates with a terminal ``done`` frame (the client resolves on it).
    """
    fixture = load_fixture("E")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)
    # A single terminal done frame (the answer path emits nothing else).
    assert len(validated) == 1, f"expected 1 frame, got {len(validated)}"
    assert validated[0][0] == "done"
    assert fixture.expected["terminal"] == "done"


def test_seam_d_answer_path_fixture_has_no_token_or_version_created():
    """The answer-path stream carries NO ``token`` frame and NO
    ``version-created`` progress frame (the ticket's invariants: the
    answer text travels in the done frame's ``message``, not via tokens;
    no render, no version)."""
    fixture = load_fixture("E")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validate_frames_stream(frames)  # schema-valid precondition
    kinds = [k for k, _ in frames]
    assert "token" not in kinds, "the answer path emits no token frames"
    assert all(
        not (k == "progress" and d.get("step") == "version-created")
        for k, d in frames
    ), "the answer path emits no version-created progress frame"
    assert fixture.expected["version_created"] is False
    assert fixture.expected["token_frames"] == 0


def test_seam_d_answer_path_done_frame_carries_kind_and_message():
    """The recorded answer-path done frame carries the answer text as
    ``message`` (a non-empty string — the schema's required field) AND
    the additive ``kind: "answer"`` discriminator (the SPA's
    verbatim-render branch — a design-loop done frame has no ``kind``
    at all)."""
    fixture = load_fixture("E")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)
    _kind, data = validated[-1]
    assert data["message"], "the answer text is the done frame's message"
    assert data["kind"] == "answer"
    assert fixture.expected["done_kind"] == "answer"
    # The state block the stubbed answer call was driven against (the
    # guard's source of truth): H is the STATED 12.0 (the ticket's
    # example — "you said that"), W/D are the assumed 20.0.
    block = fixture.payload["state_block"]
    by_name = {e["name"]: e for e in block}
    assert by_name["H"]["value"] == 12.0
    assert by_name["H"]["provenance"] == "stated"
    assert by_name["W"]["provenance"] == "assumed"
    assert by_name["D"]["provenance"] == "assumed"


def test_seam_d_answer_path_message_passes_number_guard():
    """The recorded answer's numbers all appear in the recorded state
    block (the deterministic guard's presence-only check — the fixture
    is self-consistent: nothing the recorded model said is invented)."""
    from d33d.question_answer import guard_answer_numbers

    fixture = load_fixture("E")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    _kind, data = validate_frames_stream(frames)[-1]
    block = fixture.payload["state_block"]
    assert guard_answer_numbers(data["message"], block), (
        "the recorded answer contains a number absent from the recorded "
        "state block — the guard would have vetoed this wire payload"
    )


# ---------------------------------------------------------------------------
# SEAM D replay: the recorded fixture through the schema
# ---------------------------------------------------------------------------


def test_seam_d_replay_frame_stream_shape():
    """The RECORDED fixture's frame stream: every frame is valid (kind
    tag + data-field presence/absence per the derived shape), the stream
    terminates with a terminal frame, and — when the recorded loop
    passed — the version-created frame carries the REAL STL bytes (the
    ``stl_data_uri`` decodes to the recorded render's STL) + the 6 view
    PNGs.

    The terminal frame is asserted per the recorded outcome (``done`` for
    a pass, ``error`` for an exhausted loop — the loop outcome is a
    property of the stubbed edges, not a hardcoded pass), so this test
    validates the ADAPTER's frame construction (the frame shape, the
    omit-not-null policy, the data-URI encoding) regardless of the loop
    outcome.

    The answer-path fixture (``E.json``) is the counterpart of this
    test's design-loop stream: a SINGLE terminal done frame with
    ``kind: "answer"`` and no version-created frame — its own replay
    tests (``test_seam_d_schema_passes_for_answer_path_fixture`` and
    siblings above) validate it.
    """
    fixture = load_fixture("D")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)

    # The stream terminates with a terminal frame (done or error — the
    # adapter's contract: the client resolves only on a terminal frame).
    terminal = validated[-1][0]
    assert terminal in ("done", "error"), f"terminal frame is {terminal!r}"
    assert fixture.expected["terminal"] == terminal, (
        f"fixture expected terminal {fixture.expected['terminal']!r}, got {terminal!r}"
    )

    # A version-created frame exists iff the recorded loop passed (the
    # adapter's pass path emits it; the exhausted path emits an error
    # frame instead — the frame construction under test is the same
    # code, the terminal frame differs).
    if terminal == "done":
        vc = [
            f for f in validated if f[0] == "progress" and f[1].get("step") == "version-created"
        ]
        assert vc, "no version-created frame in the recorded pass stream"
        assert fixture.expected["version_created"] is True
        vc_data = vc[0][1]
        derived = version_created_frame_fields()
        # Every derived field that is present carries a non-null value.
        for name in derived:
            if name in vc_data:
                assert vc_data[name] is not None, f"{name} is null (omit policy: omit, never null)"
        # version_id is an int (the consumer's join key).
        assert isinstance(vc_data["version_id"], int)
        # bbox_abstained is a bool (the ticket #91 flag).
        assert isinstance(vc_data["bbox_abstained"], bool)
        # stl_data_uri is a data URI (the recorded render's STL bytes —
        # NOT asserted against the committed box_20mm.stl: the live
        # render's bytes are the recorded reality, and the stub's bytes
        # are the committed fixture; the frame construction under test is
        # the encoding, not the byte identity).
        stl_uri = vc_data["stl_data_uri"]
        assert stl_uri.startswith("data:")
        # views: all 6 view PNGs, each a valid data URI.
        views = vc_data["views"]
        assert len(views) == 6
        for name, uri in views.items():
            assert uri.startswith("data:image/png;base64,"), f"view {name} is not a PNG data URI"
    else:
        # Exhausted: the terminal frame is an error frame (the adapter's
        # non-pass path — the omit-not-null policy for the structured
        # reason is what is under test here).
        assert fixture.expected["version_created"] is False
        error_data = validated[-1][1]
        assert "message" in error_data, "error frame missing 'message'"
        # The structured reason (when present) is a string, never null.
        if "reason" in error_data:
            assert isinstance(error_data["reason"], str), "error frame 'reason' is not a string"


def test_seam_d_replay_done_frame_message():
    """The recorded terminal ``done`` frame (when the loop passed) carries
    the pass message (``Design loop passed validation``) + the
    ``bbox_abstained`` flag (the ticket #91 invariant: the done frame
    carries the flag too). When the recorded loop exhausted, the terminal
    frame is an ``error`` frame (the adapter's non-pass path) — this test
    is a no-op in that case (the error frame is validated by
    ``test_seam_d_replay_frame_stream_shape``)."""
    fixture = load_fixture("D")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)
    done = validated[-1]
    if done[0] != "done":
        return  # exhausted: the error frame is the terminal frame
    assert done[1]["message"] == "Design loop passed validation"
    # The done frame carries the bbox_abstained flag (the ticket #91
    # invariant — both the version-created and done frames carry it).
    assert "bbox_abstained" in done[1]
    assert isinstance(done[1]["bbox_abstained"], bool)
