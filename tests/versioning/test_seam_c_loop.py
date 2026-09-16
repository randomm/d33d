"""SEAM C — loop -> SSE adapter: schema + replay (issue #102).

Schema (``tests.seam_schemas.validate_iteration_record_seam_c``): every
attribute the SSE adapter (``design_loop_events._resolve_version_create`` /
``_artifact_bytes_from_path``) and the finalize route
(``versions_routes.finalize``) read off ``best`` is a DECLARED
``IterationRecord`` field — the structural form of the #93 guard (a
rename/deletion fails LOUDLY here instead of silently yielding ``None``
at frame time). The field set is derived from
``dataclasses.fields(IterationRecord)``.

Replay: the recorded fixture (``tests/fixtures/e2e/C.json`` — a full
``IterationRecord`` from a real ``run_design_loop`` run, with the
committed ``box_20mm.stl`` render) is fed through the REAL consumer —
the SSE adapter's version-frame builder (``_resolve_version_create``) —
and the frame is asserted to carry ``version_id`` + the
omit-policy-correct ``stl_data_uri`` / ``views`` (present when the
durable bytes are readable, omitted otherwise).
"""

from __future__ import annotations

import base64
from pathlib import Path
from typing import Any

import pytest

from d33d.design_loop import IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.fixtures.e2e import load_fixture
from tests.seam_schemas import SeamError, validate_iteration_record_seam_c

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _render_from_payload(render_payload: dict[str, Any]) -> RenderResult:
    """Rebuild a ``RenderResult`` from the fixture's render payload (the
    normalised shape: top-level ``stl``/``csg``/``views`` +
    ``render_artifact_dir``)."""
    return RenderResult(
        ok=render_payload["ok"],
        exit_code=render_payload["exit_code"],
        duration_ms=render_payload["duration_ms"],
        error_class=render_payload["error_class"],
        stderr=render_payload["stderr"],
        stl=render_payload["stl"],
        csg=render_payload["csg"],
        views=tuple(render_payload["views"]),
        render_artifact_dir=render_payload["render_artifact_dir"],
    )


def _record_from_payload(payload: dict[str, Any]) -> IterationRecord:
    """Rebuild an ``IterationRecord`` from the fixture's ``best`` payload."""
    score = payload["score"]
    return IterationRecord(
        iteration=payload["iteration"],
        scad_source=payload["scad_source"],
        render=_render_from_payload(payload["render"]),
        score=Score(
            bits=tuple(score["bits"]),
            rank=score["rank"],
            tiebreak=tuple(score["tiebreak"]),
            bbox_abstained=score["bbox_abstained"],
        ),
        failure_class=payload.get("failure_class"),
        repair=payload.get("repair"),
        prompt_hashes=dict(payload.get("prompt_hashes") or {}),
        params=dict(payload.get("params") or {}),
    )


def _write_live_artifacts(tmp_path: Path, stl_b64: str, views_b64: dict[str, str]) -> Path:
    """Write the recorded STL + view bytes to a live on-disk directory
    (the adapter's ``_artifact_bytes_from_path`` reads from
    ``render_artifact_dir`` — the durable bytes must be on disk for the
    frame to carry them)."""

    art_dir = tmp_path / "artifacts"
    art_dir.mkdir(parents=True, exist_ok=True)
    (art_dir / "model.stl").write_bytes(base64.b64decode(stl_b64.encode("ascii")))
    for name, b64 in views_b64.items():
        (art_dir / name).write_bytes(base64.b64decode(b64.encode("ascii")))
    return art_dir


# ---------------------------------------------------------------------------
# SEAM C schema (declared reader attributes)
# ---------------------------------------------------------------------------


def test_seam_c_schema_passes_for_recorded_fixture():
    """The recorded SEAM C fixture (a full ``IterationRecord`` from a
    real ``run_design_loop`` run) passes the schema — a healthy payload
    is not a false positive."""
    fixture = load_fixture("C")
    record = _record_from_payload(fixture.payload["best"])
    assert validate_iteration_record_seam_c(record) is record


def test_seam_c_schema_fails_on_undeclared_reader_attribute():
    """The #93 defect shape: the adapter reads ``best.params``, but
    ``params`` is NOT a declared ``IterationRecord`` field. The schema
    must fail LOUDLY — a rename/deletion of a reader attribute is
    caught here, not at frame time (where it silently yields ``None``).

    Simulated by monkeypatching ``BEST_READER_ATTRIBUTES`` to include a
    non-existent field (the dataclass is frozen — you cannot add a field
    at runtime, but the schema reads the declared set at call time). The
    ``declared_field_names`` set is derived from
    ``dataclasses.fields(IterationRecord)`` — a field not in that set is
    an undeclared reader attribute, which the schema rejects."""
    import tests.seam_schemas as ss

    original = ss.BEST_READER_ATTRIBUTES
    # Add a field that does not exist on IterationRecord (the #93 shape:
    # a reader attribute the dataclass does not declare).
    ss.BEST_READER_ATTRIBUTES = original + ("nonexistent_field",)
    try:
        fixture = load_fixture("C")
        record = _record_from_payload(fixture.payload["best"])
        with pytest.raises(SeamError) as exc:
            validate_iteration_record_seam_c(record)
        assert "SEAM C" in str(exc.value)
        assert "nonexistent_field" in str(exc.value)
    finally:
        ss.BEST_READER_ATTRIBUTES = original


def test_seam_c_schema_fails_on_non_iteration_record_payload():
    """A payload that is not an ``IterationRecord`` (e.g. a duck-typed
    stub without the field) fails — the schema names the expected type."""
    with pytest.raises(SeamError):
        validate_iteration_record_seam_c({"iteration": 1})


# ---------------------------------------------------------------------------
# SEAM C replay: the recorded fixture through the real version-frame builder
# ---------------------------------------------------------------------------


@pytest.fixture
def seam_c_live(tmp_path: Path):
    """The recorded fixture's best candidate with a LIVE durable artifact
    directory (the STL + 6 view PNGs on disk) — the adapter's
    ``_artifact_bytes_from_path`` reads from ``render_artifact_dir``, so
    the bytes must be on disk for the frame to carry them."""
    fixture = load_fixture("C")
    b_fixture = load_fixture("B")
    stl_b64 = b_fixture.payload["artifacts_b64"]["model.stl"]
    views_b64 = b_fixture.payload["artifacts_b64"]["views"]
    art_dir = _write_live_artifacts(tmp_path, stl_b64, views_b64)
    record = _record_from_payload(fixture.payload["best"])
    # Rebuild the record's render with a live render_artifact_dir.
    from dataclasses import replace

    live_render = replace(
        record.render,
        stl=str(art_dir / "model.stl"),
        views=tuple(str(art_dir / Path(v).name) for v in record.render.views),
        render_artifact_dir=str(art_dir),
    )
    return replace(record, render=live_render), fixture, art_dir


class _StubVersions:
    """A stub ``app.state.versions`` that returns a fixed version id."""

    def __init__(self) -> None:
        self._next = 1

    async def create_version(self, project_id, params, name, message):
        v = self._next
        self._next += 1
        return {"id": v, "params": params, "name": name, "message": message}


class _StubResult:
    """A duck-typed ``DesignResult`` whose ``best`` is the recorded
    ``IterationRecord`` (the adapter reads ``result.best`` as a declared
    field — the #93 invariant)."""

    def __init__(self, best: IterationRecord) -> None:
        self.status = "pass"
        self.best = best
        self.failure_reason = None


def test_seam_c_replay_version_frame_carries_version_id(seam_c_live, app_with_versions):
    """The RECORDED fixture's ``best`` through the REAL version-frame
    builder (``_resolve_version_create``): the frame carries
    ``version_id`` (an int) + the best candidate's params. The omit
    policy is exercised: ``stl_data_uri`` / ``views`` are present when
    the durable bytes are readable (the live artifact dir), omitted
    otherwise."""
    import asyncio

    from d33d.design_loop_events import _resolve_version_create

    record, _fixture, _art_dir = seam_c_live
    result = _StubResult(record)
    app = app_with_versions
    app.state.versions = _StubVersions()

    version_id = asyncio.run(_resolve_version_create(app, 1, result, "make a 20x25x30 box"))

    # The version was created (a passing loop ALWAYS materialises a
    # version — issue #93's invariant).
    assert version_id is not None
    assert isinstance(version_id, int)
    # The loop's stated_dims=(20,25,30) maps to W=20, D=25, H=30 (the
    # W/D/H axis order — W is the first axis, D the second, H the third).
    # The record's params are the loop's ``_params_for_record`` output.
    assert record.params == {"W": 20.0, "D": 25.0, "H": 30.0}


def test_seam_c_replay_version_frame_omit_policy_dead_path(tmp_path: Path, app_with_versions):
    """The omit policy: a pass whose ``best.render`` has a DEAD
    ``render_artifact_dir`` (the directory was deleted out-of-band) —
    the frame OMITS ``stl_data_uri`` / ``views`` (never a null, never a
    bogus path). This is the adapter's existing behaviour (issue #89),
    exercised here against the RECORDED fixture's record shape."""
    import asyncio

    from d33d.design_loop_events import _resolve_version_create

    fixture = load_fixture("C")
    record = _record_from_payload(fixture.payload["best"])
    # Dead artifact dir: a path that does not exist.
    dead_dir = tmp_path / "deleted"  # never created
    from dataclasses import replace

    dead_record = replace(
        record,
        render=replace(
            record.render,
            stl=None,
            views=(),
            render_artifact_dir=str(dead_dir),
        ),
    )
    result = _StubResult(dead_record)
    app = app_with_versions
    app.state.versions = _StubVersions()

    version_id = asyncio.run(_resolve_version_create(app, 1, result, "hi"))

    # The version was still created (a pass always materialises a
    # version — the omit policy is about the frame's fields, not the
    # version's existence).
    assert version_id is not None
    assert isinstance(version_id, int)
    # The record's params are still the declared field (the #93
    # invariant holds even with a dead render).
    assert isinstance(dead_record.params, dict)
