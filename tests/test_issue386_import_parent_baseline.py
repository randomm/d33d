"""Issue #386 (final): the import-parent baseline is the STORED PART
mesh's genus, not ``part_report.hole_count``.

The chat adapter (``d33d.design_loop_events``) used to pass
``part_report.hole_count`` as ``through_baseline_genus`` for a v1 edit
on an import. ``hole_count`` is ``gaps_before + genus`` — for holey.stl
that is 4 + 0 = 4 — but the design loop renders against the STORED,
REPAIRED part mesh (``{repo}/versions/{v1}/part.stl``), whose gaps
pymeshfix has closed. The baseline is therefore the genus of THAT mesh,
measured with the same ``mesh_topology`` helper the v2+ parent
measurement uses, off the event loop — never ``hole_count``. A missing
or unreadable part mesh abstains (the check passes nothing the gate
would treat as a real baseline — it never fabricates one from
``hole_count``).

All fast (stub loop, no LLM, no Docker). Real fixtures: ``holey.stl``
(4 open gaps, repaired genus 0), ``through_hole_genus1.stl`` (genus 1),
``through_hole_genus3.stl`` (genus 3, the "3-hole plate" stand-in),
``through_hole_genus4.stl`` (genus 4).
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_versions(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the versions router (mounted by the
    factory) and the default git path pointed at ``tmp_path`` so the
    per-project repos are cleaned up by pytest (the same fixture shape as
    ``tests/versioning/conftest.py`` — re-declared here so this file lives
    outside the versioning package)."""
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path

    from d33d.app import create_app

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


def _exhausted_stub_result(params: dict[str, Any]):
    """An exhausted-style duck-type result (no version creation, no
    artifact reads)."""
    from d33d.design_loop import IterationRecord, Score
    from tests.versioning.test_design_loop_finalize import _default_render

    return _ExhaustedResult(params, _default_render(), IterationRecord, Score)


class _ExhaustedResult:
    def __init__(self, params: dict[str, Any], render, IterationRecord, Score) -> None:
        self.status = "exhausted"
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 30;\ncube([W, W, W]);",
            render=render,
            score=Score(bits=(False,)*5, rank=0, tiebreak=(False,)*5),
            params=dict(params),
        )
        self.failure_reason = "bbox_out_of_tolerance"


def _capturing_loop(captured: dict[str, Any]):
    """A production-seam-shaped stub loop (takes ``app``) that captures
    the loop kwargs and returns an exhausted result (no version write)."""

    async def _loop(app: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _exhausted_stub_result(kwargs.get("state_params") or {})

    return _loop


def _drive_adapter(app, pid: int, run_loop, user_message: str = "drill a 5 mm hole through it"):
    """Drive ``run_design_loop_with_events`` for one project inside a
    lifespan; returns a coroutine (``await`` via ``run_async``)."""
    from d33d.design_loop_events import run_design_loop_with_events

    async def _drive():
        app.state.run_design_loop = run_loop
        async for _ in run_design_loop_with_events(
            app,
            pid,
            user_message=user_message,
            stated_dims=(30.0, 30.0, 30.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text=user_message,
        ):
            pass

    return _drive()


def _import_part(app, project_id: int, stl_fixture: str, hole_count: int) -> Path:
    """Write the stored, REPAIRED part mesh the way the import path does
    (``{repo}/versions/{v1}/part.stl``) plus the project's part columns
    and a ``part_report`` whose ``hole_count`` is ``hole_count`` (the
    pre-repair gaps + genus — deliberately different from the stored
    mesh's genus in the holey case: holey.stl has 4 gaps, so the report
    says 4 while the repaired stored mesh has genus 0).

    The v1 row must already exist (the caller creates it); the helper is
    sync and mutates the DB directly — the file write + row update
    mirror ``versions._run_import_create``'s fixed layout. Returns the
    stored part path."""
    conn = app.state.conn
    proj = conn.get_project(project_id)
    assert proj is not None
    repo = Path(proj["git_repo_path"])
    v1 = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (project_id,),
    ).fetchone()
    assert v1 is not None
    v1_id = v1[0]
    part_dir = repo / "versions" / str(v1_id)
    part_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(FIXTURES / stl_fixture, part_dir / "part.stl")
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, "
        "part_unit_status=?, part_scale=?, part_report=? WHERE id=?",
        (
            stl_fixture,
            "stl",
            "settled",
            1.0,
            json.dumps({"hole_count": hole_count}),
            project_id,
        ),
    )
    conn.commit()
    return part_dir / "part.stl"


def _part_project(app, client, stl_fixture: str, hole_count: int):
    """A project with an imported part (stored mesh + v1 row + part
    columns); returns a coroutine that runs under one lifespan and
    leaves the caller with the project id."""

    async def _call():
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {})
        _import_part(app, pid, stl_fixture, hole_count)
        return pid

    return _call()


def test_import_parent_baseline_is_stored_genus_not_hole_count(app_with_versions):
    """(1) A v1 edit on a ``holey.stl`` import: the report says
    ``hole_count`` 4 (4 pre-repair gaps + genus 0) but the STORED part
    mesh is the repaired one (genus 0). The adapter must carry
    ``through_baseline_genus == 0`` — the genus of the stored part —
    NOT ``hole_count`` (4). With the stale 4, a rendered correct
    through-hole (genus 1) does not exceed 4 and is wrongly failed as
    geometrically_wrong."""
    captured: dict[str, Any] = {}

    async def _call(client):
        pid = await _part_project(app_with_versions, client, "holey.stl", 4)
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
        return pid

    run_async(app_with_versions, _call)
    # The stored part's genus is 0 (the 4 gaps were repaired at import);
    # the baseline must be that, not the report's 4.
    assert captured.get("through_baseline_genus") == 0, (
        f"import-parent baseline must be the stored part's genus (0), "
        f"not the report's hole_count (4); got "
        f"{captured.get('through_baseline_genus')!r}"
    )


def test_import_parent_baseline_plate_genus(app_with_versions):
    """(2) A v1 edit on a 3-hole-plate import (stored mesh genus 3, no
    gaps — report ``hole_count`` 3): the baseline is 3. The check then
    behaves as intended downstream: a rendered pocket (genus 3) does NOT
    exceed 3 (fails) and a rendered through-hole (genus 4) DOES (passes)
    — pinned here via the baseline the adapter carries (3), so the
    same check logic from ``through_hole_check`` discriminates them."""
    captured: dict[str, Any] = {}

    async def _call(client):
        pid = await _part_project(app_with_versions, client, "through_hole_genus3.stl", 3)
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
        return pid

    run_async(app_with_versions, _call)
    assert captured.get("through_baseline_genus") == 3, (
        f"import-parent baseline must be the stored part's genus (3); "
        f"got {captured.get('through_baseline_genus')!r}"
    )

    # Downstream discrimination with baseline 3: pocket (genus 3) fails
    # (no rise), through-hole (genus 4) passes.
    from d33d.through_hole_check import through_hole_check

    pocket = str(FIXTURES / "through_hole_genus3.stl")
    through = str(FIXTURES / "through_hole_genus4.stl")
    req = "drill a 5 mm hole through it"
    assert through_hole_check(req, pocket, 3) is not None, (
        "a rendered pocket (genus 3) must NOT exceed baseline 3 — the "
        "check must fire"
    )
    assert through_hole_check(req, through, 3) is None, (
        "a rendered through-hole (genus 4) exceeds baseline 3 — the "
        "check must pass (abstain from repair)"
    )


def test_import_parent_missing_part_stl_abstains(app_with_versions):
    """(3) A v1 edit on an import whose stored part mesh is MISSING
    (out-of-band loss): the adapter must pass the ``-1`` unknown
    sentinel (``resolve_baseline_genus`` maps it to ``None`` — the check
    abstains). It must NEVER fall back to the report's ``hole_count`` —
    a fabricated baseline would make the gate lie."""
    captured: dict[str, Any] = {}

    async def _call(client):
        pid = await _part_project(app_with_versions, client, "holey.stl", 4)
        # Simulate the stored part being lost out-of-band.
        conn = app_with_versions.state.conn
        proj = conn.get_project(pid)
        v1 = conn.raw.execute(
            "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        part_stl = Path(proj["git_repo_path"]) / "versions" / str(v1[0]) / "part.stl"
        part_stl.unlink()
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
        return pid

    run_async(app_with_versions, _call)
    # The -1 sentinel → resolve_baseline_genus → None → abstain.
    # NOT 4 (hole_count — the forbidden fallback).
    assert captured.get("through_baseline_genus") == -1, (
        f"a missing stored part must abstain (the -1 unknown sentinel); "
        f"got {captured.get('through_baseline_genus')!r} — the report's "
        f"hole_count (4) must never substitute"
    )
    from d33d.through_hole_check import resolve_baseline_genus

    assert resolve_baseline_genus(captured.get("through_baseline_genus")) is None


def test_import_parent_unreadable_part_stl_abstains(app_with_versions):
    """(3, unreadable variant) A v1 edit on an import whose stored part
    mesh is present but UNREADABLE (a garbage file — a corrupt out-of-band
    overwrite): same contract — the ``-1`` unknown sentinel, never
    ``hole_count``."""
    captured: dict[str, Any] = {}

    async def _call(client):
        pid = await _part_project(app_with_versions, client, "holey.stl", 4)
        conn = app_with_versions.state.conn
        proj = conn.get_project(pid)
        v1 = conn.raw.execute(
            "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        part_stl = Path(proj["git_repo_path"]) / "versions" / str(v1[0]) / "part.stl"
        part_stl.write_bytes(b"this is not an stl file at all")
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
        return pid

    run_async(app_with_versions, _call)
    assert captured.get("through_baseline_genus") == -1, (
        f"an unreadable stored part must abstain (the -1 unknown "
        f"sentinel); got {captured.get('through_baseline_genus')!r}"
    )
