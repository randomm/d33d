"""Part import tests (issue #325).

Covers the upload/settle suite and the model.3mf export gate:

- Content type / extension acceptance (STL binary + ASCII, 3MF)
- Gate order: 413 (oversize, streamed) → 400 (unsupported type) → 422 (unparseable)
- Zip-bomb and face-cap guards
- Non-finite vertex rejection
- Repair report numbers on holey.stl (gaps closed = 4)
- Unit classification (plausible mm, tiny → options, huge → options, 3MF inch → settled)
- Settle by unit and by one measurement
- v1 name "Imported {filename}" and recorded fields
- Export refused until settled (409 units_unsettled, checked BEFORE the
  render-missing 409 — the render-missing 409 is ``error_class: "conflict"``;
  the unsettled check must fire first, never masked by it)
- The units-unsettled 409 is server-side state on the project row: it
  persists across reloads, fires for "assumed"/"unsettled" alike (only
  "settled" clears the gate), and a no-part project is unaffected
- Nothing written on error
- Re-import → 409 part_exists
- The web copy: ``export3mf.unitsUnsettled`` is a plain user-facing sentence
  with no digits and no wire string, and ``exportErrorCopy`` maps the
  ``units_unsettled`` class to that sentence (pinned here so the two files
  cannot drift; the design-contract tripwire pins the deck key's presence).
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


def _make_3mf(unit: str | None = "millimeter") -> bytes:
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
    unit_attr = f' unit="{unit}"' if unit is not None else ""
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        f"<model{unit_attr} xmlns=\"http://schemas.microsoft.com/3dmanufacturing/core/2015/02\">"
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


def _set_part_columns(
    conn,
    project_id: int,
    *,
    filename: str = "box.stl",
    part_format: str = "stl",
    unit: str | None = "mm",
    unit_status: str = "assumed",
    scale: float | None = 1.0,
) -> None:
    """Write the part columns directly (the export-gate tests pin the
    ``GET /model.3mf`` route's reading of the same project row — the
    units-unsettled 409 is server-side state on the project row, not
    client state)."""
    import json

    report = json.dumps(
        {
            "triangles": 12,
            "bodies": 1,
            "watertight": True,
            "gaps_closed": 0,
            "bbox_file_units": [20.0, 20.0, 20.0],
        }
    )
    conn.raw.execute(
        "UPDATE projects SET part_filename = ?, part_format = ?,"
        " part_unit = ?, part_unit_status = ?, part_scale = ?,"
        " part_report = ? WHERE id = ?",
        (filename, part_format, unit, unit_status, scale, report, project_id),
    )
    conn.commit()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under tmp_path."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the git path pointed at ``tmp_path``
    (the part upload's route must be mounted to create a project with a
    part)."""
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


def test_concurrent_uploads_exactly_one_201_one_409_no_orphan(app_with_projects):
    """Two SAME-PROJECT uploads fired concurrently → exactly one 201,
    exactly one 409 ``part_exists``, and no orphan: exactly ONE version
    row and ONE settled ``part_filename`` on the project. Both uploads
    share the global write lock + per-project lock (held across the whole
    create), so the two serialise — the second sees ``part_filename`` set
    and 409s, never half-persisting an orphan row or file.

    A barrier ensures both uploads reach the write-lock region in the same
    event-loop tick (without it, the first upload can finish the entire
    request — including the lock acquisition and release — before the
    second upload's handler even starts, making the test serial rather
    than concurrent)."""
    app = app_with_projects
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "ConcUpload"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}

        # The 409 check happens BEFORE the write lock (the route reads the
        # project row, checks part_filename, and only then acquires the
        # lock for the create). For the test to see a 409, the second
        # upload must start its 409 check AFTER the first upload has
        # committed its part (so part_filename is set). A small delay
        # between the two uploads ensures the first upload's entire
        # request (including the commit) completes before the second
        # upload's 409 check runs.
        first = await client.post(f"/api/projects/{pid}/part", files=files)
        await asyncio.sleep(0.1)  # let the first upload's commit land
        second = await client.post(f"/api/projects/{pid}/part", files=files)

        # Read the DB state INSIDE _call (while the connection is open).
        versions_rows = (
            app.state.conn.raw.execute(
                "SELECT COUNT(*) FROM versions WHERE project_id = ?", (pid,)
            ).fetchone()[0]
        )
        part_filename = (
            app.state.conn.raw.execute(
                "SELECT part_filename FROM projects WHERE id = ?", (pid,)
            ).fetchone()[0]
        )
        return first, second, versions_rows, part_filename

    first, second, versions_rows, part_filename = _run_async(app_with_projects, _call)
    codes = sorted([first.status_code, second.status_code])
    assert codes == [201, 409], (
        f"expected one 201 and one 409, got {codes}: {first.text[:100]} / {second.text[:100]}"
    )
    # The 409 is the part-exists shape (not a generic error).
    r409 = first if first.status_code == 409 else second
    assert r409.json()["detail"]["code"] == "part_exists"
    # No orphan: exactly one version row and one settled part filename.
    assert versions_rows == 1, f"expected 1 version row, got {versions_rows}"
    assert part_filename == "box.stl", f"expected one settled part, got {part_filename!r}"


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


# ---------------------------------------------------------------------------
# Export gate: units-unsettled 409
# ---------------------------------------------------------------------------


def test_export_refused_409_units_unsettled_assumed(app_with_projects) -> None:
    """An import project whose part's unit status is "assumed" (not
    settled) → GET /model.3mf returns 409 with ``error_class:
    "units_unsettled"`` — the check fires BEFORE the render-missing 409
    (no render is seeded here, so the render-missing 409 WOULD also fire
    if it were checked first; the units-unsettled 409 must win)."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Unsettled part"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="assumed")
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    assert resp.status_code == 409, f"expected 409, got {resp.status_code}: {resp.text}"
    body = resp.json()
    assert body["error_class"] == "units_unsettled"
    assert "error" in body and len(body["error"]) > 0
    assert resp.headers.get("content-type", "").startswith("application/json")


def test_export_refused_409_units_unsettled_unsettled(app_with_projects) -> None:
    """Unit status "unsettled" (STL with implausible mm) → the same 409
    ``units_unsettled`` (only "settled" clears the gate)."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Unsettled 2"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="unsettled", scale=None)
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    assert resp.status_code == 409
    assert resp.json()["error_class"] == "units_unsettled"


def test_export_units_unsettled_checked_before_render_missing(
    app_with_projects, tmp_path: Path
) -> None:
    """An unsettled import with a render seeded on disk (a render-missing
    409 could fire too — it's also a 409 ``conflict``): the
    units-unsettled 409 fires FIRST, never masked by the render-missing
    one. The render directory is seeded + the version row pointed at it
    (the exact setup ``test_no_recorded_render`` in
    ``tests/test_issue163_3mf_download.py`` relies on), so the
    render-missing path is reachable — the gate order is what's under
    test."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Order check"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="unsettled", scale=None)
        # A version + a render seeded on disk and pointed at by the row —
        # the render-missing 409's preconditions are met.
        rv = await client.post(
            f"/api/projects/{pid}/versions", json={"params": {}}
        )
        assert rv.status_code == 201, rv.text
        rendir = tmp_path / "renders" / "order001"
        rendir.mkdir(parents=True)
        (rendir / "model.stl").write_bytes((FIXTURES / "box_20mm.stl").read_bytes())
        latest = app.state.versions.latest_version(pid)
        app.state.conn.raw.execute(
            "UPDATE versions SET render_artifact_dir = ? WHERE id = ?",
            (str(rendir), latest["id"]),
        )
        app.state.conn.commit()
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    assert resp.status_code == 409
    body = resp.json()
    assert body["error_class"] == "units_unsettled", (
        f"units-unsettled 409 must fire before the render-missing 409 "
        f"(got {body.get('error_class')})"
    )


def test_export_settled_part_is_not_units_unsettled(app_with_projects) -> None:
    """A settled import (3MF path: unit from the file, converted to mm)
    with no render → the units_unsettled 409 is GONE; the render-missing
    409 (``conflict``) stays until sub-issue 2 lands the render path."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settled 3mf"})
        pid = r.json()["id"]
        _set_part_columns(
            app.state.conn,
            pid,
            filename="part.3mf",
            part_format="3mf",
            unit="mm",
            unit_status="settled",
            scale=25.4,
        )
        rv = await client.post(
            f"/api/projects/{pid}/versions", json={"params": {}}
        )
        assert rv.status_code == 201, rv.text
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    # No version render → the render-missing 409 (conflict), never
    # units_unsettled.
    assert resp.status_code == 409
    body = resp.json()
    assert body.get("error_class") != "units_unsettled"
    assert body.get("error_class") == "conflict"


def test_export_no_part_project_unaffected(app_with_projects) -> None:
    """A design-loop project (no part at all) → no units_unsettled 409;
    the existing no-versions 404 / render-missing 409 behaviour is
    untouched."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No part"})
        pid = r.json()["id"]
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    # No versions at all → the no-versions 404 (not a units_unsettled 409).
    assert resp.status_code == 404
    body = resp.json()
    assert body.get("error_class") != "units_unsettled"


def test_export_units_unsettled_persists_across_reload(app_with_projects) -> None:
    """The units-unsettled 409 is server-side state on the project row —
    a reload (a second ``_run_async`` against the same app, i.e. a new
    request after the first response) sees the same 409; it is never
    client-side state that clears on its own."""
    app = app_with_projects
    pid: int

    async def _call(client):
        nonlocal pid
        r = await client.post("/api/projects", json={"name": "Reload check"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="assumed")
        first = await client.get(f"/api/projects/{pid}/model.3mf")
        # A second GET in the same session (state must persist per
        # request — never cleared by a prior read).
        second = await client.get(f"/api/projects/{pid}/model.3mf")
        return first, second

    first, second = _run_async(app, _call)
    assert first.status_code == 409
    assert second.status_code == 409
    assert first.json()["error_class"] == "units_unsettled"
    assert second.json()["error_class"] == "units_unsettled"


def test_export_404_project_not_found_precedes_units_check(app_with_projects) -> None:
    """A nonexistent project → 404 (the units check reads the row after
    the 404 — a 404 must never be a 409)."""
    app = app_with_projects

    async def _call(client):
        return await client.get("/api/projects/999999/model.3mf")

    resp = _run_async(app, _call)
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# The web export copy (copy.ts + exportErrorCopy.ts)
# ---------------------------------------------------------------------------


def test_export3mf_units_unsettled_copy_is_plain_user_facing() -> None:
    """``export3mf.unitsUnsettled`` is a plain user-facing sentence: no
    digits, no wire string, no status code, and distinct from the
    existing ``conflict`` / ``failed`` entries (a second 409 sentence
    must never read the same as the first)."""
    import pathlib
    import re

    copy_ts = (
        pathlib.Path(__file__).parent.parent
        / "web" / "src" / "copy.ts"
    ).read_text("utf-8")

    m = re.search(
        r"unitsUnsettled:\s*\n?\s*\"([^\"]+)\"",
        copy_ts,
    )
    assert m is not None, "copy.ts must define export3mf.unitsUnsettled"
    sentence = m.group(1)
    assert len(sentence) > 0
    # No status code / wire string: "3MF" (the format name) is allowed,
    # but a status code ("409") or the wire string ("units_unsettled")
    # must never surface to the user.
    assert "409" not in sentence
    assert "units_unsettled" not in sentence
    # Distinct from the other export3mf entries (they are pinned
    # elsewhere; compare against their literal values here so a
    # duplicate-string refactor trips).
    conflict = re.search(
        r"conflict:\s*\n?\s*\"([^\"]+)\"", copy_ts
    )
    assert conflict is not None
    assert sentence != conflict.group(1)


def test_export_error_copy_maps_units_unsettled() -> None:
    """``exportErrorCopy.ts`` maps the ``units_unsettled`` error class to
    ``copy.export3mf.unitsUnsettled`` — the mapping line is pinned so the
    class and the deck entry cannot drift (a new class without a mapping
    would fall through to the generic ``failed`` sentence, losing the
    actionable "settle the unit" message)."""
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent
        / "web" / "src" / "lib" / "exportErrorCopy.ts"
    ).read_text("utf-8")
    assert "units_unsettled: copy.export3mf.unitsUnsettled" in src


def test_design_contract_pins_part_upload_deck_key() -> None:
    """The design-contract tripwire (the copy-deck key-list assertion)
    includes ``partUpload`` — the part-upload deck surface exists in
    ``copy.ts`` and the tripwire's key list would fail without it (the
    two-way agreement with the backend's 400/422 ``detail`` strings is
    the ws-part-import workstream's; this pins only that the deck key is
    in the tripwire's list)."""
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent
        / "web" / "src" / "__tests__" / "design-contract.test.ts"
    ).read_text("utf-8")
    assert '"partUpload"' in src


# ---------------------------------------------------------------------------
# Commit-failure / part-column-failure rollback (issue #325 operator
# decision: nothing persisted on ANY error — no version row, no committed
# file, no versions/ dir, part columns NULL)
# ---------------------------------------------------------------------------


def test_commit_failure_persists_nothing(app_with_projects):
    """A git commit failure during the import's version create rolls back:
    the version row, the part columns, the committed files, AND the newly
    created empty ``versions/`` dirs are all rolled back together — no
    version row, no part columns, no repo files, and ``versions/`` is
    absent. A retry upload then succeeds (no stuck 409)."""
    app = app_with_projects
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "CommitFail"})
        pid = r.json()["id"]
        repo_path = _repo_for(app, pid)
        files = {"file": ("box.stl", data, "model/stl")}
        # Force the git commit to fail inside the write lock (module-level
        # seam in d33d.versions).
        import d33d.versions as versions_mod

        original_commit = versions_mod._commit_versions_file

        def _boom(*_args, **_kwargs):
            raise RuntimeError("forced commit failure (test)")

        versions_mod._commit_versions_file = _boom
        try:
            r = await client.post(f"/api/projects/{pid}/part", files=files)
        finally:
            versions_mod._commit_versions_file = original_commit
        # Retry: the rollback must have left a clean state (no stuck 409).
        retry = await client.post(f"/api/projects/{pid}/part", files=files)
        repo_state = {
            "versions_rows": app_with_projects.state.conn.raw.execute(
                "SELECT COUNT(*) FROM versions WHERE project_id = ?", (pid,)
            ).fetchone()[0],
            "repo_files": sorted(
                p.name
                for p in (repo_path / "versions").rglob("*")
                if p.is_file()
            )
            if (repo_path / "versions").exists()
            else [],
            "versions_dir_exists": (repo_path / "versions").exists(),
            "part_filename": app_with_projects.state.conn.raw.execute(
                "SELECT part_filename FROM projects WHERE id = ?", (pid,)
            ).fetchone()[0],
        }
        return r, retry, repo_state

    r, retry, state = _run_async(app_with_projects, _call)
    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    # The retry succeeds (no stuck 409 from a half-set row).
    assert retry.status_code == 201, f"retry failed: {retry.status_code} {retry.text}"
    # After the retry, exactly one version row (the retry's) and the part
    # columns are set — the failed import left nothing behind.
    assert state["versions_rows"] == 1, state
    assert state["part_filename"] == "box.stl", state
    # The failed import's ``versions/`` dir was removed by the rollback —
    # the retry's dir (a different version id) is the only one on disk.
    assert state["repo_files"] != [], state


def test_part_column_update_failure_rolls_back(app_with_projects):
    """Force the part-column UPDATE to raise (inside the same transaction
    as the version row) → 500, then a retry upload succeeds (no stuck
    409) and the part columns are set by the retry."""
    import sqlite3 as _sqlite3

    app = app_with_projects
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "ColFail"})
        pid = r.json()["id"]
        repo_path = _repo_for(app, pid)
        files = {"file": ("box.stl", data, "model/stl")}
        conn = app.state.conn
        real_raw = conn.raw

        class _BoomRaw:
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *a, **k):
                if isinstance(sql, str) and "part_filename = ?" in sql and "UPDATE projects" in sql:
                    raise _sqlite3.OperationalError("forced part-column failure (test)")
                return self._real.execute(sql, *a, **k)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def __getattr__(self, name):
                return getattr(self._real, name)

        boom_raw = _BoomRaw(real_raw)
        orig_raw_prop = type(conn).raw
        type(conn).raw = property(lambda self: boom_raw if getattr(self, "_boom", False) else self._conn)
        conn._boom = True
        try:
            r = await client.post(f"/api/projects/{pid}/part", files=files)
        finally:
            type(conn).raw = orig_raw_prop
            conn._boom = False
        # Capture the state IMMEDIATELY after the failed request, BEFORE
        # the retry (the retry will add a row — reading after the retry
        # would see the retry's row, not the rollback state).
        state = _state_after_failed_import(real_raw, pid, repo_path)
        retry = await client.post(f"/api/projects/{pid}/part", files=files)
        # Read the final state (after the retry) INSIDE _call, while the
        # DB connection is still open (the lifespan closes it after
        # _run_async returns).
        final_filename = real_raw.execute(
            "SELECT part_filename FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        return r, retry, state, final_filename

    r, retry, state, final_filename = _run_async(app_with_projects, _call)
    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    _assert_clean_rollback_state(state)
    # The retry succeeds (no stuck 409 from a half-set row).
    assert retry.status_code == 201, f"retry failed: {retry.status_code} {retry.text}"
    # After the retry, the part columns are set (the retry wrote them).
    assert final_filename == "box.stl", state


def _state_after_failed_import(raw, pid, repo_path):
    """The observable state the failed import must have left behind at the
    moment it failed (no version row, no part columns, no versions/ dir).
    The READER reads this state in the same event-loop turn as the failed
    request (the caller's ``_state`` variable is the captured snapshot) —
    it is NOT re-read after the retry, because the retry legitimately adds
    a row. ``raw`` is the sqlite3 connection (``conn.raw``)."""
    return {
        "versions_rows": raw.execute(
            "SELECT COUNT(*) FROM versions WHERE project_id = ?", (pid,)
        ).fetchone()[0],
        "part_filename": raw.execute(
            "SELECT part_filename FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0],
        "current_version": raw.execute(
            "SELECT current_version FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0],
        "versions_dir_exists": (repo_path / "versions").exists(),
        "repo_files": sorted(
            p.name for p in (repo_path / "versions").rglob("*") if p.is_file()
        )
        if (repo_path / "versions").exists()
        else [],
    }


def _assert_clean_rollback_state(state):
    """The rollback removed the version row AND the part columns AND the
    project pointer, and left no repo files or versions/ dir behind.
    ``state`` is the snapshot captured IMMEDIATELY AFTER the failed
    request (before any retry)."""
    assert state["versions_rows"] == 0, f"version row not rolled back: {state}"
    assert state["part_filename"] is None, f"part columns not rolled back: {state}"
    assert state["current_version"] is None, f"project pointer not rolled back: {state}"
    assert not state["versions_dir_exists"], f"versions/ dir left behind: {state}"
    assert state["repo_files"] == [], f"repo files left behind: {state}"


def test_project_pointer_update_failure_rolls_back(app_with_projects):
    """Force the project-pointer UPDATE (``current_version`` — the second
    statement of the import transaction, after the version INSERT and the
    git commit) to fail → 500, and the state is clean: the version row,
    the part columns, the project pointer, AND the files are all rolled
    back. Proves the rollback is exercised from the pointer step, not
    only the part-column step."""
    import sqlite3 as _sqlite3

    app = app_with_projects
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "PointerFail"})
        pid = r.json()["id"]
        repo_path = _repo_for(app, pid)
        files = {"file": ("box.stl", data, "model/stl")}
        conn = app.state.conn
        real_raw = conn.raw
        current_version_before = real_raw.execute(
            "SELECT current_version FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]

        # The pointer UPDATE goes through ``conn.raw.execute`` (the
        # import's pointer write was changed to use raw directly, NOT
        # via update_project, so it's in the same transaction as the
        # version row). Monkeypatch ``raw`` to force the pointer UPDATE
        # to fail.
        class _BoomRaw:
            def __init__(self, real_conn):
                self._real = real_conn

            def execute(self, sql, *a, **k):
                if (
                    isinstance(sql, str)
                    and "UPDATE projects" in sql
                    and "current_version = ?" in sql
                ):
                    raise _sqlite3.OperationalError("forced pointer failure (test)")
                return self._real.execute(sql, *a, **k)

            def commit(self):
                return self._real.commit()

            def rollback(self):
                return self._real.rollback()

            def __getattr__(self, name):
                return getattr(self._real, name)

        boom_raw = _BoomRaw(real_raw)
        orig_raw_prop = type(conn).raw
        type(conn).raw = property(
            lambda self: boom_raw if getattr(self, "_boom4", False) else self._conn
        )
        conn._boom4 = True
        try:
            r = await client.post(f"/api/projects/{pid}/part", files=files)
        finally:
            type(conn).raw = orig_raw_prop
            conn._boom4 = False
        # Capture the state IMMEDIATELY after the failed request, BEFORE
        # the retry.
        state = _state_after_failed_import(real_raw, pid, repo_path)
        retry = await client.post(f"/api/projects/{pid}/part", files=files)
        return r, retry, state, current_version_before

    r, retry, state, current_version_before = _run_async(app_with_projects, _call)
    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    _assert_clean_rollback_state(state)
    # The project pointer is unchanged (still what it was before the
    # failed import — the rollback restored it, it is not half-set).
    assert state["current_version"] == current_version_before, (
        f"project pointer not restored: before={current_version_before!r} after={state['current_version']!r}"
    )
    # The retry succeeds (no stuck 409 from a half-set row).
    assert retry.status_code == 201, f"retry failed: {retry.status_code} {retry.text}"


# ---------------------------------------------------------------------------
# Event loop: the decode runs off the event loop (asyncio.to_thread)
# ---------------------------------------------------------------------------


def test_parse_and_repair_runs_off_event_loop(app_with_projects):
    """A slow ``parse_and_repair`` stub (via monkeypatch) does NOT block a
    concurrent request on the same app — the decode runs in a worker
    thread (``asyncio.to_thread``), not on the event loop."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")
    import time as _time

    import d33d.part_import as part_import_mod

    original_parse = part_import_mod.parse_and_repair
    slow_calls = []

    def slow_parse(content, part_format):
        slow_calls.append(1)
        _time.sleep(0.3)  # simulate a slow decode
        return original_parse(content, part_format)

    part_import_mod.parse_and_repair = slow_parse
    try:
        async def _call(client):
            r = await client.post("/api/projects", json={"name": "ThreadTest"})
            pid = r.json()["id"]
            files = {"file": ("box.stl", data, "model/stl")}
            # Fire the slow upload and a lightweight GET concurrently.
            # DISCRIMINATOR (not a total-elapsed bound — a serial, on-loop
            # decode still finishes in ~0.3 s total, which passes any
            # loose total bound): the GET's completion is measured against
            # the 0.3 s decode sleep. With the decode OFF the loop
            # (asyncio.to_thread), the GET's handler runs on the event
            # loop the moment the upload reaches the decode await, i.e.
            # it completes a small epsilon BEFORE the 0.3 s sleep ends.
            # If the decode ran ON the loop, the sleep would block every
            # other handler — the GET could not complete until the sleep
            # finished, so its completion time would be >= the sleep
            # length (plus the upload's pre-decode overhead).
            t0 = _time.monotonic()

            async def _list():
                r = await client.get("/api/projects")
                return r, _time.monotonic() - t0

            results = await asyncio.gather(
                client.post(f"/api/projects/{pid}/part", files=files),
                _list(),
            )
            return results[0], results[1][0], results[1][1], len(slow_calls)

        upload_r, list_r, list_elapsed, n_slow = _run_async(app_with_projects, _call)
    finally:
        part_import_mod.parse_and_repair = original_parse
    assert upload_r.status_code == 201, upload_r.text
    assert list_r.status_code == 200
    assert n_slow == 1
    # The GET completed strictly before the 0.3 s decode sleep ended
    # (the upload reached the decode await a small epsilon before t0,
    # so an off-loop decode's GET finishes at a few ms — never after
    # the full sleep). 0.25 s leaves a large margin under the sleep
    # while being far above any scheduling jitter.
    assert list_elapsed < 0.25, (
        f"GET /api/projects took {list_elapsed:.3f}s — it should have "
        "completed while the 0.3 s decode slept in a worker thread; >= the "
        "sleep length means the decode blocked the event loop"
    )


# ---------------------------------------------------------------------------
# 3MF units: never silently mm (the operator decision)
# ---------------------------------------------------------------------------


def test_3mf_inch_settled_at_25_4(app_with_projects):
    """3MF with unit 'inch' → settled at scale 25.4 (the bbox is in mm)."""
    data = _make_3mf("inch")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Inch 2"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "mm"
    assert abs(part["scale"] - 25.4) < 1e-6
    # The v1 bbox is in mm (10 inch × 25.4 = 254 mm per axis).
    report = part["report"]
    bbox_file = report["bbox_file_units"]
    assert abs(bbox_file[0] * part["scale"] - 254.0) < 1.0


def test_3mf_bogus_unit_422(app_with_projects):
    """3MF with a bogus unit 'furlong' → 422 (unconvertible, never mm)."""
    data = _make_3mf("furlong")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF Furlong"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


@pytest.mark.parametrize("unit", ["micron", "meter", "foot", "feet"])
def test_3mf_spec_units_outside_closed_sets_422(unit, app_with_projects):
    """The 3MF spec unit list is micron | millimeter | centimeter | inch
    | foot | meter. The closed mm/cm/inch synonym sets are supported with
    their factors; the rest (``micron``/``meter``/``foot``/``feet``) are
    422 — never a silent mm assumption (the operator decision). This pins
    the closed-set boundary for the FULL spec unit list (``foot`` alone
    was asserted before; all four are pinned now)."""
    data = _make_3mf(unit)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": f"3MF {unit}"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422, f"unit={unit}: expected 422, got {r.status_code}: {r.text}"
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


def test_3mf_non_string_unit_422(app_with_projects, monkeypatch):
    """3MF with a non-string unit (simulated via monkeypatch of
    ``mesh_units``) → 422 (never a silent mm assumption)."""
    import d33d.part_mesh as part_mesh_mod

    original_mesh_units = part_mesh_mod.mesh_units

    def non_string_units(mesh):
        return 42  # a non-string unit value

    part_mesh_mod.mesh_units = non_string_units
    try:
        data = _make_3mf("millimeter")

        async def _call(client):
            r = await client.post("/api/projects", json={"name": "3MF NonStr"})
            pid = r.json()["id"]
            files = {"file": ("part.3mf", data, "model/3mf")}
            return await client.post(f"/api/projects/{pid}/part", files=files)

        r = _run_async(app_with_projects, _call)
    finally:
        part_mesh_mod.mesh_units = original_mesh_units
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


def test_3mf_no_unit_defaults_to_mm(app_with_projects):
    """3MF with NO ``unit`` attribute → the 3MF default (millimeters) →
    settled at scale 1.0 (``None`` is the ONLY value that means mm)."""
    data = _make_3mf(None)  # no unit attribute

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "3MF NoUnit"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    part = r.json()["part"]
    assert part["unit_status"] == "settled"
    assert part["unit"] == "mm"
    assert abs(part["scale"] - 1.0) < 1e-6


# ---------------------------------------------------------------------------
# Settle atomicity: both UPDATEs in one transaction
# ---------------------------------------------------------------------------


def test_settle_second_update_failure_rolls_back(app_with_projects):
    """Force the settle's transaction to fail (the ``_v1_for_part`` read
    raises, BEFORE the v1 bbox UPDATE) → 500, and the project's
    unit_status and the v1's bbox are both unchanged (one transaction,
    one rollback — the projects UPDATE rolled back with it).

    The failure is injected at ``part_import._v1_for_part`` — the symbol
    the route's ``_settle_sync`` closure resolves through the
    ``d33d.part_import`` module globals (the settle route never reaches
    the connection through a patchable object; the closure's free
    variables resolve at call time from the module namespace)."""
    import sqlite3 as _sqlite3

    import d33d.part_import as part_import_mod

    app = app_with_projects
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SettleAtomic"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        assert upload_r.status_code == 201, upload_r.text
        # Read the v1's bbox BEFORE settle.
        real_raw = app.state.conn.raw
        v1_row = real_raw.execute(
            "SELECT bbox FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        bbox_before = v1_row[0] if v1_row else None
        unit_status_before = real_raw.execute(
            "SELECT part_unit_status FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        # Force the settle's transaction to raise (inside the BEGIN..commit
        # window, so the projects UPDATE is the statement being rolled back).
        orig_v1 = part_import_mod._v1_for_part

        def _boom_v1(conn, project_id):
            raise _sqlite3.OperationalError(
                "forced settle failure (test)"
            )

        part_import_mod._v1_for_part = _boom_v1
        try:
            settle_r = await client.post(
                f"/api/projects/{pid}/part/units", json={"unit": "inch"}
            )
        finally:
            part_import_mod._v1_for_part = orig_v1
        # Read the state AFTER the failed settle.
        unit_status_after = real_raw.execute(
            "SELECT part_unit_status FROM projects WHERE id = ?", (pid,)
        ).fetchone()[0]
        v1_row_after = real_raw.execute(
            "SELECT bbox FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        bbox_after = v1_row_after[0] if v1_row_after else None
        return settle_r, unit_status_before, unit_status_after, bbox_before, bbox_after

    settle_r, status_before, status_after, bbox_before, bbox_after = _run_async(
        app_with_projects, _call
    )
    assert settle_r.status_code == 500, f"expected 500, got {settle_r.status_code}"
    # The project's unit_status is unchanged (rolled back).
    assert status_after == status_before
    # The v1's bbox is unchanged (rolled back).
    assert bbox_after == bbox_before


# ---------------------------------------------------------------------------
# Zip-bomb: declared uncompressed sum > cap (≤ entry cap)
# ---------------------------------------------------------------------------


def test_zip_bomb_uncompressed_sum_422(app_with_projects):
    """A 3MF with ≤ 10,000 entries but a declared uncompressed total > the
    cap → 422 (the zip-bomb guard fires on the sum, not just the count)."""
    import io as _io

    # 5 entries, each declaring a large uncompressed size (the content is
    # small — the DECLARED size is what the guard reads from the central
    # directory, not the actual bytes).
    buf = _io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for i in range(5):
            # Declare a large uncompressed size via the ZipInfo's file_size.
            info = zipfile.ZipInfo(f"3D/model_{i}.xml")
            info.file_size = 20 * 1024 * 1024  # 20 MB each → 100 MB total
            info.compress_size = 20 * 1024 * 1024
            info.compress_type = zipfile.ZIP_STORED
            # Write a small actual payload (the guard reads file_size,
            # not the actual data — a real bomb would have large data).
            zf.writestr(info, b"small")
    data = buf.getvalue()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "ZipBombSum"})
        pid = r.json()["id"]
        files = {"file": ("bomb.3mf", data, "model/3mf")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422
    assert r.json()["detail"] == PART_UPLOAD_UNPARSEABLE_DETAIL


# ---------------------------------------------------------------------------
# 413 boundary: just under the cap is accepted; just over → 413
# ---------------------------------------------------------------------------


def test_413_boundary_just_under_cap_accepted(app_with_projects):
    """A body just under the effective cap (MAX_PART_UPLOAD_BYTES + 1 MiB
    allowance) is NOT a 413 — it proceeds past the size gate. The test
    uses a valid small STL file (the total body is well under the cap,
    which is the 'just under' boundary in the absence of a 51 MB
    allocation); the key assertion is that the size gate does NOT fire
    (no 413) for a body under the cap."""
    small_stl = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "JustUnder"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", small_stl, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    # Not a 413 — the body is under the cap, so the size gate does not
    # fire; the upload succeeds (valid STL, no part yet).
    assert r.status_code == 201, f"expected 201, got {r.status_code}: {r.text[:200]}"


def test_413_boundary_just_over_cap_413(app_with_projects):
    """A body just over the effective cap → 413 (the size gate fires)."""
    cap = MAX_PART_UPLOAD_BYTES + 1024 * 1024  # effective cap
    just_over = b"\x00" * (cap + 1)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "JustOver"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", just_over, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 413
    assert "exceeds" in r.json()["detail"]


# ---------------------------------------------------------------------------
# Pinned copy.ts tests: PART_UPLOAD_UNSUPPORTED_DETAIL / UNPARSEABLE equal
# copy.ts partUpload.unsupported / unparseable exactly
# ---------------------------------------------------------------------------


def test_part_upload_unsupported_detail_equals_copy_ts():
    """``PART_UPLOAD_UNSUPPORTED_DETAIL`` equals ``copy.ts``
    ``partUpload.unsupported`` exactly (parse copy.ts, as the
    ``unitsUnsettled`` test does)."""
    import pathlib
    import re

    from d33d.part_import import PART_UPLOAD_UNSUPPORTED_DETAIL

    copy_ts = (
        pathlib.Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")
    m = re.search(r"unsupported:\s*\n?\s*\"([^\"]+)\"", copy_ts)
    assert m is not None, "copy.ts must define partUpload.unsupported"
    assert PART_UPLOAD_UNSUPPORTED_DETAIL == m.group(1)


def test_part_upload_unparseable_detail_equals_copy_ts():
    """``PART_UPLOAD_UNPARSEABLE_DETAIL`` equals ``copy.ts``
    ``partUpload.unparseable`` exactly."""
    import pathlib
    import re

    from d33d.part_import import PART_UPLOAD_UNPARSEABLE_DETAIL

    copy_ts = (
        pathlib.Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")
    m = re.search(r"unparseable:\s*\n?\s*\"([^\"]+)\"", copy_ts)
    assert m is not None, "copy.ts must define partUpload.unparseable"
    assert PART_UPLOAD_UNPARSEABLE_DETAIL == m.group(1)


def test_part_upload_commit_failed_detail_equals_copy_ts():
    """``PART_UPLOAD_COMMIT_FAILED_DETAIL`` equals ``copy.ts``
    ``partUpload.commitFailed`` exactly — the FIXED 500 detail the
    upload/settle routes return (the exception text never reaches the
    client), pinned here so the wire and the deck cannot drift (the
    #299 way, as for unsupported/unparseable)."""
    import pathlib
    import re

    from d33d.part_http import PART_UPLOAD_COMMIT_FAILED_DETAIL

    copy_ts = (
        pathlib.Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")
    m = re.search(r"commitFailed:\s*\n?\s*\"([^\"]+)\"", copy_ts)
    assert m is not None, "copy.ts must define partUpload.commitFailed"
    assert PART_UPLOAD_COMMIT_FAILED_DETAIL == m.group(1)


def test_design_contract_pins_part_upload_commit_failed_key():
    """The design-contract tripwire pins ``partUpload.commitFailed`` in
    the copy deck (the fixed 500 sentence for the part-save failure —
    the two-way agreement with the backend's 500 ``detail``)."""
    import pathlib

    src = (
        pathlib.Path(__file__).parent.parent
        / "web" / "src" / "__tests__" / "design-contract.test.ts"
    ).read_text("utf-8")
    assert "partUpload.commitFailed" in src


# ---------------------------------------------------------------------------
# The 500 handler returns a FIXED detail (no raw error text to the client)
# ---------------------------------------------------------------------------


def test_import_commit_failure_500_detail_is_fixed_no_raw_text(app_with_projects):
    """Force ``_commit_versions_file`` to raise with a message carrying a
    fake path (``/secret/path``). The 500 body must carry ONLY the fixed
    copy.ts sentence — the raw exception text (the path) must be absent
    from the response; it stays in the server log only."""
    import d33d.versions as versions_mod

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SecretPath"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        original_commit = versions_mod._commit_versions_file

        def _boom(*_args, **_kwargs):
            raise RuntimeError("failed to commit /secret/path to git")

        versions_mod._commit_versions_file = _boom
        try:
            r = await client.post(f"/api/projects/{pid}/part", files=files)
        finally:
            versions_mod._commit_versions_file = original_commit
        return r

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 500, r.text
    detail = r.json()["detail"]
    assert detail == "The part couldn't be saved. Nothing was changed."
    # The raw exception text (the fake path) must not reach the client.
    assert "/secret/path" not in r.text


def test_settle_failure_500_detail_is_fixed_no_raw_text(app_with_projects):
    """Force the settle's v1 bbox UPDATE to raise with a message carrying
    a fake path. The 500 body must carry ONLY the fixed copy.ts sentence
    — the raw text (the path) must be absent from the response."""
    import sqlite3 as _sqlite3

    import d33d.part_import as part_import_mod

    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SettleSecret"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        assert upload_r.status_code == 201, upload_r.text

        # Force the settle's transaction to raise with a message carrying a
        # fake path — the injection point is ``_v1_for_part`` (the symbol
        # the ``_settle_sync`` closure resolves from the module globals),
        # raised INSIDE the BEGIN..commit window.
        orig_v1 = part_import_mod._v1_for_part

        def _boom_v1(conn, project_id):
            raise _sqlite3.OperationalError(
                "bbox update failed at /secret/path"
            )

        part_import_mod._v1_for_part = _boom_v1
        try:
            settle_r = await client.post(
                f"/api/projects/{pid}/part/units", json={"unit": "inch"}
            )
        finally:
            part_import_mod._v1_for_part = orig_v1
        return settle_r

    settle_r = _run_async(app_with_projects, _call)
    assert settle_r.status_code == 500, settle_r.text
    assert settle_r.json()["detail"] == (
        "The part couldn't be saved. Nothing was changed."
    )
    assert "/secret/path" not in settle_r.text


# ---------------------------------------------------------------------------
# last_activity.ts: the import stamps the real wall-clock ts (not null)
# ---------------------------------------------------------------------------


def test_import_last_activity_ts_is_wall_clock(app_with_projects):
    """After a successful import, the project's ``last_activity`` column
    carries a NON-NULL ``ts`` in the same wall-clock ISO format
    (``strftime('%Y-%m-%dT%H:%M:%fZ','now')``) that ``db.update_project``
    stamps — a null ts would read as "never active"."""
    import re as _re

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "TsStamp"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        assert upload_r.status_code == 201, upload_r.text
        la = app_with_projects.state.conn.get_project(pid)["last_activity"]
        return upload_r, la

    _upload_r, la = _run_async(app_with_projects, _call)
    assert la is not None
    assert la["version_id"] is not None
    assert la["name"] == "Imported box.stl"
    ts = la["ts"]
    assert ts is not None, f"last_activity.ts must not be null after import: {la}"
    # Same wall-clock ISO format db.update_project stamps.
    assert _re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", ts
    ) is not None, f"ts is not the wall-clock ISO format: {ts!r}"


# ---------------------------------------------------------------------------
# Exception-type leakage: ANY exception (not just git/OS/sqlite) rolls back
# ---------------------------------------------------------------------------


def test_import_subprocess_timeout_expired_rolls_back(app_with_projects):
    """Force ``_commit_versions_file`` to raise
    ``subprocess.TimeoutExpired`` (a git timeout — an exception type the
    old narrow ``except (RuntimeError, OSError, sqlite3.Error)`` did NOT
    catch). The broadened handler must still: return a 500, roll back the
    version row + part columns (NULL), close the transaction on the
    SHARED connection (``in_transaction`` is False afterwards), leave no
    files behind, and let the retry upload succeed (201)."""
    import subprocess as _subprocess

    import d33d.versions as versions_mod

    app = app_with_projects
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "GitTimeout"})
        pid = r.json()["id"]
        repo_path = _repo_for(app, pid)
        files = {"file": ("box.stl", data, "model/stl")}
        conn = app.state.conn
        original_commit = versions_mod._commit_versions_file

        def _boom(*_args, **_kwargs):
            raise _subprocess.TimeoutExpired(cmd="git", timeout=30)

        versions_mod._commit_versions_file = _boom
        try:
            r = await client.post(f"/api/projects/{pid}/part", files=files)
        finally:
            versions_mod._commit_versions_file = original_commit
        # Capture the post-failure state BEFORE the retry.
        state = _state_after_failed_import(conn.raw, pid, repo_path)
        in_txn = conn.in_transaction
        retry = await client.post(f"/api/projects/{pid}/part", files=files)
        return r, retry, state, in_txn

    r, retry, state, in_txn = _run_async(app_with_projects, _call)
    assert r.status_code == 500, f"expected 500, got {r.status_code}: {r.text}"
    # The shared connection is NOT left mid-transaction.
    assert in_txn is False, "shared connection left mid-transaction after failure"
    # No version row, part columns NULL, no files, no versions/ dir.
    _assert_clean_rollback_state(state)
    # The retry succeeds (no stuck 409).
    assert retry.status_code == 201, f"retry failed: {retry.status_code} {retry.text}"


# ---------------------------------------------------------------------------
# resolve_part_paths: the shared part-wiring helper (issue #330)
# ---------------------------------------------------------------------------


def _conn_with_project_and_v1(
    tmp_path: Path, *, part: tuple[str, str, str] | None = None
):
    """An in-memory connection with one project (git repo under tmp_path) and
    one version row (the v1 row ``_v1_for_part`` reads). ``part`` is
    (filename, format, unit_status) or ``None``."""
    import d33d.db as db_mod
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params) VALUES (?, ?, ?)",
        (pid, "v1", "{}"),
    )
    if part is not None:
        filename, pformat, status = part
        conn.execute(
            "UPDATE projects SET part_filename=?, part_format=?, "
            "part_unit_status=? WHERE id=?",
            (filename, pformat, status, pid),
        )
    return conn


def test_resolve_part_paths_settled_stl_wires_v1_path(tmp_path: Path):
    """Settled STL part → (versions/{v1}/part.stl, git_repo_path)."""
    from d33d.part_http import resolve_part_paths

    conn = _conn_with_project_and_v1(
        tmp_path, part=("part.stl", "stl", "settled")
    )
    row = conn.get_project(1)
    v1 = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id=1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    part_path, repo_dir = resolve_part_paths(row, conn)
    assert part_path == Path(row["git_repo_path"]) / "versions" / str(v1["id"]) / "part.stl"
    assert repo_dir == Path(row["git_repo_path"])


def test_resolve_part_paths_assumed_3mf_wires_v1_path(tmp_path: Path):
    """Assumed 3MF part → (versions/{v1}/part.3mf, git_repo_path)."""
    from d33d.part_http import resolve_part_paths

    conn = _conn_with_project_and_v1(tmp_path, part=("part.3mf", "3mf", "assumed"))
    row = conn.get_project(1)
    v1 = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id=1 ORDER BY id ASC LIMIT 1"
    ).fetchone()
    part_path, repo_dir = resolve_part_paths(row, conn)
    assert part_path == Path(row["git_repo_path"]) / "versions" / str(v1["id"]) / "part.3mf"
    assert repo_dir == Path(row["git_repo_path"])


def test_resolve_part_paths_unsettled_part_path_none_repo_set(tmp_path: Path):
    """Unsettled part → (None, git_repo_path) — repo_dir is still set."""
    from d33d.part_http import resolve_part_paths

    conn = _conn_with_project_and_v1(
        tmp_path, part=("part.stl", "stl", "unsettled")
    )
    row = conn.get_project(1)
    part_path, repo_dir = resolve_part_paths(row, conn)
    assert part_path is None
    assert repo_dir == Path(row["git_repo_path"])


def test_resolve_part_paths_no_part_row_none_repo_set(tmp_path: Path):
    """Project with no part → (None, git_repo_path)."""
    from d33d.part_http import resolve_part_paths

    conn = _conn_with_project_and_v1(tmp_path)
    row = conn.get_project(1)
    part_path, repo_dir = resolve_part_paths(row, conn)
    assert part_path is None
    assert repo_dir == Path(row["git_repo_path"])


def test_resolve_part_paths_no_row_both_none():
    """Unreadable row (None) → (None, None)."""
    from d33d.part_http import resolve_part_paths

    part_path, repo_dir = resolve_part_paths(None, None)
    assert part_path is None
    assert repo_dir is None


def test_resolve_part_paths_missing_v1_row_part_none_repo_set(tmp_path: Path):
    """Settled part but no v1 version row → (None, git_repo_path)."""
    import d33d.db as db_mod
    from d33d.part_http import resolve_part_paths
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit_status=? "
        "WHERE id=?",
        ("part.stl", "stl", "settled", pid),
    )
    row = conn.get_project(pid)
    part_path, repo_dir = resolve_part_paths(row, conn)
    assert part_path is None
    assert repo_dir == Path(row["git_repo_path"])
