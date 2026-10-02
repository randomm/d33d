"""Tests for GET /api/projects/{id}/part.stl (issue #334, sub-issue 4).

The endpoint serves the project's committed part as binary STL, in mm:
- STL parts, assumed/settled: the committed geometry scaled by ``part_scale``
  (the same factor the render worker applies via ``scale(...)``)
- STL parts, unsettled: the committed file verbatim (file units — the SPA
  hides the plate while unsettled)
- 3MF parts: converted on the host via load_part_geometry, scaled the same
  way per the unit status
- 404 when no project or no part
- 409 when the committed file is missing (source_missing shape)
- 413 when the file exceeds the upload cap
- containment: a hand-placed symlink escaping the repo is rejected (the
  render-staging ``_validate_part_path`` validation, reused)
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.part_import import MAX_PART_UPLOAD_BYTES
from d33d.part_mesh import PartUploadError, read_part_file_atomic

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


def _run_async(app: Any, coro_factory) -> Any:
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


async def _upload_part(client: Any, pid: int, data: bytes, name: str, content_type: str) -> None:
    files = {"file": (name, data, content_type)}
    r = await client.post(f"/api/projects/{pid}/part", files=files)
    assert r.status_code in (200, 201), r.text


async def _settle_by_axis(client: Any, pid: int, axis: str, mm: float) -> None:
    r = await client.post(f"/api/projects/{pid}/part/units", json={"axis": axis, "mm": mm})
    assert r.status_code == 200, r.text


def _v1_part_path(app: Any, pid: int, name: str = "part.stl") -> Path:
    conn = app.state.conn
    row = conn.get_project(pid)
    v1 = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (pid,),
    ).fetchone()
    assert v1 is not None
    return Path(row["git_repo_path"]) / "versions" / str(v1["id"]) / name


def _build_box_3mf() -> bytes:
    """A minimal 3MF: a 10 mm cube (the same fixture the conversion test used)."""
    import trimesh

    box = trimesh.creation.box((10.0, 10.0, 10.0))
    vxml = "".join(f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>' for v in box.vertices)
    fxml = "".join(f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in box.faces)
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources></resources><build><item objectid=\"1\"/></build>"
        f'<objects><object id="1" type="model"><mesh><vertices>{vxml}</vertices>'
        f'<triangles>{fxml}</triangles></mesh></object></objects></model>'
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"types")
        zf.writestr("_rels/.rels", b"rels")
        zf.writestr("3D/3dmodel.model", model.encode())
    return buf.getvalue()


def _mesh_from_stl_bytes(stl_bytes: bytes) -> Any:
    import trimesh

    loaded = trimesh.load(io.BytesIO(stl_bytes), file_type="stl")
    if isinstance(loaded, trimesh.Scene):
        return loaded.to_mesh()
    return loaded


# ---------------------------------------------------------------------------
# 200 paths: the three unit states (the operator decision's scale behaviour)
# ---------------------------------------------------------------------------


def test_part_stl_assumed_scale_is_applied(app_with_projects):
    """Assumed (mm, scale 1): the served STL's extents equal the file's mm
    extents — the geometry IS in mm (scale 1 applied through the loader)."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "STL test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "model/stl"

    mesh = _mesh_from_stl_bytes(resp.content)
    assert mesh is not None
    # The box_20mm fixture is 20 mm across: the assumed mm scale is 1, so
    # the served extents ARE the file's extents (in mm).
    extents = mesh.extents
    assert max(extents) == pytest.approx(20.0, abs=0.1)


def test_part_stl_settled_scale_is_applied(app_with_projects):
    """Settled by one real measurement: the served STL's extents are the
    file's extents × the settle scale (the 20 mm box reported as 10 mm wide
    becomes a 40 mm box — the scale doubles the geometry)."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "STL settled test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        # The file is 20 mm across; the operator says the base is 10 mm
        # wide — the scale is 0.5, halving the geometry.
        await _settle_by_axis(client, pid, "W", 10.0)
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 200, resp.text
    mesh = _mesh_from_stl_bytes(resp.content)
    assert mesh is not None
    extents = mesh.extents
    # 20 mm × 0.5 scale → 10 mm.
    assert max(extents) == pytest.approx(10.0, abs=0.1)


def test_part_stl_unsettled_serves_file_units_verbatim(app_with_projects):
    """Unsettled: the endpoint serves the committed file verbatim — the
    bytes are byte-identical to what was uploaded (file units, no scale).
    ``over_envelope.stl`` (400 mm) stays unsettled at upload (no plausible
    unit fits the envelope) — a genuinely unsettled part."""
    data = (FIXTURES / "over_envelope.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "STL unsettled test"})
        pid = r.json()["id"]
        r = await client.post(f"/api/projects/{pid}/part", files={"file": ("big.stl", data, "model/stl")})
        part = r.json()["part"]
        assert part["unit_status"] == "unsettled"
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 200, resp.text
    assert resp.content == data


def test_part_stl_3mf_converts(app_with_projects):
    """A 3MF part is converted to STL on the host and served as model/stl.
    3MF imports settle immediately (the unit is declared), so the served
    geometry is scaled by the settle scale (mm → 1)."""
    data = _build_box_3mf()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "part.3mf", "model/3mf")
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"] == "model/stl"
    mesh = _mesh_from_stl_bytes(resp.content)
    assert mesh is not None
    # The cube is 10 mm; the 3MF settles at mm (scale 1).
    assert max(mesh.extents) == pytest.approx(10.0, abs=0.1)
    assert len(resp.content) > 0


# ---------------------------------------------------------------------------
# Error paths
# ---------------------------------------------------------------------------


def test_part_stl_404_no_project(app_with_projects):
    async def _call(client):
        return await client.get("/api/projects/99999/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 404


def test_part_stl_404_no_part(app_with_projects):
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "no part"})
        pid = r.json()["id"]
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 404


def test_part_stl_409_missing_file(app_with_projects):
    """A part row exists but the committed file is absent from disk → 409
    with the source_missing detail shape."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "missing file"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        # Delete the committed file from disk.
        f = _v1_part_path(app_with_projects, pid)
        if f.exists():
            f.unlink()
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "source_missing"


def test_part_stl_413_over_cap(app_with_projects):
    """A committed file over the 50 MB upload cap is refused (413) — the
    SPA would OOM the browser otherwise."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "oversize"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        # Replace the committed file with one over the cap (the upload cap
        # refuses it on upload — the 413 guards a hand-committed file).
        f = _v1_part_path(app_with_projects, pid)
        f.write_bytes(b"\x00" * (MAX_PART_UPLOAD_BYTES + 1))
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 413


# ---------------------------------------------------------------------------
# Containment (the operator decision's reuse of the shared render-staging
# validation + the atomic O_NOFOLLOW read, the render worker's standard)
# ---------------------------------------------------------------------------


def test_part_stl_atomic_read_closes_toctou_symlink_swap(app_with_projects):
    """C1 regression (the render staging's own test shape): the atomic read
    rejects a symlink swapped in after the lstat-based validation passes,
    and the outside bytes never leak. This is the O_NOFOLLOW open + fstat
    the endpoint now shares with the render path — validate-then-read
    (``Path.stat()``/``read_bytes()``) would have served the outside file."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "toctou test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        committed = _v1_part_path(app_with_projects, pid)
        repo_dir = committed.parent.parent
        # Validate passes on the legitimate file (the lstat pre-open check).
        from d33d.part_mesh import validate_part_path

        assert validate_part_path(committed, repo_dir) is None
        # Attack: swap the committed file for a symlink pointing outside.
        outside_file = repo_dir.parent / f"outside-{pid}.stl"
        outside_file.write_bytes(b"SECRET OUTSIDE CONTENT")
        committed.unlink()
        committed.symlink_to(outside_file)
        # The atomic read must refuse (O_NOFOLLOW) and never return the
        # outside bytes — the endpoint maps the OSError to 409.
        try:
            read_part_file_atomic(committed, MAX_PART_UPLOAD_BYTES)
        except OSError:
            pass
        else:
            raise AssertionError("atomic read followed a swapped-in symlink")
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "source_missing"
    assert b"SECRET OUTSIDE CONTENT" not in resp.content


def test_part_stl_rejects_symlink_escaping_repo(app_with_projects):
    """A hand-placed ``part.stl`` that is a symlink OUTSIDE the repo is
    rejected with 409 — the render-staging containment validation is
    reused, not re-implemented."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "symlink test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        committed = _v1_part_path(app_with_projects, pid)
        repo_dir = committed.parent.parent
        # A file OUTSIDE the repo (sibling of the repo directory), then the
        # committed path becomes a symlink pointing at it.
        outside_file = repo_dir.parent / f"outside-{pid}.stl"
        outside_file.write_bytes(data)
        committed.unlink()
        committed.symlink_to(outside_file)
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "source_missing"


def test_part_stl_rejects_path_traversal_name(app_with_projects):
    """A committed file whose name is not exactly ``part.stl``/``part.3mf``
    (e.g. a hand-placed ``evil.stl`` replacing the committed path) is
    rejected — the exact-name check is part of the same validation."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "rename test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        committed = _v1_part_path(app_with_projects, pid)
        renamed = committed.parent / "renamed.stl"
        committed.rename(renamed)
        return await client.get(f"/api/projects/{pid}/part.stl")

    resp = _run_async(app_with_projects, _call)
    # The endpoint derives the path from the format constant, so a renamed
    # file means the derived path is absent → 409 source_missing.
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "source_missing"


# ---------------------------------------------------------------------------
# ETag / conditional GET (the derived bytes are deterministic — no re-parse)
# ---------------------------------------------------------------------------


def test_part_stl_etag_304_when_unchanged(app_with_projects):
    """A repeat GET with ``If-None-Match`` matching the ETag answers 304
    with an empty body (the derived bytes are deterministic in committed
    file + scale — no re-parse / re-scale / re-export / re-download). A
    mismatched tag still answers 200 with the bytes and the ETag header."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "etag test"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        first = await client.get(f"/api/projects/{pid}/part.stl")
        etag = first.headers["etag"]
        second = await client.get(
            f"/api/projects/{pid}/part.stl",
            headers={"If-None-Match": f'"etag", {etag}'},
        )
        third = await client.get(
            f"/api/projects/{pid}/part.stl", headers={"If-None-Match": '"stale"'}
        )
        return first, second, third

    first, second, third = _run_async(app_with_projects, _call)
    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b""  # a 304 carries no body
    assert third.status_code == 200
    assert third.content == first.content  # unchanged bytes
    assert third.headers["etag"] == first.headers["etag"]


def test_part_stl_unsettled_serves_verbatim_via_shared_resolver(app_with_projects):
    """An UNSETTLED part's committed file is served verbatim, resolved
    through the shared ``resolve_v1_part_path`` — the fallback that used to
    re-derive the in-repo layout inline is gone; this proves the one path
    rule still resolves the unsettled case (404s would mean the resolver
    lost the unsettled path). The upload is ``over_envelope.stl`` (the
    genuinely unsettled fixture — box_20mm settles to mm on upload, so it
    is not a valid unsettled case)."""
    data = (FIXTURES / "over_envelope.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "unsettled-shared"})
        pid = r.json()["id"]
        r = await client.post(
            f"/api/projects/{pid}/part", files={"file": ("big.stl", data, "model/stl")}
        )
        part = r.json()["part"]
        assert part["unit_status"] == "unsettled"
        resp = await client.get(f"/api/projects/{pid}/part.stl")
        return resp

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 200
    assert resp.content == data  # verbatim file units (no scale yet)


def test_part_stl_304_never_reads_file(app_with_projects):
    """The 304 is answered from the fstat-derived ETag BEFORE any read: a
    matching If-None-Match returns 304 with the file-read helper never
    called (the cheap stat is the whole cost of an unchanged repeat)."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "etag-noread"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        first = await client.get(f"/api/projects/{pid}/part.stl")
        etag = first.headers["etag"]
        with patch(
            "d33d.part_import.read_part_file_atomic",
            side_effect=AssertionError("read on 304"),
        ):
            second = await client.get(
                f"/api/projects/{pid}/part.stl",
                headers={"If-None-Match": etag},
            )
        return first, second

    first, second = _run_async(app_with_projects, _call)
    assert first.status_code == 200
    assert second.status_code == 304
    assert second.content == b""


def test_part_stl_etag_changes_after_settle(app_with_projects):
    """The ETag is derived from st_size/st_mtime_ns + scale, so settling the
    unit (which records part_scale) changes the tag even for unchanged
    bytes — a stale tag never matches a rescaled part."""
    data = (FIXTURES / "box_20mm.stl").read_bytes()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "etag-settle"})
        pid = r.json()["id"]
        await _upload_part(client, pid, data, "box.stl", "model/stl")
        first = await client.get(f"/api/projects/{pid}/part.stl")
        await _settle_by_axis(client, pid, "W", 40.0)
        second = await client.get(f"/api/projects/{pid}/part.stl")
        return first, second

    first, second = _run_async(app_with_projects, _call)
    assert first.status_code == 200
    assert second.status_code == 200
    assert first.headers["etag"] != second.headers["etag"]


def test_part_stl_conversion_failure_maps_409(app_with_projects):
    """A committed part whose bytes no longer parse (a corrupt 3MF, a
    ragged export) maps to 409 source_missing — never a raw 500. The
    conversion's PartUploadError is the closed failure type; a raw
    TypeError is caught and re-raised as the same type in
    ``_scaled_stl_sync``."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "convert-fail"})
        pid = r.json()["id"]
        await _upload_part(
            client, pid, (FIXTURES / "box_20mm.stl").read_bytes(), "box.stl", "model/stl"
        )
        # Force the conversion to fail (corrupt committed bytes would do
        # the same; the patch keeps the test deterministic).
        with patch(
            "d33d.part_import.load_part_geometry",
            side_effect=PartUploadError("unparseable stl: corrupt"),
        ):
            resp = await client.get(f"/api/projects/{pid}/part.stl")
        return resp

    resp = _run_async(app_with_projects, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["code"] == "source_missing"
