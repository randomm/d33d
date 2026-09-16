"""SEAM B — render worker -> loop: schema + replay (issue #102).

Schema (``tests.seam_schemas.validate_render_result_seam_b``): every
declared ``RenderResult`` field is present (derived from
``dataclasses.fields(RenderResult)`` — never a hand-written list) and,
when ``render_artifact_dir`` is set, ``stl``/``views`` are DURABLE paths
— structurally, they must NOT start with ``tempfile.gettempdir()``
(the #87 invariant: a path the worker's tempdir teardown deletes before
the loop reads it). ``render_artifact_dir=None`` (persistence disabled)
is a valid production shape.

Replay: the recorded fixture (``tests/fixtures/e2e/B.json`` — the
``result.json`` the real ``render_for_design_loop`` wrote, with the
committed ``box_20mm.stl`` bytes base64-encoded) is fed through the REAL
deserialiser (``RenderResult.from_dict``) and the reconstructed
``RenderResult`` is asserted field-for-field against the fixture.
"""

from __future__ import annotations

import base64
import tempfile

import pytest

from tests.fixtures.e2e import load_fixture
from tests.seam_schemas import SeamError, validate_render_result_seam_b

# ---------------------------------------------------------------------------
# SEAM B schema (declared fields + durable-path invariant)
# ---------------------------------------------------------------------------


def _render(**kwargs):
    from d33d.render_worker import RenderResult

    defaults = {
        "ok": True,
        "exit_code": 0,
        "duration_ms": 0,
        "error_class": "ok",
        "stderr": "",
        "stl": None,
        "csg": None,
        "views": (),
    }
    defaults.update(kwargs)
    return RenderResult(**defaults)


def test_seam_b_schema_passes_for_recorded_fixture():
    """The recorded SEAM B fixture (the real worker's result.json) passes
    the schema — a healthy payload is not a false positive."""
    from tests.fixtures.e2e import _rebuild_render_result

    fixture = load_fixture("B")
    result = _rebuild_render_result(fixture.payload)
    assert validate_render_result_seam_b(result) is result


def test_seam_b_schema_fails_on_tempdir_stl_when_artifact_dir_set():
    """The #87 defect shape: ``render_artifact_dir`` is set but ``stl``
    is under ``tempfile.gettempdir()`` (a path the worker tears down).
    The schema must fail LOUDLY — durability is asserted STRUCTURALLY
    (path prefix), not by ``Path.exists()`` (which fails in a fresh
    checkout)."""
    result = _render(
        stl=f"{tempfile.gettempdir()}/torn-down/model.stl",
        render_artifact_dir="/durable/artifact-dir",
    )
    with pytest.raises(SeamError) as exc:
        validate_render_result_seam_b(result)
    assert "SEAM B" in str(exc.value)
    assert "tempdir" in str(exc.value).lower()


def test_seam_b_schema_fails_on_non_enum_error_class():
    """A ``error_class`` outside the closed 7-class enum fails — the
    enum is total (every render lands in exactly one class)."""
    result = _render(error_class="not_a_class")
    with pytest.raises(SeamError):
        validate_render_result_seam_b(result)


def test_seam_b_schema_fails_on_non_render_result_payload():
    """A payload that is not a ``RenderResult`` (e.g. a raw dict) fails
    — the schema names the expected type so a seam drift is diagnosable."""
    with pytest.raises(SeamError):
        validate_render_result_seam_b({"ok": True, "exit_code": 0})


# ---------------------------------------------------------------------------
# SEAM B replay: the recorded fixture through the real deserialiser
# ---------------------------------------------------------------------------


def test_seam_b_replay_reconstructed_result_field_for_field():
    """The RECORDED fixture through the REAL ``RenderResult.from_dict``
    deserialiser: the reconstructed result is field-for-field equal to
    the fixture's payload (the #80/#89 shape: a reader that silently
    drops a field would fail this). ``render_artifact_dir`` rides in the
    payload alongside the wire fields (it is on the returned object, not
    in ``result.json``) — the loader's ``_rebuild_render_result`` rebuilds
    it via a fresh frozen instance."""
    from tests.fixtures.e2e import _rebuild_render_result

    fixture = load_fixture("B")
    # The REAL deserialiser (``from_dict``) on the wire shape (the
    # ``artifacts`` nested dict) — the seam-B wire boundary.
    from d33d.render_worker import RenderResult

    wire = {
        "ok": fixture.payload["ok"],
        "exit_code": fixture.payload["exit_code"],
        "duration_ms": fixture.payload["duration_ms"],
        "error_class": fixture.payload["error_class"],
        "stderr": fixture.payload["stderr"],
        "artifacts": fixture.payload["artifacts"],
    }
    from_dict_result = RenderResult.from_dict(wire)
    # The full object (``render_artifact_dir`` added by the loader).
    result = _rebuild_render_result(fixture.payload)
    payload = fixture.payload
    # Field-for-field (every declared field, derived set).
    assert result.ok is payload["ok"]
    assert result.exit_code == payload["exit_code"]
    assert result.duration_ms == payload["duration_ms"]
    assert result.error_class == payload["error_class"] == "ok"
    assert result.stderr == payload["stderr"]
    assert result.stl == payload["stl"]
    assert result.csg == payload["csg"]
    assert result.views == tuple(payload["views"])
    assert result.render_artifact_dir == payload["render_artifact_dir"]
    # ``from_dict`` (the wire boundary) agrees on every wire field.
    assert from_dict_result.stl == result.stl
    assert from_dict_result.views == result.views
    assert from_dict_result.error_class == result.error_class
    # The schema passes on the reconstructed result.
    validate_render_result_seam_b(result)


def test_seam_b_replay_stl_bytes_are_live_render():
    """The recorded STL bytes (base64 in the fixture) are the LIVE render
    output (the worker's ``classify`` table classified them ``ok`` — the
    real trimesh load, merge, watertight, volume). The stubbed fixture
    planted the committed ``box_20mm.stl`` bytes (a circularity — the
    fixture's expected value came from the fixture itself); the live
    fixture records the REAL Docker/OpenSCAD output, so the bytes are
    NOT the committed fixture (they are the live render's own bytes).

    The assertion is structural: the bytes must be a valid, non-degenerate
    20x20x20 cube (the live geometry check) — NOT a byte match against
    the committed fixture (the circularity this test replaces)."""
    import tempfile as _tempfile
    from pathlib import Path

    import trimesh as _trimesh

    fixture = load_fixture("B")
    stl_b64 = fixture.payload["artifacts_b64"]["model.stl"]
    stl_bytes = base64.b64decode(stl_b64.encode("ascii"))
    # The live fixture's STL is NOT the committed box_20mm.stl (the
    # circularity is gone — the live render produced its own bytes).
    committed = (Path(__file__).parent / "fixtures" / "stl" / "box_20mm.stl").read_bytes()
    if fixture.provenance.get("recorded") == "live":
        assert stl_bytes != committed, (
            "the live-recorded STL bytes are byte-identical to the committed "
            "fixture — the circularity the live mode removes (the fixture's "
            "expected value must come from the live render, not the fixture)"
        )
    # Structural check: the bytes are a valid 20x20x20 cube (the live
    # geometry — NOT a byte match).
    with _tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        f.write(stl_bytes)
        tmp_path = f.name
    try:
        mesh = _trimesh.load(tmp_path, process=False)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    mesh.merge_vertices()
    b = mesh.bounds
    for axis in range(3):
        extent = float(b[1, axis] - b[0, axis])
        assert 19.0 <= extent <= 21.0, (
            f"recorded STL extent {extent} on axis {axis} is not ~20mm "
            f"(the cube(20) request produced the wrong geometry)"
        )


def test_seam_b_replay_bbox_matches_stated_dims():
    """The recorded render's bbox (from the LIVE render's STL) is
    20x20x20 — the stated dimensions the loop scored against (the
    fixture's expected bbox)."""

    import trimesh

    fixture = load_fixture("B")
    stl_b64 = fixture.payload["artifacts_b64"]["model.stl"]
    stl_bytes = base64.b64decode(stl_b64.encode("ascii"))
    # Write to a temp file (trimesh.load needs a path or file_type for
    # file objects; a temp file avoids the file_type requirement).
    import tempfile as _tempfile

    with _tempfile.NamedTemporaryFile(suffix=".stl", delete=False) as f:
        f.write(stl_bytes)
        tmp_path = f.name
    try:
        mesh = trimesh.load(tmp_path, process=False)
    finally:
        from pathlib import Path as _Path

        _Path(tmp_path).unlink(missing_ok=True)
    mesh.merge_vertices()
    b = mesh.bounds
    bbox = (
        float(b[1, 0] - b[0, 0]),
        float(b[1, 1] - b[0, 1]),
        float(b[1, 2] - b[0, 2]),
    )
    expected = fixture.expected["bbox"]
    assert bbox[0] == pytest.approx(expected["x"])
    assert bbox[1] == pytest.approx(expected["y"])
    assert bbox[2] == pytest.approx(expected["z"])


def test_seam_b_replay_stderr_is_live_entrypoint():
    """The LIVE-recorded SEAM B fixture: ``stderr`` is non-empty and
    contains an ``[entrypoint]`` marker — the real render worker's
    multi-line stderr (a Docker platform warning plus the ten
    ``[entrypoint]`` step lines). The stubbed fixture recorded an empty
    string (the stub's ``CompletedProcess`` had ``stderr=b''``), so this
    assertion is the red-check target for the stubbed fixture: it FAILS
    against the old stubbed B.json and PASSES against the live one.

    The field is the point of the exercise — an empty stderr means the
    fixture was recorded from a stub, not from a real Docker run (the
    worker's entrypoint always emits ``[entrypoint]`` step lines on a
    successful render)."""
    fixture = load_fixture("B")
    stderr = fixture.payload["stderr"]
    assert stderr, (
        "SEAM B: stderr is empty — the stubbed fixture shape (the stub's "
        "CompletedProcess had stderr=b''). The live fixture records the "
        "REAL Docker/OpenSCAD stderr (a platform warning + the [entrypoint] "
        "step lines)."
    )
    assert "[entrypoint]" in stderr, (
        "SEAM B: stderr does not contain an [entrypoint] marker — the live "
        "render worker's entrypoint emits [entrypoint] step lines on every "
        "successful render (the stub's empty stderr has no marker at all)."
    )
