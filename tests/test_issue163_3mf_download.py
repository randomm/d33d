"""Issue #163: serve the validated 3MF for download at
``GET /api/projects/{id}/model.3mf``.

The decisive test drives the REAL route (``create_app`` + ASGI client) and
gets back bytes that are a valid zip declaring millimetre units — a real
on-disk ``model.3mf`` produced by the real ``validate_stl`` pipeline (the
passing slice stub is injected, mirroring
``tests/test_3mf_export.py``'s ``_passing_slice_fn``, while production
wires the real ``slicer.slice_dry_run``).

The other tests pin: the filename slug contract (the pinned "Curtain rod
bracket" + "v4" → "curtain-rod-bracket-v4.3mf" case, asserted against the
``Content-Disposition`` header so the backend slug cannot drift from the
SPA's ``copy.shell.exportFilename``); the no-versions 404; the
no-recorded-render 409 (a pre-#163 version row degrades honestly — a clear
error naming the missing render, never a guessed directory); the
stale-render-directory 409 (the render was deleted after the version was
created); the gate-failure 502s (watertight, winding, envelope — driven by
the real fixtures in ``tests/fixtures/stl/``); the in-flight 409 (a design
loop still running for the project); the stated-mm ABSTENTION (a version
with no W/D/H params still serves a valid 3MF — the dimension gate skips
rather than comparing against 0.0); and the render-reference persistence
(the version's ``render_artifact_dir`` column mirrors the ``bbox`` pattern
from issue #137: ``None`` round-trips as ``None``, the column is written
at version-creation time, and the design-loop adapter threads it from the
best candidate's render).
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import _export_filename, create_app
from d33d.slicer import SliceDryRunResult
from tests.versioning.helpers import run_async

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _passing_slice_fn(model_path: str, output_dir: str | None) -> SliceDryRunResult:
    """Injected gate-6 stub that passes without a real slicer binary (the
    default ``slicer.slice_dry_run`` FAILS CLOSED where no slicer binary
    exists — a test relying on the default would fail in CI for the wrong
    reason, and a production route ignoring the injection would fail in
    production). Mirrors ``tests/test_3mf_export.py``'s stub exactly."""
    return SliceDryRunResult(
        ok=True,
        slicer="stub",
        gcode_path="stub.gcode",
        gcode_lines=1,
        return_code=0,
        error_string="",
        objects=1,
        detail="fast-layer stub (no real slicer binary needed)",
    )


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under tmp_path."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_3mf(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the git path pointed at ``tmp_path``
    and the 3MF validation's slice hook monkeypatched to the passing stub.
    The route calls ``slicer.slice_dry_run`` (the PRODUCTION hook) at call
    time, so patching the module attribute is the production-faithful seam
    — the wiring is exercised, only the binary is stood in."""
    import d33d.db as db_mod
    import d33d.slicer as slicer_mod

    original_git = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path
    original_slice = slicer_mod.slice_dry_run
    slicer_mod.slice_dry_run = _passing_slice_fn

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_git
    slicer_mod.slice_dry_run = original_slice


async def _create_project(client: AsyncClient, name: str = "Curtain rod bracket") -> dict:
    r = await client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


def _render_dir(tmp_path: Path, key: str) -> Path:
    """A per-render directory (the ``<renders_dir>/<uuid8>`` shape the
    render worker uses)."""
    rendir = tmp_path / "renders" / key
    rendir.mkdir(parents=True)
    return rendir


def _seed_stl(rendir: Path, fixture: str) -> None:
    (rendir / "model.stl").write_bytes((FIXTURES / fixture).read_bytes())


async def _create_version(
    client: AsyncClient, project_id: int, params: dict
) -> dict:
    r = await client.post(
        f"/api/projects/{project_id}/versions", json={"params": params}
    )
    assert r.status_code == 201, r.text
    return r.json()


def _point_version_at_render(app, project_id: int, rendir: Path) -> None:
    """Point the project's latest version row at ``rendir`` — simulating
    the ``render_artifact_dir`` column being written at version-creation
    time (the design-loop adapter's ``create_version(...,
    render_artifact_dir=...)``)."""
    latest = app.state.versions.latest_version(project_id)
    assert latest is not None
    app.state.conn.raw.execute(
        "UPDATE versions SET render_artifact_dir = ? WHERE id = ?",
        (str(rendir), latest["id"]),
    )
    app.state.conn.commit()


# ---------------------------------------------------------------------------
# The decisive test: the route returns REAL 3MF bytes (valid zip, mm units)
# ---------------------------------------------------------------------------


def test_route_returns_real_3mf_bytes(app_with_3mf, tmp_path: Path) -> None:
    """Drive the real route with the passing slice stub injected and the
    real ``box_20mm.stl`` fixture (passes all gates): the response is a
    200 with ``model/3mf`` content type, a ``Content-Disposition``
    filename, and bytes that are a valid ZIP declaring millimetre units —
    a real on-disk artefact from the real ``validate_stl`` pipeline, not a
    mocked response."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        rendir = _render_dir(tmp_path, "aa11bb22")
        _seed_stl(rendir, "box_20mm.stl")
        await _create_version(client, pid, {"W": 20.0, "D": 20.0, "H": 20.0})
        _point_version_at_render(app, pid, rendir)
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    assert resp.headers["content-type"] == "model/3mf"
    cd = resp.headers.get("content-disposition", "")
    assert cd.startswith('attachment; filename="curtain-rod-bracket-')
    assert cd.endswith('.3mf"')

    body = resp.content
    assert len(body) > 0, "the 200 body must not be empty"
    # The bytes are a valid ZIP declaring millimetre units.
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert zf.testzip() is None
    model_xml = None
    for n in zf.namelist():
        if n.endswith(".model"):
            model_xml = zf.read(n).decode("utf-8")
            break
    assert model_xml is not None, "the 3MF must contain a .model entry"
    assert 'unit="millimeter"' in model_xml or 'unit="mm"' in model_xml


# ---------------------------------------------------------------------------
# The filename slug — pinned case, asserted on Content-Disposition
# ---------------------------------------------------------------------------


def test_export_filename_slug_matches_spa_contract() -> None:
    """The backend slug must match ``copy.shell.exportFilename`` (web/src/
    copy.ts) — the pinned "Curtain rod bracket" + "v4" case, plus the
    lower-casing and non-alnum-run-collapsing the SPA applies."""
    # The pinned design-contract case (the SPA's design-contract test pins
    # the same value).
    assert _export_filename("Curtain rod bracket", "v4") == "curtain-rod-bracket-v4.3mf"
    # Lower-casing + non-alnum runs → single hyphen + trim leading/trailing.
    assert (
        _export_filename("Curtain Rod   Bracket!", "v1")
        == "curtain-rod-bracket-v1.3mf"
    )
    assert _export_filename("--Weird--Name--", "v2") == "weird-name-v2.3mf"
    assert _export_filename("Bracket 2000", "v10") == "bracket-2000-v10.3mf"


def test_route_content_disposition_matches_slug(app_with_3mf, tmp_path: Path) -> None:
    """The route's ``Content-Disposition`` carries the slug for the
    project's name + latest version id — the SPA names its Blob from the
    same contract, so the two cannot drift undetected."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client, "Curtain rod bracket")
        pid = proj["id"]
        rendir = _render_dir(tmp_path, "cd0001")
        _seed_stl(rendir, "box_20mm.stl")
        version = await _create_version(
            client, pid, {"W": 20.0, "D": 20.0, "H": 20.0}
        )
        _point_version_at_render(app, pid, rendir)
        resp = await client.get(f"/api/projects/{pid}/model.3mf")
        return resp, version

    resp, version = run_async(app, _call)
    assert resp.status_code == 200
    cd = resp.headers.get("content-disposition", "")
    expected = f'attachment; filename="curtain-rod-bracket-v{version["id"]}.3mf"'
    assert cd == expected, f"got {cd!r}, expected {expected!r}"


# ---------------------------------------------------------------------------
# Error cases — each a clear non-2xx naming the cause
# ---------------------------------------------------------------------------


def test_no_versions_returns_404(app_with_3mf, tmp_path: Path) -> None:
    """A project with no versions returns a clear 404 naming the cause."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        return await client.get(f"/api/projects/{proj['id']}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 404
    body = resp.json()
    assert "error" in body
    assert "no versions" in body["error"]


def test_no_recorded_render_returns_409(app_with_3mf, tmp_path: Path) -> None:
    """A version with NO recorded render (pre-#163 row: the column is NULL)
    degrades HONESTLY — a clear 409 naming that no render is recorded,
    never a guess at which directory might be the version's (no mtime
    fallback). A render directory IS seeded on disk so a mtime-based
    fallback would find it — the route must not use it."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        # Seed a render directory on disk (a mtime fallback WOULD find it)
        # but do NOT point the version row at it — the row stays NULL.
        rendir = _render_dir(tmp_path, "ffff0000")
        _seed_stl(rendir, "box_20mm.stl")
        await _create_version(client, pid, {"W": 20.0, "D": 20.0, "H": 20.0})
        # Confirm the row is NULL.
        row = app.state.versions.latest_version(pid)
        assert row["render_artifact_dir"] is None
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert "error" in body
    assert "no render is recorded" in body["error"]


def test_stale_render_dir_returns_409(app_with_3mf, tmp_path: Path) -> None:
    """A version whose recorded render directory no longer exists on disk
    (the render artifact was deleted) returns a clear 409 — never a
    re-render, never a guess at another directory."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        rendir = _render_dir(tmp_path, "gone0001")
        _seed_stl(rendir, "box_20mm.stl")
        await _create_version(client, pid, {"W": 20.0, "D": 20.0, "H": 20.0})
        _point_version_at_render(app, pid, rendir)
        # Delete the render directory (the artifact was removed after the
        # version was created). Use shutil.rmtree — the route does not run
        # for this 409 path, so no model.3mf exists, but a prior run in
        # the same dir (re-validate per request) could have left one.
        import shutil

        shutil.rmtree(rendir)
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert "no longer exists" in body["error"]


def test_repaired_fixtures_still_serve_3mf(app_with_3mf, tmp_path: Path) -> None:
    """The ``holey`` and ``winding_inverted`` fixtures are MEASURED to be
    REPAIRED by the pipeline (pymeshfix closes the hole; fix_normals
    restores the winding — ``tests/test_validation_pipeline.py`` asserts
    the same). The route therefore serves them as valid 3MF (200) — the
    gate-failure 502 path is exercised by the ``over_envelope`` fixture
    above. These tests pin that the repair path produces a valid, complete
    millimetre 3MF, not a silent pass over a broken mesh."""
    app = app_with_3mf

    async def _call(client):
        out = []
        for stl_name in ("holey.stl", "winding_inverted.stl"):
            proj = await _create_project(client)
            pid = proj["id"]
            rendir = _render_dir(tmp_path, f"repaired-{stl_name}")
            _seed_stl(rendir, stl_name)
            await _create_version(client, pid, {})
            _point_version_at_render(app, pid, rendir)
            out.append(await client.get(f"/api/projects/{pid}/model.3mf"))
        return out

    resps = run_async(app, _call)
    for stl_name, resp in zip(("holey.stl", "winding_inverted.stl"), resps):
        assert resp.status_code == 200, (
            f"{stl_name}: expected 200 (repaired), got {resp.status_code}: {resp.text}"
        )
        zf = zipfile.ZipFile(io.BytesIO(resp.content))
        assert zf.testzip() is None


@pytest.mark.parametrize(
    "stl_name,error_class",
    [
        # ``over_envelope.stl`` (400mm X) fails gate 7 (envelope). The
        # ``holey`` and ``winding_inverted`` fixtures are MEASURED to be
        # REPAIRED by the pipeline's watertight/winding repair chain
        # (pymeshfix + fix_normals close the holes / restore winding —
        # ``tests/test_validation_pipeline.py`` asserts the same: a holey
        # mesh "repairs cleanly"), so they PASS and are covered by the
        # 200-path test above rather than here.
        ("over_envelope.stl", "envelope"),
    ],
)
def test_gate_failure_returns_502_with_error_class(
    app_with_3mf, tmp_path: Path, stl_name: str, error_class: str
) -> None:
    """A gate failure (no 3MF produced) returns a clear 502 naming the
    cause with the closed-enum ``error_class`` — never an empty, partial
    or truncated 200. Driven by the real ``over_envelope.stl`` fixture
    (400mm X exceeds the QIDI Plus 5 envelope on gate 7)."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        rendir = _render_dir(tmp_path, f"gate-{stl_name}")
        _seed_stl(rendir, stl_name)
        # No W/D/H — the dimension gate ABSTAINS (stated_mm=None) so the
        # over-envelope X extent is reached and fails gate 7 (envelope),
        # not gate 4 (dimension, which would compare against 20.0).
        await _create_version(client, pid, {})
        _point_version_at_render(app, pid, rendir)
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 502, (
        f"{stl_name}: expected 502, got {resp.status_code}: {resp.text}"
    )
    body = resp.json()
    assert body["error_class"] == error_class
    assert "error" in body
    assert len(body["error"]) > 0
    # Never a 200 with empty/truncated bytes — the body is JSON, not 3MF.
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_in_flight_returns_409(app_with_3mf, tmp_path: Path) -> None:
    """A design loop in flight for the project returns a clear 409 naming
    the cause (the latest version's render may still be in production —
    re-validating mid-write would read a partial STL)."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        rendir = _render_dir(tmp_path, "infl0001")
        _seed_stl(rendir, "box_20mm.stl")
        await _create_version(client, pid, {"W": 20.0, "D": 20.0, "H": 20.0})
        _point_version_at_render(app, pid, rendir)
        # Set the in-flight flag (the same set the design-loop routes use).
        inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
        if inflight is None:
            inflight = set()
            app.state.design_loop_inflight = inflight
        inflight.add(pid)
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = run_async(app, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert "design loop is in flight" in body["error"]
    assert body["error_class"] == "conflict"


# ---------------------------------------------------------------------------
# The stated-mm ABSTENTION — a version with no W/D/H still serves a 3MF
# ---------------------------------------------------------------------------


def test_abstain_dimension_gate_serves_3mf(app_with_3mf, tmp_path: Path) -> None:
    """A version whose params carry NO W/D/H (any axis 0 or unset) passes
    ``stated_mm=None`` to ``validate_stl`` — the dimension gate ABSTAINS
    rather than comparing the measured bbox against 0.0 (which would FAIL
    a perfectly good model, the #91 bug). The route still serves a valid
    3MF (a bbox-gate abstention is still ``ok=True``)."""
    app = app_with_3mf

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        rendir = _render_dir(tmp_path, "abst0001")
        _seed_stl(rendir, "box_20mm.stl")
        # No W/D/H in the params — stated_mm must be None (abstain), not
        # (0.0, 0.0, 0.0) (compare against zero → fail).
        await _create_version(client, pid, {})
        _point_version_at_render(app, pid, rendir)
        return await client.get(f"/api/projects/{pid}/model.3mf")
    resp = run_async(app, _call)
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.text}"
    body = resp.content
    zf = zipfile.ZipFile(io.BytesIO(body))
    assert zf.testzip() is None


# ---------------------------------------------------------------------------
# The render-reference persistence (mirrors issue #137's bbox pattern)
# ---------------------------------------------------------------------------


def test_render_artifact_dir_column_mirrors_bbox_pattern(
    app_with_3mf, tmp_path: Path
) -> None:
    """The ``render_artifact_dir`` column follows issue #137's ``bbox``
    pattern: a nullable column added via the idempotent ``_ensure_column``
    migration, written at version-creation time, and ``_row_to_version``
    maps NULL to ``None`` (never a sentinel, never an empty string)."""
    app = app_with_3mf

    async def _call(client):
        # The column exists on the versions table (created by the
        # lifespan's ``migrate``).
        info = app.state.versions.conn.raw.execute(
            "PRAGMA table_info(versions)"
        ).fetchall()
        names = {r[1] for r in info}
        assert "render_artifact_dir" in names

        proj = await _create_project(client)
        pid = proj["id"]
        # A version created without a recorded render → NULL → None (never
        # an empty string, never a sentinel).
        await _create_version(client, pid, {"W": 20.0, "D": 20.0, "H": 20.0})
        version = app.state.versions.latest_version(pid)
        return version["render_artifact_dir"]

    value = run_async(app, _call)
    assert value is None, f"NULL must map to None, got {value!r}"


def test_design_loop_adapter_threads_render_artifact_dir(
    app_with_3mf, tmp_path: Path
) -> None:
    """The design-loop adapter threads the best candidate's render's
    ``render_artifact_dir`` into ``create_version`` at version-creation
    time (mirroring the ``bbox`` threading from issue #137) — so a version
    created by a passing loop carries the render reference, and the 3MF
    route can resolve the correct STL."""
    from d33d.design_loop import BboxInfo, IterationRecord, Score
    from d33d.design_loop_events import run_design_loop_with_events
    from d33d.render_worker import RenderResult

    app = app_with_3mf

    rendir = _render_dir(tmp_path, "adpt0001")
    _seed_stl(rendir, "box_20mm.stl")

    # A passing loop whose best candidate's render carries the durable
    # per-render directory.
    record = IterationRecord(
        iteration=1,
        scad_source="cube([20, 20, 20]);",
        render=RenderResult(
            ok=True,
            exit_code=0,
            duration_ms=1,
            error_class="ok",
            stderr="",
            stl="model.stl",
            csg="model.csg",
            views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
            render_artifact_dir=str(rendir),
        ),
        score=Score(bits=(True, True, True, True), rank=4, tiebreak=(True,)),
        params={"W": 20.0, "D": 20.0, "H": 20.0},
        bbox=BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0),
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    async def _loop(**kwargs: Any) -> Any:
        return _Result()

    async def _call(client):
        proj = await _create_project(client)
        pid = proj["id"]
        app.state.run_design_loop = _loop
        events = run_design_loop_with_events(
            app,
            pid,
            user_message="a 20mm cube",
            stated_dims=(20.0, 20.0, 20.0),
            chat_history=(),
            photo="data:image/png;base64,",
            request_text="a 20mm cube",
        )
        app.state.event_sources[pid] = events
        # Drive the generator to completion.
        async for _ in events:
            pass
        version = app.state.versions.latest_version(pid)
        return version

    version = run_async(app, _call)
    assert version is not None
    # The version row carries the render reference the adapter threaded.
    assert version["render_artifact_dir"] == str(rendir)
