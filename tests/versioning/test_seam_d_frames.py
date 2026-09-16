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

import base64

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
# SEAM D replay: the recorded fixture through the schema
# ---------------------------------------------------------------------------


def test_seam_d_replay_frame_stream_shape():
    """The RECORDED fixture's frame stream: every frame is valid (kind
    tag + data-field presence/absence per the derived shape), the stream
    terminates with a terminal ``done`` frame, and the version-created
    frame carries the REAL committed STL bytes (the ``stl_data_uri``
    decodes to the committed ``box_20mm.stl``) + the 6 view PNGs."""
    from pathlib import Path

    fixture = load_fixture("D")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)

    # The stream terminates with a terminal done frame.
    assert validated[-1][0] == "done"
    assert fixture.expected["terminal"] == "done"

    # The version-created frame: the derived field set is present.
    vc = [
        f for f in validated if f[0] == "progress" and f[1].get("step") == "version-created"
    ]
    assert vc, "no version-created frame in the recorded stream"
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
    # stl_data_uri decodes to the committed box_20mm.stl bytes.
    stl_uri = vc_data["stl_data_uri"]
    assert stl_uri.startswith("data:")
    stl_bytes = base64.b64decode(stl_uri.split(";base64,", 1)[1])
    committed = (Path(__file__).parent.parent / "fixtures" / "stl" / "box_20mm.stl").read_bytes()
    assert stl_bytes == committed, "stl_data_uri is not the committed box_20mm.stl"
    # views: all 6 view PNGs, each a valid data URI.
    views = vc_data["views"]
    assert len(views) == 6
    for name, uri in views.items():
        assert uri.startswith("data:image/png;base64,"), f"view {name} is not a PNG data URI"


def test_seam_d_replay_done_frame_message():
    """The recorded terminal ``done`` frame carries the pass message
    (``Design loop passed validation``) + the ``bbox_abstained`` flag
    (the ticket #91 invariant: the done frame carries the flag too)."""
    fixture = load_fixture("D")
    frames = [(f["kind"], f["data"]) for f in fixture.payload["frames"]]
    validated = validate_frames_stream(frames)
    done = validated[-1]
    assert done[0] == "done"
    assert done[1]["message"] == "Design loop passed validation"
    # The done frame carries the bbox_abstained flag (the ticket #91
    # invariant — both the version-created and done frames carry it).
    assert "bbox_abstained" in done[1]
    assert isinstance(done[1]["bbox_abstained"], bool)
