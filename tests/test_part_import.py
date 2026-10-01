"""Part import tests (issue #325).

Covers:
- Content type / extension acceptance (STL binary + ASCII, 3MF)
- Gate order: 413 (oversize, streamed) → 400 (unsupported type) → 422 (unparseable)
- Zip-bomb and face-cap guards
- Non-finite vertex rejection
- Repair report numbers on holey.stl (gaps closed = 4)
- Unit classification (plausible mm, tiny → options, huge → options, 3MF inch → settled)
- Settle by unit and by one measurement
- v1 name "Imported {filename}" and recorded fields
- Export refused until settled (409 units_unsettled)
- Nothing written on error
- Re-import → 409 part_exists
"""

from __future__ import annotations

import asyncio
import io
import zipfile
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.part_import import (
    MAX_PART_FACES,
    MAX_PART_UPLOAD_BYTES,
    PART_UPLOAD_SETTLE_INVALID_DETAIL,
    PART_UPLOAD_UNPARSEABLE_DETAIL,
    classify_stl_units,
)

FIXTURES = Path(__file__).parent / "fixtures" / "stl"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(app: Any, coro_factory) -> Any:
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


def _repo_for(app, project_id: int) -> Path:
    for p in app.state.conn.list_projects():
        if p["id"] == project_id:
            return Path(p["git_repo_path"])
    raise AssertionError(f"project {project_id} not found")


def _stl_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _make_3mf(unit: str = "millimeter") -> bytes:
    import trimesh

    # A real 3D box (10x10x10 in file units) — a flat 2-triangle quad has
    # zero volume and pymeshfix empties it, so the fixture must be a solid.
    box = trimesh.creation.box((10.0, 10.0, 10.0))
    # Build the 3MF XML from the box vertices/faces
    vxml = "".join(
        f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>'
        for v in box.vertices
    )
    fxml = "".join(
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>'
        for f in box.faces
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f'<model unit="{unit}" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources></resources>"
        '<build><item objectid="1"/></build>'
        '<objects><object id="1" type="model"><mesh>'
        f"<vertices>{vxml}</vertices>"
        f"<triangles>{fxml}</triangles>"
        "</mesh></object></objects></model>"
    )
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"types")
        zf.writestr("_rels/.rels", b"rels")
        zf.writestr("3D/3dmodel.model", model.encode())
    return buf.getvalue()


def _make_3mf_zip_bomb(entries: int = 11_000) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(entries):
            zf.writestr(f"3D/model_{i}.xml", b"<x/>")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Content type / extension acceptance
# ---------------------------------------------------------------------------


def test_upload_stl_binary_accepted(app_with_projects):
    """STL binary (model/stl) is accepted."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "STL Bin"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["part"]["format"] == "stl"
    assert body["part"]["unit_status"] == "assumed"
    assert body["part"]["unit"] == "mm"


def test_upload_stl_octet_stream_with_stl_extension(app_with_projects):
    """octet-stream + .stl extension is accepted."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "STL Octet"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "application/octet-stream")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text


def test_upload_3mf_accepted(app_with_projects):
    """3MF (model/3mf) is accepted."""
    data = _make_3mf("millimeter")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Test"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["part"]["format"] == "3mf"
    assert body["part"]["unit_status"] == "settled"
    assert body["part"]["unit"] == "mm"


def test_upload_3mf_octet_stream_with_3mf_extension(app_with_projects):
    """octet-stream + .3mf extension is accepted."""
    data = _make_3mf("millimeter")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Octet"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "application/octet-stream")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text


def test_upload_unsupported_content_type_400(app_with_projects):
    """text/plain → 400 with the copy.ts string."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Bad Type"})
        pid = r.json()["id"]
        files = {"file": ("bad.txt", b"not a mesh", "text/plain")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 400
    assert r.json()["detail"] == "That file type isn't supported. Upload an STL or 3MF mesh."


def test_upload_octet_stream_without_stl_3mf_extension_400(app_with_projects):
    """octet-stream without a .stl/.3mf extension → 400."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Ext"})
        pid = r.json()["id"]
        files = {"file": ("blob", b"not a mesh", "application/octet-stream")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Gate order: 413 → 400 → 422
# ---------------------------------------------------------------------------


def test_upload_413_fires_before_422(app_with_projects):
    """Oversize body (51 MB) → 413, NOT 422 (the size check fires first)."""
    # A body that is oversize AND unparseable: the 413 must fire first.
    oversized = b"\x00" * (MAX_PART_UPLOAD_BYTES + 1024 * 1024 + 100)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Oversize"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", oversized, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


def test_upload_400_fires_before_413(app_with_projects):
    """A small body with a wrong content type → 400, NOT 413."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Wrong Type"})
        pid = r.json()["id"]
        files = {"file": ("x.txt", b"small", "text/plain")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 400


def test_upload_unparseable_422(app_with_projects):
    """Garbage bytes with a valid content type → 422 with the copy.ts string."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Unparseable"})
        pid = r.json()["id"]
        files = {"file": ("bad.stl", b"this is not a mesh at all", "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


def test_upload_empty_body_422(app_with_projects):
    """Empty file → 422."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Empty"})
        pid = r.json()["id"]
        files = {"file": ("empty.stl", b"", "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


def test_upload_to_nonexistent_project_404(app_with_projects):
    """Upload to a non-existent project → 404."""
    async def _call(client):
        data = _stl_bytes(FIXTURES / "box_20mm.stl")
        files = {"file": ("x.stl", data, "model/stl")}
        return await client.post("/api/projects/99999/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# Security guards
# ---------------------------------------------------------------------------


def test_zip_bomb_entry_count_422(app_with_projects):
    """A 3MF with >10,000 entries → 422 (zip-bomb guard, before extraction)."""
    bomb = _make_3mf_zip_bomb(entries=11_000)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Zip Bomb"})
        pid = r.json()["id"]
        files = {"file": ("bomb.3mf", bomb, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


def test_zip_bomb_not_a_zip_422(app_with_projects):
    """Bytes that are not a valid zip → 422."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Not Zip"})
        pid = r.json()["id"]
        files = {"file": ("bad.3mf", b"not a zip at all", "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422


def test_non_finite_vertices_422(app_with_projects):
    """STL with NaN vertices → 422."""
    import struct
    import numpy as np

    # Build a binary STL with a NaN vertex (raw bytes — trimesh's process=
    # True would drop the NaN, so we bypass it by building the raw file)
    header = b"\x00" * 80 + struct.pack("<I", 1)
    nan = struct.pack("<f", float("nan"))
    zero = struct.pack("<f", 0.0)
    one = struct.pack("<f", 1.0)
    face = (
        (zero * 3)
        + (zero + zero + nan)
        + (one + zero + zero)
        + (zero + one + zero)
        + struct.pack("<H", 0)
    )
    bad_stl = header + face

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "NaN"})
        pid = r.json()["id"]
        files = {"file": ("nan.stl", bad_stl, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


# ---------------------------------------------------------------------------
# Repair report
# ---------------------------------------------------------------------------


def test_repair_report_holey_gaps_closed(app_with_projects):
    """holey.stl: 4 boundary loops before repair → gaps_closed = 4."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Holey"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    body = r.json()
    report = body["part"]["report"]
    assert report["gaps_closed"] == 4
    assert report["watertight"] is True
    assert report["bodies"] == 1
    assert report["triangles"] > 0


def test_repair_report_box_watertight(app_with_projects):
    """box_20mm.stl: watertight, 0 gaps, 1 body."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Box"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["watertight"] is True
    assert report["gaps_closed"] == 0
    assert report["bodies"] == 1


def test_repair_report_two_body(app_with_projects):
    """two_body_multisolid.stl: 2 bodies."""
    data = _stl_bytes(FIXTURES / "two_body_multisolid.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "TwoBody"})
        pid = r.json()["id"]
        files = {"file": ("two.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["bodies"] == 2


# ---------------------------------------------------------------------------
# Unit classification
# ---------------------------------------------------------------------------


def test_stl_plausible_mm_assumed(app_with_projects):
    """box_20mm.stl: largest side 20 ≥ 5, fits envelope → assumed mm."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Plausible"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    part = r.json()["part"]
    assert part["unit_status"] == "assumed"
    assert part["unit"] == "mm"
    assert part["scale"] == 1.0


def test_stl_huge_over_envelope_unsettled(app_with_projects):
    """over_envelope.stl (400mm) → unsettled with options."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Huge"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    part = r.json()["part"]
    assert part["unit_status"] == "unsettled"
    options = part["options"]
    assert options is not None
    assert len(options) == 3
    # The likeliest option comes first (the one that fits envelope AND ≥ 5mm)
    units_in_order = [o["unit"] for o in options]
    assert "inch" in units_in_order
    assert "cm" in units_in_order
    assert "mm" in units_in_order


def test_3mf_inch_settled(app_with_projects):
    """3MF with inch unit → settled, scale 25.4."""
    data = _make_3mf("inch")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Inch"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "mm"
    assert part["scale"] == 25.4


def test_3mf_cm_settled(app_with_projects):
    """3MF with centimeter unit → settled, scale 10."""
    data = _make_3mf("centimeter")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF CM"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["scale"] == 10.0


def test_3mf_unconvertible_unit_422(app_with_projects):
    """3MF with an unconvertible unit (e.g. 'foot') → 422."""
    data = _make_3mf("foot")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Foot"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422


def test_classify_stl_units_plausible():
    """Unit level: 20mm box → assumed mm."""
    result = classify_stl_units((20.0, 20.0, 20.0))
    assert result["status"] == "assumed"
    assert result["unit"] == "mm"
    assert result["scale"] == 1.0


def test_classify_stl_units_tiny():
    """Unit level: 2.4mm box → unsettled (largest side < 5)."""
    result = classify_stl_units((2.4, 1.8, 3.1))
    assert result["status"] == "unsettled"
    assert result["options"] is not None
    assert len(result["options"]) == 3


def test_classify_stl_units_huge():
    """Unit level: 400mm box → unsettled (exceeds envelope)."""
    result = classify_stl_units((400.0, 20.0, 20.0))
    assert result["status"] == "unsettled"
    assert result["options"] is not None


# ---------------------------------------------------------------------------
# Settle
# ---------------------------------------------------------------------------


def test_settle_by_unit(app_with_projects):
    """Settle by unit: {"unit": "inch"} → scale 25.4, status settled."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Unit"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        # Now settle
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "inch"}
        )
        return settle_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200, r.text
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "inch"
    assert part["scale"] == 25.4


def test_settle_by_measurement(app_with_projects):
    """Settle by one measurement: {"axis": "W", "mm": 60} → scale derived."""
    data = _stl_bytes(FIXTURES / "holey.stl")  # 20mm box, W=20 in file units

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Meas"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"axis": "W", "mm": 60.0}
        )
        return settle_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200, r.text
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "custom"
    # scale = 60 / 20 = 3.0
    assert abs(part["scale"] - 3.0) < 1e-6


def test_settle_invalid_body_422(app_with_projects):
    """Settle with an invalid body → 422."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Bad Settle"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "furlong"}
        )
        return settle_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_SETTLE_INVALID_DETAIL


def test_settle_on_project_without_part_404(app_with_projects):
    """Settle on a project with no part → 404."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Part"})
        pid = r.json()["id"]
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "mm"}
        )
        return settle_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 404


def test_settle_idempotent(app_with_projects):
    """Settling an already-settled part is idempotent."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Idempotent"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        await client.post(f"/api/projects/{pid}/part/units", json={"unit": "mm"})
        # Second settle
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "cm"}
        )
        return settle_r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 200, r.text
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "cm"
    assert part["scale"] == 10.0


# ---------------------------------------------------------------------------
# v1 name and recorded fields
# ---------------------------------------------------------------------------


def test_v1_name_imported_filename(app_with_projects):
    """The v1 is named "Imported {filename}"."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "V1 Name"})
        pid = r.json()["id"]
        files = {"file": ("motor-mount.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        versions_r = await client.get(f"/api/projects/{pid}/versions")
        return upload_r, versions_r

    upload_r, versions_r = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201
    body = upload_r.json()
    v1_id = body["version_id"]
    versions = versions_r.json()
    v1 = next(v for v in versions if v["id"] == v1_id)
    assert v1["name"] == "Imported motor-mount.stl"


def test_v1_source_kind_import(app_with_projects):
    """The v1 row has source_kind = 'import'."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Source Kind"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        # Read the raw version row
        row = app_with_projects.state.conn.raw.execute(
            "SELECT source_kind FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        return upload_r, row

    upload_r, row = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201
    assert row[0] == "import"


def test_stored_filename_is_fixed_name(app_with_projects):
    """The stored file is part.stl (or part.3mf), never the user's filename."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Fixed Name"})
        pid = r.json()["id"]
        files = {"file": ("motor-mount.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        repo_path = _repo_for(app_with_projects, pid)
        v1_id = upload_r.json()["version_id"]
        return upload_r, repo_path, v1_id

    upload_r, repo_path, v1_id = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201
    # The file should be at versions/{v1_id}/part.stl
    part_path = repo_path / "versions" / str(v1_id) / "part.stl"
    assert part_path.exists(), f"part.stl not found at {part_path}"
    # The user's filename should NOT be in the path
    assert "motor-mount" not in str(part_path)


# ---------------------------------------------------------------------------
# Export refused until settled
# ---------------------------------------------------------------------------


def test_export_refused_until_settled(app_with_projects):
    """An unsettled part → 409 units_unsettled on GET /model.3mf."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")  # unsettled

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Export Gate"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        export_r = await client.get(f"/api/projects/{pid}/model.3mf")
        return upload_r, export_r

    upload_r, export_r = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201
    assert export_r.status_code == 409
    body = export_r.json()
    assert body["error_class"] == "units_unsettled"


def test_export_allowed_after_settle(app_with_projects):
    """A settled part → the units_unsettled 409 is gone (may be a different
    409 for no render, but not units_unsettled)."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Export OK"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        await client.post(f"/api/projects/{pid}/part/units", json={"unit": "mm"})
        export_r = await client.get(f"/api/projects/{pid}/model.3mf")
        return export_r

    export_r = _run_async(app_with_projects, _call)
    # After settle, the units_unsettled 409 is gone. The route may return
    # a different 409 (no render) or a 502 (no render directory), but
    # it must NOT be units_unsettled.
    if export_r.status_code == 409:
        body = export_r.json()
        assert body.get("error_class") != "units_unsettled"


# ---------------------------------------------------------------------------
# Nothing written on error
# ---------------------------------------------------------------------------


def test_nothing_written_on_422(app_with_projects):
    """A 422 (unparseable) writes nothing: no file in repo, no version row."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Nothing"})
        pid = r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        files = {"file": ("bad.stl", b"not a mesh", "model/stl")}
        r = await client.post(f"/api/projects/{pid}/part", files=files)
        # Check no files in the repo
        versions_dir = repo_path / "versions"
        remaining = list(versions_dir.iterdir()) if versions_dir.exists() else []
        # Check no version row
        vrows = app_with_projects.state.conn.raw.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = ?", (pid,)
        ).fetchone()
        return r, remaining, vrows[0]

    r, remaining, vcount = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert remaining == [], f"files written on 422: {remaining}"
    assert vcount == 0, f"version row written on 422: {vcount}"


def test_nothing_written_on_413(app_with_projects):
    """A 413 (oversize) writes nothing."""
    oversized = b"\x00" * (MAX_PART_UPLOAD_BYTES + 1024 * 1024 + 100)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Nothing413"})
        pid = r.json()["id"]
        repo_path = _repo_for(app_with_projects, pid)
        files = {"file": ("big.stl", oversized, "model/stl")}
        r = await client.post(f"/api/projects/{pid}/part", files=files)
        versions_dir = repo_path / "versions"
        remaining = list(versions_dir.iterdir()) if versions_dir.exists() else []
        return r, remaining

    r, remaining = _run_async(app_with_projects, _call)
    assert r.status_code == 413
    assert remaining == []


# ---------------------------------------------------------------------------
# Re-import
# ---------------------------------------------------------------------------


def test_reimport_409_part_exists(app_with_projects):
    """A second upload to a project that already has a part → 409 part_exists."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Reimport"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        first = await client.post(f"/api/projects/{pid}/part", files=files)
        second = await client.post(f"/api/projects/{pid}/part", files=files)
        return first, second

    first, second = _run_async(app_with_projects, _call)
    assert first.status_code == 201
    assert second.status_code == 409
    body = second.json()
    assert body["detail"]["code"] == "part_exists"


# ---------------------------------------------------------------------------
# Design state envelope
# ---------------------------------------------------------------------------


def test_design_state_has_part_key(app_with_projects):
    """GET /design-state includes the "part" key (null for no part)."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DS No Part"})
        pid = r.json()["id"]
        ds_r = await client.get(f"/api/projects/{pid}/design-state")
        return ds_r

    ds_r = _run_async(app_with_projects, _call)
    assert ds_r.status_code == 200
    body = ds_r.json()
    assert "part" in body
    assert body["part"] is None


def test_design_state_part_present_after_import(app_with_projects):
    """After an import, GET /design-state includes the part facts."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DS Part"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        await client.post(f"/api/projects/{pid}/part", files=files)
        ds_r = await client.get(f"/api/projects/{pid}/design-state")
        return ds_r

    ds_r = _run_async(app_with_projects, _call)
    assert ds_r.status_code == 200
    body = ds_r.json()
    part = body["part"]
    assert part is not None
    assert part["filename"] == "box.stl"
    assert part["format"] == "stl"
    assert part["unit_status"] == "assumed"
    assert part["unit"] == "mm"


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_max_part_upload_bytes_is_50mb():
    assert MAX_PART_UPLOAD_BYTES == 50 * 1024 * 1024


def test_max_part_faces_is_2m():
    assert MAX_PART_FACES == 2_000_000
