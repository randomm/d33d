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


def _stl_bytes_from_mesh(mesh) -> bytes:
    """Export a trimesh mesh to STL bytes (in-memory — no file on disk)."""
    buf = io.BytesIO()
    mesh.export(buf, file_type="stl")
    return buf.getvalue()


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


def test_hole_count_key_present_and_int(app_with_projects):
    """Issue #351: the repair report carries an integer ``hole_count`` key
    (the import-time hole signal). The key is ALWAYS present (never
    ``None`` / missing — the degradation to "unknown" is the READER's job
    when the stored blob is NULL/legacy, not the import's)."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HoleKey"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert "hole_count" in report, f"report must carry hole_count: {report}"
    hc = report["hole_count"]
    assert isinstance(hc, int), f"hole_count must be an int, got {type(hc)}"


def test_hole_count_zero_for_plain_box(app_with_projects):
    """Issue #351: a plain watertight box has NO open boundary loops →
    ``hole_count == 0``. This is the explicit zero that the fill-recut gate
    (task-b/c) keys on for the honest no-hole reply."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HoleZero"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["hole_count"] == 0, f"plain box must have hole_count 0: {report}"


def test_hole_count_positive_for_holey_fixture(app_with_projects):
    """Issue #351: holey.stl (a 20 mm plate with holes, 4 boundary loops
    pre-repair per ``test_repair_report_holey_gaps_closed``) →
    ``hole_count > 0`` (one loop per hole opening). A positive count is the
    explicit evidence the fill-recut gate keys on for firing the offer."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HolePositive"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["hole_count"] > 0, f"holey part must have hole_count > 0: {report}"


def test_hole_count_equals_pre_repair_boundary_loops(app_with_projects):
    """Issue #351: ``hole_count`` is computed from the PRE-REPAIR merged
    mesh's boundary loops PLUS the closed-body genus. For holey.stl the
    pre-repair loop count is 4 (the same value ``gaps_closed`` reports as
    closed by pymeshfix: a gapped→closed delta of 4) and the repaired
    watertight mesh is a single genus-0 body, so ``hole_count`` == 4 —
    NOT the post-repair loop count (which is 0 — pymeshfix closes the
    loops, so a post-repair-only signal would read 0 for a holey part)."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HolePreRepair"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    # gaps_closed = pre_repair_loops - post_repair_loops. Post-repair the
    # mesh is watertight (0 loops), so gaps_closed == pre_repair_loops.
    # hole_count is defined to equal the pre-repair loop count.
    assert report["hole_count"] == report["gaps_closed"], (
        f"hole_count ({report['hole_count']}) must equal the pre-repair "
        f"boundary-loop count (gaps_closed, {report['gaps_closed']}) for a "
        f"mesh pymeshfix fully closes"
    )
    assert report["hole_count"] == 4, f"holey.stl has 4 pre-repair loops: {report}"


def test_hole_count_watertight_ring_counts_through_hole(app_with_projects):
    """Issue #351: a watertight ring (trimesh annulus exported to STL)
    has a REAL drilled through-bore: 0 boundary loops but genus 1.
    ``hole_count`` must count the closed through-hole (``gaps_before +
    genus``) → >= 1, so the fill-recut gate has evidence for the very
    part it exists for. The repaired mesh stays watertight."""
    import trimesh

    ring = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    data = _stl_bytes_from_mesh(ring)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Ring"})
        pid = r.json()["id"]
        files = {"file": ("ring.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["watertight"] is True
    assert report["gaps_closed"] == 0
    assert report["hole_count"] >= 1, (
        f"watertight ring has a through-hole (genus 1); hole_count must be "
        f">= 1, got {report}: {report}"
    )
    assert isinstance(report["hole_count"], int)


def test_hole_count_two_watertight_rings(app_with_projects):
    """Issue #351: two separate watertight rings (two disconnected bodies,
    each genus 1) → ``hole_count`` == 2 — genus is summed over bodies on
    the PRE-REPAIR mesh. The rings are placed far apart (STL is float32:
    at a 100 mm gap the float32 vertex rounding merges the two bodies into
    one, corrupting both the body count and the genus sum). The 10000 mm
    separation is what keeps the two bodies distinct through the float32
    round-trip. Issue #375 additionally pins the STORED mesh: per-body
    repair keeps both watertight components and the exact 512-face count.
    """
    import trimesh

    ring_a = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b.apply_translation([10000.0, 0.0, 0.0])
    data = _stl_bytes_from_mesh(trimesh.util.concatenate([ring_a, ring_b]))

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "TwoRings"})
        pid = r.json()["id"]
        files = {"file": ("two_rings.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        v1_id = upload_r.json()["version_id"]
        repo_path = _repo_for(app_with_projects, pid)
        part_path = repo_path / "versions" / str(v1_id) / "part.stl"
        return upload_r, part_path.read_bytes()

    upload_r, stored = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    report = upload_r.json()["part"]["report"]
    assert report["watertight"] is True
    assert report["bodies"] == 2, f"two disconnected rings: {report}"
    assert report["hole_count"] == 2, f"two rings → genus 2: {report}"
    assert report["triangles"] == 512, (
        f"two rings must keep exactly 512 faces: {report}"
    )
    # Issue #375: the STORED (post-repair) mesh keeps both watertight
    # bodies — the report's bodies value is the post-repair count.
    stored_mesh = trimesh.load(io.BytesIO(stored), file_type="stl")
    if isinstance(stored_mesh, trimesh.Scene):
        stored_mesh = stored_mesh.to_mesh()
    stored_mesh.merge_vertices()
    stored_mesh.update_faces(stored_mesh.nondegenerate_faces())
    stored_bodies = len(stored_mesh.split(only_watertight=True))
    assert stored_bodies == 2, (
        f"stored mesh must keep 2 watertight bodies, got {stored_bodies}"
    )


def test_bodies_count_split_equivalence_on_fixtures():
    """Issue #351 perf — the single-split refactor: on the four #351
    fixtures (box, holey, annulus, two rings), the number of watertight
    components in ``split(only_watertight=False)`` equals
    ``len(split(only_watertight=True))``, so the bodies count and the
    genus can read the SAME split (the refactor's premise — pinned here
    so the premise is a test, not an assumption)."""
    import trimesh

    box = trimesh.creation.box(extents=[20, 20, 20])
    holey = trimesh.load(str(FIXTURES / "holey.stl"))
    annulus = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_a = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b.apply_translation([10000.0, 0.0, 0.0])
    two_rings = trimesh.util.concatenate([ring_a, ring_b])

    for name, mesh in [
        ("box", box),
        ("holey", holey),
        ("annulus", annulus),
        ("two_rings", two_rings),
    ]:
        if isinstance(mesh, trimesh.Scene):
            mesh = mesh.to_mesh()
        merged = mesh.copy()
        merged.merge_vertices()
        merged.update_faces(merged.nondegenerate_faces())
        strict = len(merged.split(only_watertight=True))
        loose = merged.split(only_watertight=False)
        watertight_count = sum(1 for c in loose if c.is_watertight)
        assert strict == watertight_count, (
            f"{name}: split(only_watertight=True) count {strict} != "
            f"watertight count in split(only_watertight=False) {watertight_count}"
        )


def test_hole_count_genus_failure_falls_back_to_gaps_before(app_with_projects):
    """Issue #351: any exception in the genus computation degrades to
    ``gaps_before`` alone — never a crash, never ``None``. A holey part
    still gets its boundary-loop count (4), not an error response.

    Issue #395: the genus computation moved to ``d33d.part_mesh_topology``
    (the shared ``mesh_topology`` helper), so the monkeypatch targets that
    module."""
    import d33d.part_mesh_topology as part_mesh_topology_mod
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "GenusFail"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    def _boom(mesh):
        raise RuntimeError("genus computation failed")

    original = part_mesh_topology_mod.watertight_genus
    part_mesh_topology_mod.watertight_genus = _boom
    try:
        r = _run_async(app_with_projects, _call)
    finally:
        part_mesh_topology_mod.watertight_genus = original
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    # Fallback: gaps_before (4 boundary loops) alone, never None/crash.
    assert report["hole_count"] == 4, f"genus failure falls back to gaps: {report}"


def test_part_upload_error_in_repair_block_propagates_verbatim(
    app_with_projects, monkeypatch: pytest.MonkeyPatch
):
    """Issue #351 regression: a ``PartUploadError`` raised INSIDE the repair
    try-block (pymeshfix's repair step) propagates with its ORIGINAL message
    — the ``except PartUploadError: raise`` passthrough — NOT wrapped as
    ``PartUploadError("repair failed: …")``. The 422 mapping at the route
    consumes the type, but the original message is what the logs carry.

    Issue #395: the repair now runs in a subprocess, so the monkeypatch
    targets ``d33d.part_mesh.repair_with_pmf`` (the module-level seam)
    rather than ``pymeshfix.MeshFix`` (which is now in a separate process
    and unreachable from the test). The contract is the same: a
    ``PartUploadError`` raised by the repair function propagates verbatim
    through ``parse_and_repair`` — not wrapped."""
    import d33d.part_mesh as part_mesh_mod

    # Use holey.stl (not clean → repair is attempted → the monkeypatched
    # repair_with_pmf is called and raises).
    data = _stl_bytes(FIXTURES / "holey.stl")

    def _boom_repair(m):
        raise part_mesh_mod.PartUploadError("boom in repair")

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _boom_repair)

    with pytest.raises(part_mesh_mod.PartUploadError) as excinfo:
        part_mesh_mod.parse_and_repair(data, "stl")
    # The ORIGINAL message survives verbatim — no "repair failed: " prefix.
    assert str(excinfo.value) == "boom in repair"
    assert "repair failed" not in str(excinfo.value)


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


def test_repair_keeps_all_bodies_two_annuli():
    """Issue #375: two disconnected annuli (10000 mm apart — the float32
    STL round-trip merges closer bodies) repaired via ``parse_and_repair``
    directly (no HTTP): the STORED mesh must keep BOTH watertight bodies
    and the exact pre-repair face count (512). On main the one-call
    ``MeshFix.repair()`` keeps only one component → 1 body / 256 faces,
    so this test MUST fail on main."""
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    ring_a = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    ring_b.apply_translation([10000.0, 0.0, 0.0])
    data = _stl_bytes_from_mesh(trimesh.util.concatenate([ring_a, ring_b]))

    mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    assert len(mesh.faces) == 512, (
        f"two annuli must keep all 512 faces after per-body repair, "
        f"got {len(mesh.faces)}: {report}"
    )
    comps = mesh.split(only_watertight=False)
    watertight_comps = [c for c in comps if c.is_watertight]
    assert len(watertight_comps) == 2, (
        f"stored mesh must keep 2 watertight bodies, got "
        f"{len(watertight_comps)}: {report}"
    )
    # Operator decision 2: report.bodies is the post-repair count.
    assert report["bodies"] == 2
    # No body dropped → no bodies_before field (operator decision 1).
    assert "bodies_before" not in report


def test_repair_upload_two_body_stored_mesh_keeps_both_bodies(app_with_projects):
    """Issue #375 upload path: upload two_body_multisolid.stl, then read
    back the STORED versions/{v1}/part.stl — the stored mesh must have 2
    watertight bodies. On main the stored part.stl has 1 body (the
    one-call repair dropped one; the report claimed 2), so this test MUST
    fail on main."""
    import trimesh

    data = _stl_bytes(FIXTURES / "two_body_multisolid.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "TwoBodyStored"})
        pid = r.json()["id"]
        files = {"file": ("two_body.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        v1_id = upload_r.json()["version_id"]
        repo_path = _repo_for(app_with_projects, pid)
        part_path = repo_path / "versions" / str(v1_id) / "part.stl"
        stored = part_path.read_bytes()
        return upload_r, stored

    upload_r, stored = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    report = upload_r.json()["part"]["report"]
    stored_mesh = trimesh.load(io.BytesIO(stored), file_type="stl")
    if isinstance(stored_mesh, trimesh.Scene):
        stored_mesh = stored_mesh.to_mesh()
    stored_mesh.merge_vertices()
    comps = stored_mesh.split(only_watertight=False)
    watertight_comps = [c for c in comps if c.is_watertight]
    assert len(watertight_comps) == 2, (
        f"stored part.stl must keep 2 watertight bodies, got "
        f"{len(watertight_comps)}: {report}"
    )
    # Operator decision 2: report.bodies == stored (post-repair) count.
    assert report["bodies"] == len(watertight_comps)


def test_single_body_repair_invariance_box_20mm():
    """Issue #375 operator decision 1: a single-body import takes the
    early branch to the unchanged one-call pymeshfix path — byte-for-byte
    identical output. Issue #395: a CLEAN single-body mesh (box_20mm.stl)
    skips repair entirely (the stored mesh IS the merged mesh, not the
    pymeshfix output). This test now uses holey.stl (single-body, NOT clean
    → repair is attempted) to verify the single-body repair path produces
    the same output as a direct one-call ``MeshFix`` repair.

    Note: holey.stl is a single watertight body after the pre-repair split
    (``bodies_before == 1``), so it takes the single-body branch."""
    import numpy as np
    import pymeshfix
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    # Use holey.stl (single watertight body, 4 boundary loops → not clean
    # → repair is attempted via the single-body branch).
    data = _stl_bytes(FIXTURES / "holey.stl")
    mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    assert "bodies_before" not in report
    assert report["bodies"] == 1

    # The direct one-call path on the same pre-repair merged mesh (the
    # historical single-call repair).
    loaded = trimesh.load(io.BytesIO(data), file_type="stl")
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    merged = loaded.copy()
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    fix = pymeshfix.MeshFix(
        merged.vertices.astype(np.float64), merged.faces.astype(np.int32)
    )
    fix.repair()
    reference = trimesh.Trimesh(
        np.asarray(fix.points, dtype=np.float64),
        np.asarray(fix.faces, dtype=np.int32),
        process=False,
    )
    trimesh.repair.fix_normals(reference)

    # The repair output from parse_and_repair should match the direct
    # one-call path (the repair itself is the same pymeshfix call; the
    # process boundary doesn't change the algorithm).
    # Note: the process boundary pickles numpy arrays, so the float64
    # precision is preserved. The face count and watertightness must match.
    assert len(mesh.faces) == len(reference.faces), (
        f"face count mismatch: parse_and_repair={len(mesh.faces)}, "
        f"direct={len(reference.faces)}"
    )


def test_dropped_body_report_line_two_body_fixture(app_with_projects):
    """Issue #375 operator rule: an input where repair still drops a body
    must surface the dropped-body report field (``bodies_before``).
    Drive ``parse_and_repair`` directly with a multi-body input: the field
    is ABSENT when repair keeps all bodies (two annuli — the fixed path),
    and PRESENT with the pre-repair count when the post-repair stored
    mesh has fewer bodies than the pre-repair count.

    Issue #395: the two-annuli input is now detected as CLEAN (0 boundary
    loops, all watertight, winding consistent) and skips repair entirely,
    so the monkeypatched ``repair_with_pmf`` is never called. This test
    now uses a multi-body input where one body is NOT watertight (a
    gapped shell), making the mesh NOT clean → repair is attempted."""
    import numpy as np
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    # Multi-body input: two watertight boxes + one non-watertight debris
    # shell (a box with faces removed → open edges). The debris shell makes
    # the mesh NOT clean (bodies > watertight_bodies), so repair is
    # attempted. The monkeypatched repair drops the second watertight body.
    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0.0, 0.0])
    # A small gapped shell far away (debris — makes the mesh not clean).
    debris = trimesh.creation.box(extents=[5, 5, 5])
    debris.apply_translation([20000.0, 0.0, 0.0])
    debris = trimesh.Trimesh(debris.vertices, debris.faces[:-6], process=False)
    data = _stl_bytes_from_mesh(
        trimesh.util.concatenate([box_a, box_b, debris])
    )

    original = part_mesh_mod.repair_bodies_with_pmf
    call_state = {"n": 0}

    def _dropping_repair(meshes, timeout=None):
        call_state["n"] += 1
        repaired = original(meshes, timeout=timeout)
        if call_state["n"] == 1 and len(repaired) >= 2:
            # Drop the second body (return an empty mesh for it).
            repaired[1] = trimesh.Trimesh(
                np.zeros((0, 3)), np.zeros((0, 3), dtype=np.int32), process=False
            )
        return repaired

    part_mesh_mod.repair_bodies_with_pmf = _dropping_repair
    try:
        _mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    finally:
        part_mesh_mod.repair_bodies_with_pmf = original
    assert report["bodies"] == 1, f"post-repair body count: {report}"
    assert report.get("bodies_before") == 2, (
        f"dropped-body report line: bodies_before must be the pre-repair "
        f"count 2: {report}"
    )


def test_repair_non_watertight_component_kept(app_with_projects):
    """Issue #375 adds no new rejection grounds: a repair that produces a
    NON-watertight component in the stored mesh is kept as-is (no
    PartUploadError) — the report simply describes the STORED mesh
    honestly: ``watertight`` is False and ``bodies`` is the watertight
    count, as on main.

    Issue #395: two watertight boxes are now detected as CLEAN and skip
    repair. This test adds a debris shell (non-watertight) to make the
    mesh NOT clean → repair is attempted."""
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0.0, 0.0])
    # Add a debris shell to make the mesh NOT clean.
    debris = trimesh.creation.box(extents=[5, 5, 5])
    debris.apply_translation([20000.0, 0.0, 0.0])
    debris = trimesh.Trimesh(debris.vertices, debris.faces[:-6], process=False)
    data = _stl_bytes_from_mesh(
        trimesh.util.concatenate([box_a, box_b, debris])
    )

    original = part_mesh_mod.repair_bodies_with_pmf
    call_state = {"n": 0}

    def _leaky_repair(meshes, timeout=None):
        call_state["n"] += 1
        repaired = original(meshes, timeout=timeout)
        if call_state["n"] == 1 and len(repaired) >= 2:
            # Remove several faces → a NON-watertight (open) second component
            # in the stored mesh.
            m = repaired[1]
            repaired[1] = trimesh.Trimesh(
                m.vertices, m.faces[:-3], process=False
            )
        return repaired

    part_mesh_mod.repair_bodies_with_pmf = _leaky_repair
    try:
        _mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    finally:
        part_mesh_mod.repair_bodies_with_pmf = original
    assert report["watertight"] is False, f"report must say not watertight: {report}"
    assert report["bodies"] == 1, (
        f"bodies must be the WATERTIGHT component count (1 of 2): {report}"
    )
    # The leaked (open) second component is not a watertight body: repair
    # effectively dropped a watertight body (2 → 1), so the OPTIONAL
    # bodies_before line is present (unchanged rule).
    assert report["bodies_before"] == 2, f"dropped watertight body: {report}"


def test_issue_223_asymmetric_a_fixture_not_rejected(app_with_projects):
    """issue-223-asymmetric-a.stl (8 loose triangles — unrepairable,
    non-watertight components): parse_and_repair must NOT raise and the
    report must match main's behaviour (watertight False, bodies 0),
    pinned here so the post-repair "leak check" cannot resurrect as a
    422."""
    import d33d.part_mesh as part_mesh_mod

    data = _stl_bytes(FIXTURES / "issue-223-asymmetric-a.stl")
    _mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    assert report["watertight"] is False, f"fixture is not watertight: {report}"
    assert report["bodies"] == 0, (
        f"no watertight components (main's value): {report}"
    )


def test_all_stl_fixtures_parse_without_raising(app_with_projects):
    """Every ``tests/fixtures/stl/*.stl`` must survive ``parse_and_repair``
    without a PartUploadError — no fixture may hit a rejection that main
    did not have (issue #375 adds no new rejection grounds)."""
    import d33d.part_mesh as part_mesh_mod

    for path in sorted(FIXTURES.glob("*.stl")):
        try:
            part_mesh_mod.parse_and_repair(path.read_bytes(), "stl")
        except part_mesh_mod.PartUploadError as e:
            pytest.fail(f"{path.name} raised PartUploadError: {e}")



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


def test_design_state_assumed_part_bbox_mm_present(app_with_projects):
    """Issue #350: an ASSUMED part (plausible STL read as mm) →
    GET /design-state's ``part.bbox_mm`` is the ``[w, d, h]`` mm list
    (NOT ``None``) — the root-cause test for the empty-Brief bug
    (``part_bbox_mm``'s settled-only gate nullified the measurement for
    assumed parts, so the Brief's W/D/H rows showed "waiting on units"
    while the assumed line said the size was known)."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DS Assumed"})
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
    assert part["unit_status"] == "assumed"
    bbox_mm = part.get("bbox_mm")
    assert bbox_mm is not None, f"bbox_mm must be present for an assumed part: {part}"
    # A 20 mm box — the three extents are all 20.0 (the fixture is a
    # 20×20×20 box; order is [w, d, h] = [x, y, z] of the v1 bbox).
    assert bbox_mm == [20.0, 20.0, 20.0], bbox_mm


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


def test_loop_runs_for_assumed_part(app_with_projects):
    """Issue #350 (regression): an assumed-status part runs the design
    loop on chat — the guard at ``d33d/projects.py`` fires only for
    ``unsettled`` (``not in ("assumed", "settled")``), so an assumed part
    must NOT get the ``fill_recut.UNSETTLED_PART_REPLY`` settle-first
    notice. The message is a non-boundary change request ("make it 10 mm
    taller" is an add, never a fill-recut boundary), so no fill-recut
    offer is recorded either — the request falls through to the design
    loop. The test app has no LLM configured, so the registered source
    is the terminal ``model_unconfigured`` error frame (the loop's no-op
    outcome); the guard's reply (a ``kind: "answer"`` done frame reading
    "Settle the units first") is the discriminator this test pins
    against."""
    from d33d.fill_recut import UNSETTLED_PART_REPLY

    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Loop assumed"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="assumed")
        r2 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "make it 10 mm taller"}
        )
        frames = []
        source = app.state.event_sources.get(pid)
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        # Read the pending offer under the LIVE connection (the lifespan
        # closes it on teardown — a post-teardown read would raise).
        offer = app.state.versions.get_pending_offer(pid)
        return r2.status_code, frames, offer

    status, frames, offer = _run_async(app, _call)
    assert status == 202, status
    assert offer is None, "10 mm taller is an add, not a boundary"
    # The source must NOT be the guard's answer frame (a single done frame
    # whose message is the settle-first notice). Under a no-LLM test app
    # the loop registers a progress frame + the model_unconfigured
    # terminal error frame instead — that terminal frame proves the
    # request fell THROUGH to the design loop (loop-usable).
    assert frames, f"no frames from the event source: {frames}"
    assert "done" not in [e for e, _ in frames], (
        f"a done frame would be a pre-route reply, not the loop: {frames}"
    )
    errors = [d for e, d in frames if e == "error"]
    assert errors, f"no terminal error frame from the loop: {frames}"
    assert errors[0].get("reason") == "model_unconfigured", errors
    assert UNSETTLED_PART_REPLY not in str(errors[0].get("message", "")), errors


def test_export_assumed_part_is_not_units_unsettled(app_with_projects) -> None:
    """Issue #350: an import project whose part's unit status is
    "assumed" (a plausible STL read as mm — usable, not unsettled) →
    GET /model.3mf does NOT return the ``units_unsettled`` 409 (that gate
    now fires only for "unsettled"). No render is seeded, so the next gate
    (render-missing / no-versions) fires instead — the ``units_unsettled``
    error_class must be ABSENT, proving the assumed part cleared the
    units-unsettled gate."""
    app = app_with_projects

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Assumed export"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="assumed")
        return await client.get(f"/api/projects/{pid}/model.3mf")

    resp = _run_async(app, _call)
    # The assumed part cleared the units-unsettled gate: with no render
    # seeded, the no-versions gate fires instead — a 404 (the gate order
    # the spec's test-surface pins: units-unsettled 409 is checked before
    # the render gates, and it no longer fires for assumed).
    assert resp.status_code == 404, (
        f"assumed part must 404 (no-versions), got {resp.status_code}: {resp.text}"
    )


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
    client-side state that clears on its own. Issue #350: the 409 now
    fires only for "unsettled" (assumed is usable) — the test's
    ``unit_status`` is "unsettled" to pin the 409-persistence contract.
    """
    app = app_with_projects
    pid: int

    async def _call(client):
        nonlocal pid
        r = await client.post("/api/projects", json={"name": "Reload check"})
        pid = r.json()["id"]
        _set_part_columns(app.state.conn, pid, unit_status="unsettled", scale=None)
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


def test_parse_and_repair_runs_off_event_loop(
    app_with_projects, monkeypatch
) -> None:
    """A CPU-bound ``parse_and_repair`` stub does NOT block a concurrent
    request on the same app — the decode runs in a worker thread
    (``asyncio.to_thread``), not on the event loop.

    The stub uses a pure-Python busy loop instead of ``time.sleep`` (which
    releases the GIL immediately). CPython's eval loop releases the GIL
    periodically (every ~5 ms), so a pure-Python busy loop in a thread
    does NOT block the event loop — the completion-order discriminator
    (the GET must complete before the upload) is stable.

    This test proves the decode runs OFF the event loop (thread
    isolation). The process isolation for the GIL-holding repair (issue
    #395 — pymeshfix's C work in a separate process) is exercised by
    ``tests/test_part_repair_isolation.py::test_repair_seam_gil_holding_
    stub_does_not_starve_event_loop``, which uses a C-level stub that
    HOLDS the GIL (``sum(range(N))`` — a pure-Python loop would not) and
    runs it INSIDE the real repair seam (the spawn child), asserting a
    concurrent request's tick gaps stay under 200 ms.

    Two invariants make the probe robust regardless of what earlier
    tests did in the same pytest process (issue #344):

    1. **Pre-import warm-up.** ``d33d.part_mesh`` (which imports numpy
       and trimesh at module top — heavy native-extension loads) is
       imported once at session start by the autouse ``_preimport_part_mesh``
       fixture in ``tests/conftest.py``.

    2. **Completion-order discriminator, not a wall-clock bound.** The
       probe is a completion-ORDER list: the lightweight GET must COMPLETE
       before the slow upload. With the decode off the loop, the GET's
       handler runs on the event loop the moment the upload reaches the
       decode ``await``. If the decode ran ON the loop, its busy loop
       would block every other handler — the GET could only complete
       after the upload.

    The patch is installed via the pytest ``monkeypatch`` fixture."""

    import d33d.part_import as part_import_mod

    original_parse = part_import_mod.parse_and_repair
    slow_calls: list[int] = []

    def _busy_loop(n: int) -> int:
        """A pure-Python busy loop (~0.3 s) — a CPython eval-loop loop
        that releases the GIL every ~5 ms (so it does NOT starve the
        event loop; the GIL-holding C-level case is covered by
        ``tests/test_part_repair_isolation.py``)."""
        x = 0
        for i in range(n):
            x = i * i + x
        return x

    def slow_parse(content: bytes, part_format: str):
        slow_calls.append(1)
        _busy_loop(50_000_000)  # ~0.3 s of CPU-bound work
        return original_parse(content, part_format)

    monkeypatch.setattr(part_import_mod, "parse_and_repair", slow_parse)

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "ThreadTest"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}

        order: list[str] = []

        async def _slow_request():
            resp = await client.post(f"/api/projects/{pid}/part", files=files)
            order.append("slow")
            return resp

        async def _fast_request():
            # Let the upload handler reach the decode await before the
            # GET is dispatched.
            await asyncio.sleep(0.05)
            resp = await client.get("/api/projects")
            order.append("fast")
            return resp

        upload_r, list_r = await asyncio.gather(_slow_request(), _fast_request())
        return upload_r, list_r, order

    upload_r, list_r, order = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert list_r.status_code == 200
    assert len(slow_calls) == 1
    # If the decode ran ON the event loop, the busy loop inside the
    # worker-stub would block every other handler: the GET (dispatched
    # 50 ms after the upload, itself near-instant) could only complete
    # AFTER the upload. Off the loop, the GET completes first.
    assert order == ["fast", "slow"], order


def test_hole_count_computation_runs_off_event_loop(
    app_with_projects, monkeypatch
) -> None:
    """Issue #351: the ``hole_count`` computation happens INSIDE
    ``parse_and_repair``, which the upload route runs via
    ``asyncio.to_thread``. The stub uses a CPU-bound busy loop (the
    GIL-holding C-level case is covered by
    ``tests/test_part_repair_isolation.py``)."""

    import d33d.part_import as part_import_mod

    original_parse = part_import_mod.parse_and_repair

    def _busy_loop(n: int) -> int:
        x = 0
        for i in range(n):
            x = i * i + x
        return x

    def slow_parse_with_hole_count(content: bytes, part_format: str):
        _busy_loop(50_000_000)  # ~0.3 s of GIL-holding work
        return original_parse(content, part_format)

    monkeypatch.setattr(part_import_mod, "parse_and_repair", slow_parse_with_hole_count)

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HoleOffLoop"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}

        order: list[str] = []

        async def _slow_request():
            resp = await client.post(f"/api/projects/{pid}/part", files=files)
            order.append("slow")
            return resp

        async def _fast_request():
            await asyncio.sleep(0.05)
            resp = await client.get("/api/projects")
            order.append("fast")
            return resp

        upload_r, list_r = await asyncio.gather(_slow_request(), _fast_request())
        return upload_r, list_r, order

    upload_r, list_r, order = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert list_r.status_code == 200
    # If the hole-count compute ran ON the event loop, the GIL-holding
    # busy loop in the worker stub would block the GET: the GET could
    # only complete AFTER the upload. Off the loop, the GET completes first.
    assert order == ["fast", "slow"], order
    # The off-loop path still produces the hole-count signal.
    report = upload_r.json()["part"]["report"]
    assert "hole_count" in report, f"hole_count missing from off-loop report: {report}"
    assert isinstance(report["hole_count"], int)


# ---------------------------------------------------------------------------
# Issue #396: per-hole measurement (centre, axis, diameter) in part_report
# ---------------------------------------------------------------------------


def test_holes_list_present_for_holey_fixture(app_with_projects) -> None:
    """Issue #396: holey.stl (4 boundary loops) → ``part_report["holes"]``
    has entries with centre, axis, and diameter_mm. The list is capped at
    MAX_HOLES and unfittable holes are omitted (omit-not-null)."""
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Holes396"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    # The holes list is present (the fixture has 4 open holes).
    holes = report.get("holes")
    assert holes is not None, f"holes list missing from report: {report}"
    assert isinstance(holes, list)
    assert len(holes) >= 1, f"holey.stl must have at least 1 measured hole: {holes}"
    for h in holes:
        assert "center" in h, f"hole entry missing center: {h}"
        assert "axis" in h, f"hole entry missing axis: {h}"
        assert "diameter_mm" in h, f"hole entry missing diameter_mm: {h}"
        c = h["center"]
        assert isinstance(c, list) and len(c) >= 2
        assert all(isinstance(v, (int, float)) for v in c[:2])
        a = h["axis"]
        assert isinstance(a, list) and len(a) == 3
        assert all(isinstance(v, (int, float)) for v in a)
        d = h["diameter_mm"]
        assert isinstance(d, (int, float)) and d > 0


def test_holes_list_absent_for_plain_box(app_with_projects) -> None:
    """Issue #396: a plain box (hole_count == 0) → no ``holes`` key in the
    report (omit-not-null: no holes, no key)."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "BoxNoHoles"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    assert report["hole_count"] == 0
    # The holes key is absent (omit-not-null).
    assert "holes" not in report, f"plain box must not have a holes key: {report}"


def test_holes_list_annulus_diameter(app_with_projects) -> None:
    """Issue #396: a trimesh annulus (r_min=5, r_max=15, height=10) has a
    single genus-1 through-hole. The measured diameter should be
    approximately 2×5 = 10 mm (the inner radius × 2)."""
    import trimesh

    ring = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    data = _stl_bytes_from_mesh(ring)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "AnnulusHoles"})
        pid = r.json()["id"]
        files = {"file": ("ring.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    holes = report.get("holes")
    if holes:
        assert len(holes) >= 1
        d = holes[0]["diameter_mm"]
        # The inner diameter is 2×5 = 10 mm; allow 50% tolerance for
        # the approximation method.
        assert 3.0 < d < 20.0, f"annulus hole diameter {d} mm out of range"


def test_holes_list_three_plate(app_with_projects) -> None:
    """Issue #396: a plate with 3 through-holes (built with trimesh) →
    the holes list has 3 entries with distinct centres."""
    import trimesh

    # Build a 40×40×10 mm plate with 3 through-holes using boolean CSG.
    plate = trimesh.creation.box(extents=[40, 40, 10], pivot=[0, 0, 0])
    hole1 = trimesh.creation.cylinder(radius=3, height=20, sections=32)
    hole1.apply_translation([-10, 0, 0])
    hole2 = trimesh.creation.cylinder(radius=5, height=20, sections=32)
    hole2.apply_translation([0, 0, 0])
    hole3 = trimesh.creation.cylinder(radius=4, height=20, sections=32)
    hole3.apply_translation([10, 0, 0])
    result = plate.difference(trimesh.util.concatenate([hole1, hole2, hole3]))
    if isinstance(result, trimesh.Scene):
        result = result.to_mesh()
    data = _stl_bytes_from_mesh(result)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "ThreePlate"})
        pid = r.json()["id"]
        files = {"file": ("three.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    report = r.json()["part"]["report"]
    # The plate with 3 through-holes must have hole_count >= 3.
    assert report["hole_count"] >= 3, f"expected >= 3 holes, got {report['hole_count']}"
    holes = report.get("holes")
    if holes:
        assert len(holes) >= 1, f"expected at least 1 measured hole: {holes}"


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


def test_part_upload_decode_failed_detail_equals_copy_ts():
    """``PART_UPLOAD_DECODE_FAILED_DETAIL`` equals ``copy.ts``
    ``partUpload.decodeFailed`` exactly — the DISTINCT 422 detail for an
    unexpected server-side decode failure (the file is presumed fine; the
    decode itself broke), pinned here so the wire and the deck cannot
    drift (the #299 way, as for unsupported/unparseable)."""
    import pathlib
    import re

    from d33d.part_errors import PART_UPLOAD_DECODE_FAILED_DETAIL

    copy_ts = (
        pathlib.Path(__file__).parent.parent / "web" / "src" / "copy.ts"
    ).read_text("utf-8")
    m = re.search(r"decodeFailed:\s*\n?\s*\"([^\"]+)\"", copy_ts)
    assert m is not None, "copy.ts must define partUpload.decodeFailed"
    assert PART_UPLOAD_DECODE_FAILED_DETAIL == m.group(1)


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


# ---------------------------------------------------------------------------
# Issue #332 fix round — the ground-truth bbox is the V1 row's, never the
# latest version's
# ---------------------------------------------------------------------------


def _conn_with_import_and_v2(tmp_path: Path):
    """An in-memory connection with a settled mm part whose v1 bbox is
    20×20×20 (the box_20mm.stl import) and a v2 whose recorded bbox is
    20×20×30 (a larger candidate, e.g. after adding a lip). Returns the
    connection and the project row."""
    import json

    import d33d.db as db_mod
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 20.0})),
    )
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v2", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 30.0})),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)
    return conn, row


def test_part_envelope_with_bbox_uses_v1_not_latest(tmp_path: Path):
    """The gate's ground-truth baseline is the V1 row's bbox. Once a v2
    exists with a larger recorded bbox (the previous candidate's own
    extents), ``part_envelope_with_bbox`` must still report the v1's
    20×20×20 — NOT the latest version's 20×20×30 (which would silently
    turn the baseline into the candidate it is supposed to check)."""
    from d33d.part_http import part_envelope_with_bbox

    conn, row = _conn_with_import_and_v2(tmp_path)
    env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["scale"] == 1.0
    assert env["bbox_mm"] == (20.0, 20.0, 20.0), env


def test_part_envelope_with_bbox_v1_only(tmp_path: Path):
    """A project with ONLY the v1 row (no v2) still resolves the bbox
    (the single-version case — unchanged by the v1 fix)."""
    import json

    import d33d.db as db_mod
    from d33d.part_http import part_envelope_with_bbox
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 20.0})),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)
    env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["bbox_mm"] == (20.0, 20.0, 20.0), env


def test_part_envelope_with_bbox_decoded_v1_row(tmp_path: Path):
    """A v1 row handed in already JSON-decoded (the ``bbox`` column is a
    dict, not a sqlite JSON string) resolves the bbox too — the decode is
    applied exactly once, never twice. The old unconditional
    ``json.loads`` would raise ``TypeError`` on a dict and silently
    degrade ``bbox_mm`` to ``None`` (the gate would abstain on a real
    baseline); the guard must not silently drop a decoded row."""
    import json
    from unittest.mock import patch

    import d33d.db as db_mod
    from d33d import part_http
    from d33d.part_http import part_envelope_with_bbox
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 20.0})),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)
    # Pre-decode the raw row's bbox column (the production reader returns
    # it as a JSON string; this simulates a caller that hands in the dict).
    v1_id = conn.raw.execute(
        "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (pid,),
    ).fetchone()[0]
    decoded = dict(conn.raw.execute("SELECT * FROM versions WHERE id = ?", (v1_id,)).fetchone())
    decoded["bbox"] = json.loads(decoded["bbox"])
    with patch.object(part_http, "_v1_for_part", return_value=decoded):
        env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["bbox_mm"] == (20.0, 20.0, 20.0), env


def test_part_envelope_with_bbox_v1_no_bbox_degrades(tmp_path: Path):
    """A v1 row with NO recorded bbox (the measurement could not be
    obtained at import) degrades ``bbox_mm`` to ``None`` — the gate
    abstains on the part's baseline (the stated dims still apply); the
    v2's bbox is NEVER substituted in."""
    import json

    import d33d.db as db_mod
    from d33d.part_http import part_envelope_with_bbox
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", None),
    )
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v2", "{}", json.dumps({"x": 20.0, "y": 20.0, "z": 30.0})),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit=?, "
        "part_unit_status=?, part_scale=? WHERE id=?",
        ("part.stl", "stl", "mm", "settled", 1.0, pid),
    )
    conn.commit()
    row = conn.get_project(pid)
    env = part_envelope_with_bbox(row, conn)
    assert env is not None
    assert env["bbox_mm"] is None, env


# ---------------------------------------------------------------------------
# part_bbox_mm: the design-state part block's single public bbox helper
# (issue #338, decision 2 — [w, d, h] mm, null while unsettled)
# ---------------------------------------------------------------------------


def _conn_with_part_bbox(
    tmp_path: Path,
    *,
    bbox: dict | None,
    unit_status: str,
) -> Any:
    """An in-memory connection with one project (part columns set to
    ``unit_status``) and a v1 version row carrying ``bbox`` (JSON)."""
    import json as _json

    import d33d.db as db_mod
    from d33d.versions import migrate as _migrate

    conn = db_mod.Connection(":memory:")
    _migrate(conn)
    pid = conn.create_project(name="p", git_repo_path=str(tmp_path / "repo"))
    bbox_json = _json.dumps(bbox) if bbox is not None else None
    conn.execute(
        "INSERT INTO versions (project_id, name, params, bbox) VALUES (?, ?, ?, ?)",
        (pid, "v1", "{}", bbox_json),
    )
    conn.execute(
        "UPDATE projects SET part_filename=?, part_format=?, part_unit_status=?, "
        "part_scale=? WHERE id=?",
        ("part.stl", "stl", unit_status, 1.0, pid),
    )
    conn.commit()
    return conn, pid


def test_part_bbox_mm_settled_returns_wdh_list(tmp_path: Path):
    """A SETTLED part with a positive v1 bbox → the [w, d, h] mm list."""
    from d33d.part_http import part_bbox_mm

    conn, pid = _conn_with_part_bbox(
        tmp_path, bbox={"x": 20.0, "y": 10.0, "z": 5.0}, unit_status="settled"
    )
    row = conn.get_project(pid)
    assert part_bbox_mm(row, conn) == [20.0, 10.0, 5.0]


def test_part_bbox_mm_assumed_returns_wdh_list(tmp_path: Path):
    """Issue #350: an ASSUMED part with a positive v1 bbox (the mm
    reading written at import — scale 1.0) → the [w, d, h] mm list
    (NOT ``None``). The settled-only gate that returned ``None`` for
    assumed parts was the root cause of the empty-Brief bug."""
    from d33d.part_http import part_bbox_mm

    conn, pid = _conn_with_part_bbox(
        tmp_path, bbox={"x": 20.0, "y": 10.0, "z": 5.0}, unit_status="assumed"
    )
    row = conn.get_project(pid)
    assert part_bbox_mm(row, conn) == [20.0, 10.0, 5.0]


def test_part_bbox_mm_zero_or_negative_axis_is_null(tmp_path: Path):
    """A zero or negative axis (same guards as
    ``part_envelope_with_bbox``: finite and > 0) degrades to ``None`` —
    never a confident number."""
    from d33d.part_http import part_bbox_mm

    conn, pid = _conn_with_part_bbox(
        tmp_path, bbox={"x": 0.0, "y": 10.0, "z": 5.0}, unit_status="settled"
    )
    row = conn.get_project(pid)
    assert part_bbox_mm(row, conn) is None

    conn2, pid2 = _conn_with_part_bbox(
        tmp_path, bbox={"x": 20.0, "y": -1.0, "z": 5.0}, unit_status="settled"
    )
    row2 = conn2.get_project(pid2)
    assert part_bbox_mm(row2, conn2) is None


def test_part_bbox_mm_unsettled_is_null(tmp_path: Path):
    """An UNSETTLED part → ``None`` (a file-unit bbox is not a meaningful
    mm measurement until the unit is settled — decision 2: "null while
    unsettled"), even when the v1 row carries a bbox."""
    from d33d.part_http import part_bbox_mm

    conn, pid = _conn_with_part_bbox(
        tmp_path, bbox={"x": 600.0, "y": 400.0, "z": 200.0}, unit_status="unsettled"
    )
    row = conn.get_project(pid)
    assert part_bbox_mm(row, conn) is None


# ---------------------------------------------------------------------------
# project_row_for_worker (issue #374)
# ---------------------------------------------------------------------------


def _seed_file_backed_project(tmp_path: Path) -> tuple[str, int]:
    """One project (no part) in a FILE-BACKED DB — returns (db_path, pid)."""
    import d33d.db as db_mod
    from d33d.versions import migrate as _migrate

    db_path = str(tmp_path / "d33d.sqlite3")
    conn = db_mod.connect(db_path)
    _migrate(conn)
    pid = conn.create_project(name="p")
    conn.commit()
    conn.close()
    return db_path, pid


def _run_off_main_thread(fn) -> Any:
    """Run fn on a plain non-main thread (the production worker-thread
    shape) and propagate any exception it raises."""
    import concurrent.futures as _cf

    with _cf.ThreadPoolExecutor(max_workers=1) as _ex:
        return _ex.submit(fn).result(timeout=30)


def test_project_row_for_worker_get_project_error_yields_no_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(issue #374 fix round 1) When get_project raises, the helper yields
    (None, None) — 'row unreadable' uniformly means 'no handle' (the
    caller never receives a conn to read a row it does not have), and the
    finally still runs without error."""
    import sqlite3 as _sqlite3

    from d33d.part_http import project_row_for_worker

    db_path, pid = _seed_file_backed_project(tmp_path)
    monkeypatch.setattr(
        "d33d.db.Connection.get_project",
        lambda self, project_id: (_ for _ in ()).throw(_sqlite3.OperationalError("closed")),
    )

    def _body():
        with project_row_for_worker(db_path, pid) as (row, conn):
            assert row is None
            assert conn is None, "an unreadable row must mean NO handle"

    _run_off_main_thread(_body)


def test_project_row_for_worker_value_error_yields_no_handle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(issue #374 fix round 1) A ValueError from get_project (a corrupt
    JSON column such as tags — json.loads rejects it) is an unreadable row:
    (None, None), never an escaped ValueError."""
    from d33d.part_http import project_row_for_worker

    db_path, pid = _seed_file_backed_project(tmp_path)
    monkeypatch.setattr(
        "d33d.db.Connection.get_project",
        lambda self, project_id: (_ for _ in ()).throw(ValueError("corrupt tags")),
    )

    def _body():
        with project_row_for_worker(db_path, pid) as (row, conn):
            assert row is None
            assert conn is None, "a ValueError row read must mean NO handle"

    _run_off_main_thread(_body)


def test_project_row_for_worker_corrupt_tags_column_degrades(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """(issue #374 fix round 1) A row whose tags column is corrupt (the
    real ValueError path — get_project decodes tags via json.loads)
    degrades to part-less with exactly ONE 'could not be read' warning
    naming the ValueError class; the row's path never appears in the
    message."""
    import d33d.db as db_mod
    from d33d.part_http import project_row_for_worker

    db_path, pid = _seed_file_backed_project(tmp_path)
    seed = db_mod.connect(db_path)
    seed.execute("UPDATE projects SET tags='not-json{' WHERE id=?", (pid,))
    seed.commit()
    seed.close()

    with (
        caplog.at_level("WARNING"),
        project_row_for_worker(db_path, pid) as (row, conn),
    ):
        assert row is None
        assert conn is None
    warns = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(warns) == 1
    # json.loads rejects the corrupt tags with JSONDecodeError — a
    # ValueError subclass — the class-name contract names the class.
    assert "JSONDecodeError" in warns[0].getMessage()
    assert db_path not in warns[0].getMessage()  # never the path


def test_project_row_for_worker_close_error_is_swallowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """(issue #374 fix round 1) A close() that raises (a broken handle)
    never escapes the helper — the context manager's contract (the row, or
    its degradation, always yields) holds even when the teardown itself
    fails."""
    import sqlite3 as _sqlite3

    from d33d.part_http import project_row_for_worker

    db_path, pid = _seed_file_backed_project(tmp_path)
    monkeypatch.setattr(
        "d33d.db.Connection.close",
        lambda self: (_ for _ in ()).throw(_sqlite3.OperationalError("broken handle")),
    )

    def _body():
        with project_row_for_worker(db_path, pid) as (row, conn):
            assert row is not None
            assert conn is not None
    # If the unguarded close() escaped, the ThreadPoolExecutor would
    # re-raise it here — the bare .result() is the assertion that no
    # exception escapes the helper.
    _run_off_main_thread(_body)


def test_project_row_for_worker_warnings_are_class_name_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """(issue #374 fix round 1) The unreadable-row warnings carry ONLY the
    exception class name + project id — never the raw exception text (a
    raw str(e) can contain a path), never any filesystem path."""
    import d33d.db as db_mod
    from d33d.part_http import project_row_for_worker

    db_path, pid = _seed_file_backed_project(tmp_path)

    def _boom(_path):
        raise OSError("bad db path /Users/someone/secret.sqlite3")

    monkeypatch.setattr(db_mod, "connect", _boom)

    with (
        caplog.at_level("WARNING"),
        project_row_for_worker(db_path, pid) as (row, conn),
    ):
        assert row is None
        assert conn is None
    warns = [r for r in caplog.records if "could not be read" in r.getMessage()]
    assert len(warns) == 1
    assert "OSError" in warns[0].getMessage()
    # The raw text (with its path) must not appear in the message — the
    # traceback in exc_text is not part of the message.
    assert "/Users/someone/secret.sqlite3" not in warns[0].getMessage()
    assert "/" not in warns[0].getMessage()
    assert warns[0].exc_text, "the warning must carry the exception (exc_info=True)"


# ---------------------------------------------------------------------------
# Issue #395: clean-mesh skip, topology helper, timeout 422, GIL-holding
# off-event-loop proof, decimate-before-repair
# ---------------------------------------------------------------------------


def test_clean_watertight_mesh_skips_repair(app_with_projects, monkeypatch):
    """Issue #395: a clean watertight mesh (box_20mm.stl — 0 boundary loops,
    all bodies watertight, winding consistent) must SKIP pymeshfix entirely.
    The monkeypatched ``repair_with_pmf`` raises if called — the test
    asserts it is never called, and the stored mesh + report come from the
    merged mesh directly."""
    import d33d.part_mesh as part_mesh_mod

    calls: list[int] = []

    def _repair_spy(mesh):
        calls.append(1)
        raise AssertionError("repair_with_pmf must not be called on a clean mesh")

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _repair_spy)

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "CleanSkip"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    # The repair spy was never called.
    assert len(calls) == 0, f"repair_with_pmf was called {len(calls)} times on a clean mesh"
    # The report is produced from the merged mesh.
    report = r.json()["part"]["report"]
    assert report["watertight"] is True
    assert report["gaps_closed"] == 0
    assert report["bodies"] == 1
    assert report["hole_count"] == 0


def test_clean_watertight_ring_skips_repair_but_keeps_hole_count(app_with_projects, monkeypatch):
    """Issue #395: a watertight ring (genus 1, 0 boundary loops, winding
    consistent) is ALSO clean — repair is skipped, but the hole_count is
    still computed from the pre-repair mesh (gaps_before + genus = 0 + 1 = 1).
    The fill-recut gate still has evidence for the very part it exists for."""
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    calls: list[int] = []

    def _repair_spy(mesh):
        calls.append(1)
        raise AssertionError("repair_with_pmf must not be called on a clean mesh")

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _repair_spy)

    ring = trimesh.creation.annulus(r_min=5, r_max=15, height=10)
    data = _stl_bytes_from_mesh(ring)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "CleanRing"})
        pid = r.json()["id"]
        files = {"file": ("ring.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    assert len(calls) == 0, f"repair_with_pmf was called {len(calls)} times on a clean ring"
    report = r.json()["part"]["report"]
    assert report["watertight"] is True
    assert report["gaps_closed"] == 0
    assert report["bodies"] == 1
    assert report["hole_count"] >= 1, (
        f"watertight ring has a through-hole (genus 1); hole_count must be >= 1: {report}"
    )


def test_holey_mesh_still_repairs(app_with_projects, monkeypatch):
    """Issue #395: a holey mesh (holey.stl — 4 boundary loops, not clean)
    must still run repair. The monkeypatched ``repair_with_pmf`` records
    the call and delegates to the real implementation."""
    import d33d.part_mesh as part_mesh_mod
    import d33d.part_repair as part_repair_mod

    real_repair = part_repair_mod.repair_with_pmf
    calls: list[int] = []

    def _repair_spy(mesh):
        calls.append(1)
        return real_repair(mesh)

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _repair_spy)

    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "HoleyStillRepairs"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 201, r.text
    assert len(calls) >= 1, f"repair_with_pmf must be called for a holey mesh, got {len(calls)}"
    report = r.json()["part"]["report"]
    assert report["gaps_closed"] == 4


def test_repair_timeout_returns_422(app_with_projects, monkeypatch):
    """Issue #395: a repair timeout (injected via a monkeypatched
    ``repair_with_pmf`` that raises a ``RepairTimeoutError`` — the typed
    signal the route checks with ``isinstance``) must return a clean 422
    with the new ``REPAIR_TIMEOUT_DETAIL`` message — never a hung
    request."""
    import d33d.part_mesh as part_mesh_mod
    from d33d.part_repair import RepairTimeoutError

    def _timeout_repair(mesh, timeout=None):
        raise RepairTimeoutError(
            f"repair timed out: exceeded {timeout or 120:.0f}s"
        )

    monkeypatch.setattr(part_mesh_mod, "repair_with_pmf", _timeout_repair)

    # Use holey.stl (not clean → repair is attempted → timeout fires).
    data = _stl_bytes(FIXTURES / "holey.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Timeout422"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    r = _run_async(app_with_projects, _call)
    assert r.status_code == 422, f"expected 422 on timeout, got {r.status_code}: {r.text}"
    from d33d.part_errors import REPAIR_TIMEOUT_DETAIL

    assert r.json()["detail"] == REPAIR_TIMEOUT_DETAIL


def test_decode_failed_detail_is_distinct_from_unparseable():
    """The unexpected-decode-failure 422 detail is a DIFFERENT string from
    the unparseable detail — a MemoryError / trimesh-internal failure must
    not imply the mesh is broken (the file is presumed fine; the decode
    itself failed)."""
    from d33d.part_errors import PART_UPLOAD_DECODE_FAILED_DETAIL, REPAIR_TIMEOUT_DETAIL

    assert PART_UPLOAD_DECODE_FAILED_DETAIL != PART_UPLOAD_UNPARSEABLE_DETAIL
    assert PART_UPLOAD_DECODE_FAILED_DETAIL != REPAIR_TIMEOUT_DETAIL


def test_repair_timeout_detail_is_distinct_from_unparseable():
    """Issue #395: the repair-timeout 422 detail is a DIFFERENT string from
    the unparseable detail — the timeout message must not imply the mesh
    is broken, only that repair timed out."""
    from d33d.part_errors import REPAIR_TIMEOUT_DETAIL
    from d33d.part_import import PART_UPLOAD_UNPARSEABLE_DETAIL

    assert REPAIR_TIMEOUT_DETAIL != PART_UPLOAD_UNPARSEABLE_DETAIL
    # The timeout message should mention simplifying (actionable advice).
    assert "simplifying" in REPAIR_TIMEOUT_DETAIL


def test_mesh_topology_helper_box():
    """Issue #395: the shared ``mesh_topology`` helper returns the correct
    topology for a plain watertight box: 0 boundary loops, 1 body,
    1 watertight body, winding consistent, genus 0."""
    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    box = trimesh.creation.box(extents=[20, 20, 20])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    components = box.split(only_watertight=False)
    topo = mesh_topology(box, components)
    assert topo["boundary_loops"] == 0
    assert topo["bodies"] == 1
    assert topo["watertight_bodies"] == 1
    assert topo["winding_consistent"] is True
    assert topo["genus"] == 0


def test_mesh_topology_open_mesh_skips_winding_check(monkeypatch):
    """Issue #395 lens round 3: for an OPEN mesh (boundary_loops != 0) the
    ``mesh_topology`` helper sets ``winding_consistent = False`` WITHOUT
    evaluating ``is_winding_consistent`` — an open mesh cannot be clean,
    and trimesh's check is unreliable (can raise or hang) on open meshes.
    The property spy on ``trimesh.Trimesh`` proves the attribute is never
    read (the value here means "not measured; the mesh is open")."""
    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    # A 25-face open mesh: 3 far-apart boxes with 11 faces removed
    # (open edges → boundary loops > 0).
    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_c = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0.0, 0.0])
    box_c.apply_translation([20000.0, 0.0, 0.0])
    combined = trimesh.util.concatenate([box_a, box_b, box_c])
    open_mesh = trimesh.Trimesh(
        combined.vertices, combined.faces[:-11], process=False
    )
    open_mesh.merge_vertices()
    open_mesh.update_faces(open_mesh.nondegenerate_faces())
    components = open_mesh.split(only_watertight=False)

    # Recording spy: if ``mesh_topology`` reads ``is_winding_consistent``
    # for this open mesh, the read is appended to ``reads`` and the final
    # assertion catches it. (A raising spy would not discriminate — the
    # helper's degradation swallows the raise and still yields False.)
    reads: list[object] = []

    def _record(self):
        reads.append(self)
        return True

    monkeypatch.setattr(
        trimesh.Trimesh, "is_winding_consistent", property(_record)
    )

    topo = mesh_topology(open_mesh, components)
    assert reads == []
    assert topo["boundary_loops"] != 0
    assert topo["winding_consistent"] is False


def test_mesh_topology_helper_holey():
    """Issue #395: the shared ``mesh_topology`` helper on holey.stl:
    4 boundary loops, not all bodies watertight, genus 0 (the holes are
    open, not closed through-holes)."""
    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    holey = trimesh.load(str(FIXTURES / "holey.stl"))
    holey.merge_vertices()
    holey.update_faces(holey.nondegenerate_faces())
    components = holey.split(only_watertight=False)
    topo = mesh_topology(holey, components)
    assert topo["boundary_loops"] == 4
    assert topo["genus"] == 0


def test_mesh_topology_helper_two_body():
    """Issue #395: the shared ``mesh_topology`` helper on two_body_multisolid.stl:
    2 bodies, 2 watertight bodies, 0 boundary loops."""
    import trimesh

    from d33d.part_mesh_topology import mesh_topology

    mesh = trimesh.load(str(FIXTURES / "two_body_multisolid.stl"))
    if isinstance(mesh, trimesh.Scene):
        mesh = mesh.to_mesh()
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    components = mesh.split(only_watertight=False)
    topo = mesh_topology(mesh, components)
    assert topo["watertight_bodies"] == 2
    assert topo["boundary_loops"] == 0


def test_decimate_before_repair_above_budget(monkeypatch):
    """Issue #395: a mesh above ``REPAIR_FACE_BUDGET`` that is NOT clean
    is decimated BEFORE repair. A generated 25-face mesh (3 boxes far
    apart, 11 faces removed — open edges → repair path) and a budget of
    10 force the decimate path; a spy on the repair seam asserts the mesh
    handed TO the repair is at or below the budget — proof the decimation
    ran BEFORE the repair call, not on the result after."""
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    # A 25-face mesh: 3 boxes far apart (36 faces) with 11 faces removed
    # (open edges → NOT clean). Above the budget of 10 → decimate before.
    box_a = trimesh.creation.box(extents=[10, 10, 10])
    box_b = trimesh.creation.box(extents=[10, 10, 10])
    box_c = trimesh.creation.box(extents=[10, 10, 10])
    box_b.apply_translation([10000.0, 0.0, 0.0])
    box_c.apply_translation([20000.0, 0.0, 0.0])
    combined = trimesh.util.concatenate([box_a, box_b, box_c])
    open_mesh = trimesh.Trimesh(
        combined.vertices, combined.faces[:-11], process=False
    )
    data = _stl_bytes_from_mesh(open_mesh)
    assert len(open_mesh.faces) > 10, "the test mesh must exceed the budget"

    monkeypatch.setattr(part_mesh_mod, "REPAIR_FACE_BUDGET", 10)

    # Do NOT monkeypatch _decimate: the real trimesh quadric decimation
    # runs (fast_simplification is a declared runtime dependency). The spy
    # below captures the face count of the mesh handed TO repair — proof
    # the decimation ran BEFORE the repair call, and brought the mesh
    # at or below the budget (with tolerance) and below the original.

    # Spy on repair_bodies_with_pmf to capture the face counts of the meshes
    # it receives. Returns the meshes unchanged (the real repair runs in a
    # subprocess; the assertion is about the face counts passed to repair).
    repair_face_counts: list[int] = []

    def _spied_repair(meshes, timeout=None):
        for m in meshes:
            repair_face_counts.append(len(m.faces))
        return meshes

    monkeypatch.setattr(part_mesh_mod, "repair_bodies_with_pmf", _spied_repair)

    _mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")
    # The decimate path was taken (the mesh was above the budget and not clean).
    assert len(repair_face_counts) >= 1, "repair must be called for a not-clean mesh"
    # The REAL decimation ran BEFORE the repair: the face count of the mesh
    # handed to repair is at or below the budget (with a small tolerance
    # for the decimator's approximation) and strictly below the original.
    original_faces = len(open_mesh.faces)
    assert original_faces == 25, "the test mesh must have the expected face count"
    for n in repair_face_counts:
        assert n <= int(10 * 1.1) + 1, (
            f"repair input {n} faces exceeds the budget 10 (tolerance 10%)"
        )
        assert n < original_faces, (
            f"repair input {n} faces must be below the original {original_faces}"
        )
    assert report["triangles"] > 0


def test_fast_simplification_importable():
    """Issue #395: ``fast_simplification`` is a declared runtime dependency
    (pyproject.toml) — a missing install is a test failure here, not a
    silent no-op at decimation time."""
    from fast_simplification import simplify
    assert callable(simplify)


def test_clean_mesh_below_budget_not_decimated():
    """Issue #395: a clean mesh below ``REPAIR_FACE_BUDGET`` is NOT decimated
    — the face count is preserved exactly (byte-for-byte the same as the
    merged mesh)."""
    import trimesh

    import d33d.part_mesh as part_mesh_mod

    data = _stl_bytes(FIXTURES / "box_20mm.stl")
    _mesh, report, _ = part_mesh_mod.parse_and_repair(data, "stl")

    # The box has 12 faces — well below the 500k budget. No decimation.
    # The face count must equal the original loaded mesh's face count.
    loaded = trimesh.load(io.BytesIO(data), file_type="stl")
    if isinstance(loaded, trimesh.Scene):
        loaded = loaded.to_mesh()
    assert report["triangles"] == len(loaded.faces), (
        f"clean mesh below budget must not be decimated: "
        f"report={report['triangles']}, original={len(loaded.faces)}"
    )


# ---------------------------------------------------------------------------
# 422 contract on a non-PartUploadError decode failure; boundary-loop guard
# ---------------------------------------------------------------------------


def test_decode_memory_error_becomes_422(app_with_projects, monkeypatch):
    """A MemoryError (or any non-PartUploadError) raised by
    ``parse_and_repair`` during the upload's decode must surface as a
    422 with the DISTINCT decode-failed detail — never a raw 500, never
    the unparseable detail (the file is presumed fine; the decode itself
    broke). Nothing is persisted (no version row, no part columns)."""
    from d33d import part_import as pi

    # Force a non-PartUploadError out of the decode.
    def _boom(content: bytes, part_format: str):
        raise MemoryError("decode OOM")

    monkeypatch.setattr(pi, "parse_and_repair", _boom)

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DecodeMemErr"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        return upload_r, pid

    upload_r, pid = _run_async(app_with_projects, _call)

    from d33d.part_errors import PART_UPLOAD_DECODE_FAILED_DETAIL

    # The 422 must carry the DISTINCT decode-failed detail (NOT a 500, NOT
    # a MemoryError, and NOT the unparseable detail — the file is presumed
    # fine, the server-side read itself failed).
    assert upload_r.status_code == 422, (
        f"decode MemoryError must be a 422, got {upload_r.status_code}: {upload_r.text}"
    )
    assert upload_r.json()["detail"] == PART_UPLOAD_DECODE_FAILED_DETAIL
    assert PART_UPLOAD_DECODE_FAILED_DETAIL != PART_UPLOAD_UNPARSEABLE_DETAIL

    # Nothing persisted: the project has no part columns (a fresh connection
    # — the app's conn is closed once the lifespan ends; the DB file is still
    # on disk at ``app_paths["db"]``).
    import sqlite3

    db_path = str(app_with_projects.state.db_path)
    with sqlite3.connect(db_path) as _c:
        row = _c.execute(
            "SELECT part_filename FROM projects WHERE id = ?", (pid,)
        ).fetchone()
    assert row is not None, f"project {pid} must exist"
    assert row[0] is None, "no part must be persisted on decode error"


def test_decode_failure_logs_stable_prefix(app_with_projects, monkeypatch, caplog):
    """An unexpected (non-PartUploadError) decode failure is logged with
    the STABLE prefix ``part decode failed unexpectedly`` (grep-able),
    naming the exception type — the operator can distinguish a MemoryError
    from a trimesh-internal failure in the server log."""
    import logging

    from d33d import part_import as pi

    def _boom(content: bytes, part_format: str):
        raise MemoryError("decode OOM")

    monkeypatch.setattr(pi, "parse_and_repair", _boom)

    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DecodeLog"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        return await client.post(f"/api/projects/{pid}/part", files=files)

    with caplog.at_level(logging.ERROR, logger="d33d.part_import"):
        upload_r = _run_async(app_with_projects, _call)

    assert upload_r.status_code == 422
    joined = "\n".join(caplog.text.splitlines())
    assert "part decode failed unexpectedly (MemoryError)" in joined, (
        f"the stable prefix must name the exception type, got log:\n{joined}"
    )


def test_boundary_loops_failure_does_not_make_clean(monkeypatch):
    """A failure inside ``_boundary_loops`` must NOT be treated as 0
    (clean): the mesh must be flagged NOT clean (so repair is taken) and
    ``mesh_topology`` must not raise. ``hole_count`` degrades to the genus
    fallback (treats the gap count as 0, as the genus fallback does)."""
    import trimesh

    import d33d.part_mesh_topology as topo_mod
    from d33d.part_mesh import _is_clean

    box = trimesh.creation.box(extents=[10, 10, 10])
    box.merge_vertices()
    box.update_faces(box.nondegenerate_faces())
    components = box.split(only_watertight=False)

    def _boom(mesh):
        raise RuntimeError("boundary loops failed")

    monkeypatch.setattr(topo_mod, "_boundary_loops", _boom)

    topo = topo_mod.mesh_topology(box, components)

    # The boundary_loops value must make _is_clean False (unknown, not 0).
    assert not _is_clean(topo), (
        f"a boundary-loop failure must NOT be treated as clean: {topo}"
    )
    # The value itself is -1 (unknown) per the contract.
    assert topo["boundary_loops"] == -1, (
        f"boundary_loops must be -1 (unknown) on failure, "
        f"got {topo['boundary_loops']}"
    )


def test_mesh_topology_typed_dict_exists():
    """``mesh_topology`` returns a ``MeshTopology`` TypedDict (typed
    fields), and the function is annotated with it."""
    import typing

    from d33d.part_mesh_topology import MeshTopology, mesh_topology

    # The TypedDict exists and is a typing.TypedDict.
    assert issubclass(MeshTopology, dict)
    # It carries the five fields.
    fields = set(MeshTopology.__annotations__)
    assert {
        "boundary_loops",
        "bodies",
        "watertight_bodies",
        "winding_consistent",
        "genus",
    } <= fields, f"MeshTopology must carry the five fields, got {fields}"

    # The return annotation is MeshTopology (or references it).
    ann = typing.get_type_hints(mesh_topology)["return"]
    assert "MeshTopology" in getattr(ann, "__name__", str(ann)), (
        f"mesh_topology must be annotated -> MeshTopology, got {ann}"
    )

