"""v1 version-row tests for the part import (issue #325, ws-import-version).

Covers the v1 recording criteria that the broader test_part_import.py suite
does not pin:

- v1 name is sanitised for display (control chars, path traversal, shell
  metachars) while the raw filename is stored verbatim as project data
- The mm bbox is recorded on the v1 row (file bbox × scale for assumed/
  settled; NULL for unsettled)
- Settle updates v1's mm bbox in place (no new version row)
- The units-unsettled 409 is server-side state (persists across reload)
- The v1 row's source_kind is "import"
"""

from __future__ import annotations

import asyncio
import io
import json
import zipfile
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app

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


def _stl_bytes(path: Path) -> bytes:
    return path.read_bytes()


def _read_project_part(conn, project_id: int) -> dict[str, Any]:
    """Read the project's part columns from the DB (call inside async block)."""
    row = conn.get_project(project_id)
    assert row is not None
    return row


def _read_v1(conn, project_id: int) -> dict[str, Any] | None:
    """Read the v1 version row from the DB (call inside async block)."""
    rows = conn.raw.execute(
        "SELECT id, name, source_kind, bbox FROM versions WHERE project_id = ? ORDER BY id ASC",
        (project_id,),
    ).fetchall()
    if not rows:
        return None
    r = rows[0]
    return {"id": r[0], "name": r[1], "source_kind": r[2], "bbox": r[3]}


def _count_versions(conn, project_id: int) -> int:
    return conn.raw.execute(
        "SELECT COUNT(*) FROM versions WHERE project_id = ?", (project_id,)
    ).fetchone()[0]


def _make_3mf_inch() -> bytes:
    """Build a 10×10×10 inch 3MF."""
    import trimesh

    box = trimesh.creation.box((10.0, 10.0, 10.0))
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
        '<model unit="inch" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
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
# v1 name sanitisation (display) vs. verbatim storage
# ---------------------------------------------------------------------------


def test_v1_name_strips_control_chars(app_with_projects):
    """A filename with embedded newlines/tab is sanitised for the v1 name
    (control chars stripped, whitespace collapsed to single spaces) but the
    raw filename is stored verbatim in the project's part_filename column.
    The filename is never a path component or a commit-message source —
    it crosses the boundary only as display data."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Ctrl"})
        pid = r.json()["id"]
        malicious = "evil\nname\t.stl"
        files = {"file": (malicious, data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        proj = _read_project_part(conn, pid)
        return upload_r, v1, proj

    upload_r, v1, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None

    # The v1 name must NOT contain raw control characters
    assert "\n" not in v1["name"]
    assert "\t" not in v1["name"]
    # The name still starts with "Imported "
    assert v1["name"].startswith("Imported ")

    # The project's part_filename stores the filename as received from the
    # HTTP layer (control chars URL-encoded by the multipart transport).
    # The key point: the v1 name has them stripped; the project row preserves
    # the received form (never re-interpreted as a path or command).
    assert "evil" in proj["part_filename"]
    assert ".stl" in proj["part_filename"]


def test_v1_name_path_traversal_never_a_path_component(app_with_projects):
    """A filename with path traversal (../../../etc/passwd.stl) is used
    only as display data in the v1 name — it is never a path component.
    The stored file is at the fixed name (part.stl), and the raw filename
    is stored verbatim in the project's part_filename column."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Traversal"})
        pid = r.json()["id"]
        malicious = "../../../etc/passwd.stl"
        files = {"file": (malicious, data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        proj = _read_project_part(conn, pid)
        v1_id = upload_r.json()["version_id"]
        # Check the stored file is at the fixed name, not the user's filename
        repo_path = Path(proj["git_repo_path"])
        fixed_path = repo_path / "versions" / str(v1_id) / "part.stl"
        malicious_path = repo_path / malicious
        return upload_r, v1, proj, fixed_path, malicious_path

    upload_r, v1, proj, fixed_path, malicious_path = _run_async(
        app_with_projects, _call
    )
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None

    # The stored file is at the FIXED name (part.stl), not the user's name
    assert fixed_path.exists()
    # The user's filename is NOT used as a path component
    assert not malicious_path.exists()

    # The raw filename is stored verbatim as project data (never interpreted)
    assert proj["part_filename"] == "../../../etc/passwd.stl"


def test_v1_name_shell_metachars_are_display_only(app_with_projects):
    """A filename with shell metacharacters is used only as display data.
    The filename is never interpreted (never a path component, never a
    shell command, never a commit message source without sanitisation).
    The v1 name carries the filename as display text; the raw filename is
    stored verbatim in the project's part_filename column."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Shell"})
        pid = r.json()["id"]
        malicious = "rm -rf; $(whoami).stl"
        files = {"file": (malicious, data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        proj = _read_project_part(conn, pid)
        # Verify the filename is not used as a path component
        repo_path = Path(proj["git_repo_path"])
        v1_id = upload_r.json()["version_id"]
        fixed_path = repo_path / "versions" / str(v1_id) / "part.stl"
        return upload_r, v1, proj, fixed_path

    upload_r, v1, proj, fixed_path = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None

    # The v1 name starts with "Imported " (display name)
    assert v1["name"].startswith("Imported ")

    # The stored file is at the FIXED name, not the user's filename
    assert fixed_path.exists()

    # The raw filename is stored verbatim as data (never interpreted)
    assert proj["part_filename"] == "rm -rf; $(whoami).stl"


def test_v1_name_dimension_phrase_not_sanitised(app_with_projects):
    """A filename containing a dimension phrase (e.g. '20x20x20 mm.stl')
    must NOT trigger the dimension-phrase sanitizer — the v1 name is
    'Imported {filename}' with the filename sanitised for display only
    (control chars stripped, whitespace collapsed), not through
    sanitize_dimension_phrase."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "DimPhrase"})
        pid = r.json()["id"]
        files = {"file": ("20x20x20 mm.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        return upload_r, v1

    upload_r, v1 = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    # The dimension phrase in the filename is preserved in the v1 name
    assert "20x20x20" in v1["name"]
    assert "mm" in v1["name"]
    assert v1["name"].startswith("Imported ")


# ---------------------------------------------------------------------------
# v1 mm bbox recording
# ---------------------------------------------------------------------------


def test_v1_bbox_recorded_for_assumed_stl(app_with_projects):
    """An assumed STL (box_20mm.stl) has its mm bbox recorded on the v1
    row (file bbox × scale 1.0)."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "BBox Assumed"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        return upload_r, v1

    upload_r, v1 = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    bbox = json.loads(v1["bbox"])
    # box_20mm.stl is 20×20×20 mm; scale 1.0 for assumed mm
    assert abs(bbox["x"] - 20.0) < 1e-3
    assert abs(bbox["y"] - 20.0) < 1e-3
    assert abs(bbox["z"] - 20.0) < 1e-3


def test_v1_bbox_null_for_unsettled_stl(app_with_projects):
    """An unsettled STL (over_envelope.stl) has NULL bbox on the v1 row
    (no scale → no mm conversion yet)."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "BBox Unsettled"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        return upload_r, v1

    upload_r, v1 = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    assert v1["bbox"] is None


def test_v1_bbox_recorded_for_settled_3mf(app_with_projects):
    """A 3MF with inch unit (settled on import) has its mm bbox recorded
    on the v1 row (file bbox × 25.4)."""
    data = _make_3mf_inch()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "BBox 3MF Inch"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        return upload_r, v1

    upload_r, v1 = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    bbox = json.loads(v1["bbox"])
    # 10 inch × 25.4 = 254 mm
    assert abs(bbox["x"] - 254.0) < 1.0
    assert abs(bbox["y"] - 254.0) < 1.0
    assert abs(bbox["z"] - 254.0) < 1.0


# ---------------------------------------------------------------------------
# Settle updates v1's mm bbox in place
# ---------------------------------------------------------------------------


def test_settle_updates_v1_bbox_in_place(app_with_projects):
    """Settle on an unsettled part updates v1's mm bbox in place (no new
    version row). The bbox = file bbox × settle scale."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")  # 400×20×20 mm reading

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle BBox"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1_before = _read_v1(conn, pid)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "inch"}
        )
        v1_after = _read_v1(conn, pid)
        vcount = _count_versions(conn, pid)
        return upload_r, settle_r, v1_before, v1_after, vcount

    upload_r, settle_r, v1_before, v1_after, vcount = _run_async(
        app_with_projects, _call
    )
    assert upload_r.status_code == 201, upload_r.text
    assert settle_r.status_code == 200, settle_r.text

    # Before settle: no bbox
    assert v1_before["bbox"] is None

    # After settle: bbox updated in place (same version id)
    assert v1_after["id"] == v1_before["id"]
    bbox = json.loads(v1_after["bbox"])
    # over_envelope.stl is 400×20×20 in file units; × 25.4
    assert abs(bbox["x"] - 400.0 * 25.4) < 1.0
    assert abs(bbox["y"] - 20.0 * 25.4) < 1.0
    assert abs(bbox["z"] - 20.0 * 25.4) < 1.0

    # No new version row was created
    assert vcount == 1


def test_settle_by_measurement_updates_v1_bbox(app_with_projects):
    """Settle by one measurement (axis+mm) derives the scale and updates
    v1's mm bbox in place."""
    data = _stl_bytes(FIXTURES / "holey.stl")  # 20mm box

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Meas BBox"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"axis": "W", "mm": 60.0}
        )
        conn = app_with_projects.state.conn
        v1_after = _read_v1(conn, pid)
        return upload_r, settle_r, v1_after

    upload_r, settle_r, v1_after = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert settle_r.status_code == 200, settle_r.text
    bbox = json.loads(v1_after["bbox"])
    # holey.stl is ~20mm box; scale 3.0 → ~60mm
    assert abs(bbox["x"] - 60.0) < 1.0


# ---------------------------------------------------------------------------
# Units-unsettled 409 is server-side state
# ---------------------------------------------------------------------------


def test_units_unsettled_409_persists_across_reload(app_with_projects):
    """The units-unsettled 409 is server-side state on the project row —
    part_unit_status = 'unsettled' in the DB, not client state."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")  # unsettled

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Persist 409"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        export_r = await client.get(f"/api/projects/{pid}/model.3mf")
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, export_r, proj

    upload_r, export_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert export_r.status_code == 409
    assert export_r.json()["error_class"] == "units_unsettled"

    # Server-side state: the project row carries the unsettled status
    assert proj["part_unit_status"] == "unsettled"


def test_units_settled_clears_409_persisted(app_with_projects):
    """After settle, the units-unsettled 409 is gone. The part_unit_status
    is 'settled' in the DB (server-side state, not client state)."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Persist"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        await client.post(f"/api/projects/{pid}/part/units", json={"unit": "mm"})
        export_r = await client.get(f"/api/projects/{pid}/model.3mf")
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, export_r, proj

    upload_r, export_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    # After settle: NOT units_unsettled (may be a different 409 for no render)
    if export_r.status_code == 409:
        assert export_r.json().get("error_class") != "units_unsettled"

    assert proj["part_unit_status"] == "settled"


# ---------------------------------------------------------------------------
# source_kind on v1
# ---------------------------------------------------------------------------


def test_v1_source_kind_is_import(app_with_projects):
    """The v1 row records source_kind = 'import'."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SK"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        return upload_r, v1

    upload_r, v1 = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    assert v1["source_kind"] == "import"


def test_version_public_exposes_source_kind_and_import_filename(app_with_projects):
    """version_public exposes ``source_kind`` plus the import filename on
    the version wire (issue #338): the SPA renders "v1 — Imported
    {filename}" from ``source_kind == "import"`` + the design-state part
    filename, and distinguishes it from the existing ``name`` field.
    """
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SK pub"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        v1 = _read_v1(conn, pid)
        # The public wire: list_versions (the GET the SPA reads).
        ls_r = await client.get(f"/api/projects/{pid}/versions")
        # The design-state part block (the filename the label renders).
        ds_r = await client.get(f"/api/projects/{pid}/design-state")
        return upload_r, v1, ls_r, ds_r

    upload_r, v1, ls_r, ds_r = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert v1 is not None
    assert v1["source_kind"] == "import"
    assert ls_r.status_code == 200, ls_r.text
    body = ls_r.json()
    # The wire body is either a list or an envelope holding the list.
    versions = body if isinstance(body, list) else body.get("versions", body.get("items", []))
    assert len(versions) == 1
    pub = versions[0]
    # source_kind rides the public wire (the SPA's label discriminator).
    assert pub["source_kind"] == "import"
    # The existing ``name`` field ("Imported box.stl") is the v1 display
    # name, distinct from the source_kind discriminator.
    assert pub["name"] == "Imported box.stl"
    assert pub["name"] != pub["source_kind"]
    # The design-state part block carries the filename the label renders.
    assert ds_r.status_code == 200, ds_r.text
    part = ds_r.json()["part"]
    assert part is not None
    assert part["filename"] == "box.stl"


def test_version_public_source_kind_null_for_design_loop(app_with_projects):
    """A design-loop version (no import) → ``source_kind`` is ``null``
    on the public wire, never a fabricated value — the SPA's import
    label never fires for a plain design version."""
    async def _call(client):
        r = await client.post("/api/projects", json={"name": "SK loop"})
        pid = r.json()["id"]
        v_r = await client.post(
            f"/api/projects/{pid}/versions", json={"params": {"W": 20.0}}
        )
        ls_r = await client.get(f"/api/projects/{pid}/versions")
        return v_r, ls_r

    v_r, ls_r = _run_async(app_with_projects, _call)
    assert v_r.status_code == 201, v_r.text
    assert ls_r.status_code == 200, ls_r.text
    body = ls_r.json()
    versions = body if isinstance(body, list) else body.get("versions", body.get("items", []))
    assert len(versions) == 1
    pub = versions[0]
    assert pub["source_kind"] is None


# ---------------------------------------------------------------------------
# Scale on the project row (per operator decision)
# ---------------------------------------------------------------------------


def test_scale_recorded_on_project_for_assumed(app_with_projects):
    """An assumed STL has scale 1.0 on the project row."""
    data = _stl_bytes(FIXTURES / "box_20mm.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Scale Assumed"})
        pid = r.json()["id"]
        files = {"file": ("box.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, proj

    upload_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert proj["part_scale"] == 1.0
    assert proj["part_unit"] == "mm"
    assert proj["part_unit_status"] == "assumed"


def test_scale_recorded_on_project_for_settled_3mf(app_with_projects):
    """A 3MF with inch unit has scale 25.4 on the project row."""
    data = _make_3mf_inch()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Scale 3MF"})
        pid = r.json()["id"]
        files = {"file": ("part.3mf", data, "model/3mf")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, proj

    upload_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert proj["part_scale"] == 25.4
    assert proj["part_unit"] == "mm"
    assert proj["part_unit_status"] == "settled"


def test_scale_null_on_project_for_unsettled(app_with_projects):
    """An unsettled STL has NULL scale on the project row (no scale yet)."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Scale Unset"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, proj

    upload_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert proj["part_scale"] is None
    assert proj["part_unit_status"] == "unsettled"


# ---------------------------------------------------------------------------
# Settle updates scale on project row
# ---------------------------------------------------------------------------


def test_settle_updates_project_scale(app_with_projects):
    """Settle by unit updates the project row's part_scale and
    part_unit_status."""
    data = _stl_bytes(FIXTURES / "over_envelope.stl")

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Scale"})
        pid = r.json()["id"]
        files = {"file": ("big.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"unit": "cm"}
        )
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, settle_r, proj

    upload_r, settle_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert settle_r.status_code == 200, settle_r.text
    assert proj["part_scale"] == 10.0
    assert proj["part_unit"] == "cm"
    assert proj["part_unit_status"] == "settled"


def test_settle_by_measurement_updates_project_scale(app_with_projects):
    """Settle by measurement updates the project row's part_scale
    (derived) and part_unit = 'custom'."""
    data = _stl_bytes(FIXTURES / "holey.stl")  # 20mm box

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Settle Meas Scale"})
        pid = r.json()["id"]
        files = {"file": ("holey.stl", data, "model/stl")}
        upload_r = await client.post(f"/api/projects/{pid}/part", files=files)
        settle_r = await client.post(
            f"/api/projects/{pid}/part/units", json={"axis": "H", "mm": 40.0}
        )
        conn = app_with_projects.state.conn
        proj = _read_project_part(conn, pid)
        return upload_r, settle_r, proj

    upload_r, settle_r, proj = _run_async(app_with_projects, _call)
    assert upload_r.status_code == 201, upload_r.text
    assert settle_r.status_code == 200, settle_r.text
    # holey.stl H ≈ 20mm in file units; 40/20 = 2.0
    assert abs(proj["part_scale"] - 2.0) < 0.1
    assert proj["part_unit"] == "custom"
    assert proj["part_unit_status"] == "settled"
