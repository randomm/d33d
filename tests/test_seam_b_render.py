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


def test_seam_b_replay_stl_bytes_are_committed_fixture():
    """The recorded STL bytes (base64 in the fixture) are the committed
    ``box_20mm.stl`` fixture — the worker's ``classify`` table classified
    them ``ok`` (the real trimesh load, merge, watertight, volume)."""
    from pathlib import Path

    fixture = load_fixture("B")
    stl_b64 = fixture.payload["artifacts_b64"]["model.stl"]
    stl_bytes = base64.b64decode(stl_b64.encode("ascii"))
    committed = (Path(__file__).parent / "fixtures" / "stl" / "box_20mm.stl").read_bytes()
    assert stl_bytes == committed, "the recorded STL bytes are not the committed fixture"


def test_seam_b_replay_bbox_matches_stated_dims():
    """The recorded render's bbox (from the committed box_20mm.stl) is
    20x25x30 — the stated dimensions the loop scored against (the
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
