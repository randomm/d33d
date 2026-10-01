"""Part import: upload, parse/repair/measure, unit settlement (issue #325).

The backend half of "start a design from an existing file" (epic #283,
sub-issue 1):

- ``POST /api/projects/{id}/part`` — multipart upload of an STL (binary or
  ASCII) or 3MF mesh, stored as the project's part. Gate order per the
  operator decision: **413 first** (the body is streamed from
  ``request.stream()`` and refused mid-read once it exceeds
  ``MAX_PART_UPLOAD_BYTES`` — an oversize body is never parsed), then
  **400** (content type / extension), then **422** (unparseable, empty,
  non-finite, over the face cap, zip-bomb — ``detail`` is the verbatim
  ``web/src/copy.ts`` ``partUpload.unparseable`` string, the #299 way).
  On ANY error nothing is persisted: no project part columns, no version
  row, no committed file, no leftover temp file.
- ``POST /api/projects/{id}/part/units`` — settle the part's units:
  ``{"unit": "mm"|"cm"|"inch"}`` (fixed scale 1 / 10 / 25.4) or
  ``{"axis": "W"|"D"|"H", "mm": <positive float>}`` (one real measurement
  derives the scale, unit ``"custom"``). Settling UPDATES the project row
  and the v1 row's mm bbox in place under the shared write lock — it never
  creates a version, and is idempotent on an already-settled part.

Security (untrusted input, in the backend process only): trimesh parses in
process — no shell, no subprocess, and the user's filename is never a path
component (the file is written to a ``tempfile`` path and, on success,
committed under the fixed name ``versions/{v1_id}/part.stl`` /
``part.3mf``). Parse cost is bounded by the named ``MAX_PART_FACES`` cap
(checked after ``trimesh.load``, before repair). 3MF (a ZIP) is guarded
against zip bombs from the central directory BEFORE extraction (entry count
and declared-uncompressed total). Non-finite vertices are rejected both
before any extent math and again after pymeshfix (which can emit NaN).

The import facts live on the PROJECT (one part per project — a re-upload is
a 409 ``part_exists``): ``part_filename`` / ``part_format`` /
``part_unit`` / ``part_unit_status`` / ``part_scale`` / ``part_report`` /
``part_options``. The v1 version row records ``source_kind = "import"``
and its own mm bbox (``versions.source_kind``); the design-state route
surfaces the facts via its ``"part"`` envelope key and passes the v1 mm
bbox as the measurement once settled, so W/D/H render ``measured``
(``d33d.design_state`` untouched — the signature stays).
"""

from __future__ import annotations

import json
import math
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import trimesh
from fastapi import APIRouter, HTTPException, Request

from d33d import db as db_mod
from d33d.print_validation import QIDI_PLUS_5_ENVELOPE_MM
from d33d.versions import ImportCommitFailed

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

#: Max part upload body (50 MB — larger than the photo's 20 MB; a mesh is
#: bigger than a photo). The 413 fires mid-stream, before any parse.
MAX_PART_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

#: One read chunk of the streamed body (the same 1 MiB as the photo route).
_PART_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB

#: A multipart body carries framing overhead around the file field — the
#: 413 applies to the TOTAL body, so the allowance keeps a file just under
#: the cap from tripping on framing bytes alone.
_PART_MULTIPART_ALLOWANCE = 1024 * 1024  # 1 MiB

#: The named face cap for an import (a distinct constant from
#: ``print_validation.MAX_FACES`` — that bound belongs to the render
#: pipeline's gate 5 and must not be conflated with the import's parse-cost
#: bound). Checked right after ``trimesh.load``, on the total across ALL
#: geometries of the loaded scene, before repair.
MAX_PART_FACES = 2_000_000

#: 3MF zip-bomb guards (checked from the ZIP central directory BEFORE any
#: extraction — a bomb checked after extraction has already decompressed).
MAX_PART_ZIP_ENTRIES = 10_000
MAX_PART_ZIP_UNCOMPRESSED = MAX_PART_UPLOAD_BYTES

#: The part's stored in-repo name is FIXED — the user's filename is data
#: (v1 name, project columns, response) and never a path component.
PART_FILENAME = "part.stl"
PART_3MF_FILENAME = "part.3mf"

#: Content types accepted for each format (declared content types — the
#: octet-stream allowance is combined with the filename extension).
_STL_CONTENT_TYPES = {
    "model/stl",
    "application/sla",
    "application/vnd.ms-pki.stl",
    "application/octet-stream",
}
_3MF_CONTENT_TYPES = {
    "model/3mf",
    "application/vnd.ms-package.3dmanufacturing-3dmodel+xml",
    "application/octet-stream",
}

#: 3MF unit values (from the file's ``<model unit="...">`` attribute) and
#: their mm factor. Any unit outside this closed set is unconvertible — a
#: clean 422 (the ``_force_mm`` precedent), never a silent mm assumption.
_MM_UNIT_VALUES = {"mm", "millimeter", "millimeters", "millimetre", "cmm"}
_CM_UNIT_VALUES = {"cm", "centimeter", "centimeters", "centimetre"}
_INCH_UNIT_VALUES = {"in", "inch", "inches"}

#: The candidate units offered while an STL is unsettled (in priority order
#: for the "likeliest first" sort).
_CANDIDATE_UNITS = ("inch", "cm", "mm")

#: Axis lexicon → bbox component index (W→x, D→y, H→z).
_AXIS_TO_INDEX = {"W": 0, "D": 1, "H": 2}


# ---------------------------------------------------------------------------
# copy.ts verbatim wire strings (the SPA renders ``detail`` verbatim; the
# design-contract tripwire pins the two-way agreement, the #299 way)
# ---------------------------------------------------------------------------

PART_UPLOAD_UNSUPPORTED_DETAIL = (
    "That file type isn't supported. Upload an STL or 3MF mesh."
)
PART_UPLOAD_UNPARSEABLE_DETAIL = (
    "That file isn't a readable mesh. Check it opens in another 3D tool and try again."
)
PART_UPLOAD_SETTLE_INVALID_DETAIL = (
    "That unit choice isn't valid. Pick mm, cm, or inch — or give one measured axis."
)
PART_EXISTS_DETAIL = "This project already has a part."


# ---------------------------------------------------------------------------
# Exceptions (the 422 ladder — every failure is the same UNPARSEABLE detail)
# ---------------------------------------------------------------------------


class PartUploadError(ValueError):
    """A part upload failed the decode gate (unparseable, empty, non-finite,
    over the face cap, zip-bomb). The route maps it to the 422 with the
    verbatim ``partUpload.unparseable`` detail — no persistence on the way
    out (the route unlinks the temp file)."""


class ImportCommitError(Exception):
    """The import's version create failed after the row + file were written
    (the ``_run_create`` rollback already deleted the row and unlinked the
    mesh file — nothing is persisted). The route maps this to a 500.

    ``d33d.versions.ImportCommitFailed`` is the canonical type; this alias
    keeps the part-import module's public surface self-contained."""


# ---------------------------------------------------------------------------
# Unit handling
# ---------------------------------------------------------------------------


def _mm_factor_for_3mf_unit(unit: Any) -> float | None:
    """The mm factor for a 3MF file's declared unit, or ``None`` when the
    unit is absent/unreadable (3MF defaults to millimeters — the
    ``<model unit>`` attribute is optional per spec) or unconvertible
    (the caller 422s — never a silent mm assumption for a unit we cannot
    convert)."""
    if unit is None or not isinstance(unit, str):
        return 1.0
    u = unit.strip().lower()
    if u in _MM_UNIT_VALUES:
        return 1.0
    if u in _CM_UNIT_VALUES:
        return 10.0
    if u in _INCH_UNIT_VALUES:
        return 25.4
    return None


def _unit_scale(unit: str) -> float:
    """The file-units→mm scale for a settle-by-unit body (``mm``/``cm``/
    ``inch``). The caller validates ``unit`` is in this closed set."""
    return {"mm": 1.0, "cm": 10.0, "inch": 25.4}[unit]


def _fits_envelope(extents_mm: tuple[float, float, float]) -> bool:
    """True when every axis fits the QIDI X-Plus 5 build envelope (the named
    constant — the same value gate 7 reads; never a copy of the numbers)."""
    env = QIDI_PLUS_5_ENVELOPE_MM
    return all(e <= env[i] for i, e in enumerate(extents_mm))


def _candidate_option(
    unit: str, scale: float, file_extents: tuple[float, float, float]
) -> dict[str, Any]:
    """One candidate unit option: the unit word, its scale, and the
    resulting mm extents (file units × scale)."""
    mm = [e * scale for e in file_extents]
    largest = max(mm)
    return {
        "unit": unit,
        "scale": scale,
        "extents_mm": mm,
        "fits_envelope": _fits_envelope(tuple(mm)),
        "at_least_5mm": largest >= 5.0,
    }


def classify_stl_units(
    file_extents: tuple[float, float, float],
) -> dict[str, Any]:
    """The STL unit classification (STL carries no units):

    - largest side >= 5 mm AND the mm-reading fits the envelope →
      ``{"status": "assumed", "unit": "mm", "scale": 1.0}`` (shown as
      assumed — the user can still change it);
    - otherwise ``{"status": "unsettled", "options": [...]}`` with the
      candidate units (mm, cm, inch) each carrying its resulting mm
      extents, likeliest first: those whose largest mm side is >= 5 AND
      fits the envelope come first, in priority inch > cm > mm; the rest
      follow in the same priority.
    """
    mm_extents = file_extents
    largest_mm = max(mm_extents)
    if largest_mm >= 5.0 and _fits_envelope(mm_extents):
        return {"status": "assumed", "unit": "mm", "scale": 1.0, "options": None}
    options = [
        _candidate_option(unit, _unit_scale(unit), file_extents)
        for unit in _CANDIDATE_UNITS
    ]

    def _rank(opt: dict[str, Any]) -> tuple[int, int]:
        good = 1 if (opt["fits_envelope"] and opt["at_least_5mm"]) else 0
        # inch=0, cm=1, mm=2 — the priority within each tier.
        priority = _CANDIDATE_UNITS.index(opt["unit"])
        return (-good, priority)

    options.sort(key=_rank)
    return {"status": "unsettled", "unit": None, "scale": None, "options": options}


# ---------------------------------------------------------------------------
# Parse + repair + measure
# ---------------------------------------------------------------------------


def _boundary_loops(mesh: trimesh.Trimesh) -> int:
    """The number of boundary loops (connected open-edge components) on a
    mesh — one loop per gap. The repair report's ``gaps_closed`` is the
    count BEFORE repair minus AFTER (pymeshfix closes them)."""
    edges, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    open_edges = edges[counts == 1]
    if len(open_edges) == 0:
        return 0
    adj: dict[int, set[int]] = {i: set() for i in range(len(open_edges))}
    pos: dict[tuple[int, int], int] = {}
    for i, (a, b) in enumerate(open_edges):
        pos[(int(a), int(b))] = i
    for i, (a, b) in enumerate(open_edges):
        j = pos.get((int(b), int(a)))
        if j is not None:
            adj[i].add(j)
            adj[j].add(i)
    seen: set[int] = set()
    n = 0
    for i in range(len(open_edges)):
        if i in seen:
            continue
        n += 1
        stack = [i]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for y in adj[x]:
                if y not in seen:
                    stack.append(y)
    return n


def _total_faces(loaded: Any) -> int:
    """The total face count across ALL geometries (a Scene's geos, or a
    single Trimesh) — the face cap applies to the total, never just the
    first body."""
    if isinstance(loaded, trimesh.Scene):
        return sum(len(g.faces) for g in loaded.geometry.values())
    if isinstance(loaded, trimesh.Trimesh):
        return len(loaded.faces)
    return 0


def _check_zip_bomb(data: bytes) -> None:
    """The 3MF zip-bomb guard: entry count + declared-uncompressed total
    from the ZIP central directory, BEFORE any extraction (a bomb checked
    after extraction has already decompressed). Raises
    ``PartUploadError`` (the 422) on a violation or a non-zip."""
    import io as _io

    try:
        zf = zipfile.ZipFile(_io.BytesIO(data))
    except (zipfile.BadZipFile, OSError) as e:
        raise PartUploadError(f"not a zip: {e}") from e
    with zf:
        infos = zf.infolist()
        if len(infos) > MAX_PART_ZIP_ENTRIES:
            raise PartUploadError(
                f"3MF has {len(infos)} entries; max {MAX_PART_ZIP_ENTRIES}"
            )
        total = 0
        for info in infos:
            # ``file_size`` is the declared uncompressed size (the bomb
            # signal — a stored/deflated entry both declare it). ``None``/
            # 0 on a streaming entry falls back to the compressed size
            # (never an underestimate of the extraction cost).
            declared = info.file_size or info.compress_size
            total += declared
            if total > MAX_PART_ZIP_UNCOMPRESSED:
                raise PartUploadError(
                    f"3MF uncompressed size {total} exceeds "
                    f"{MAX_PART_ZIP_UNCOMPRESSED}"
                )


def _is_finite(mesh: trimesh.Trimesh) -> bool:
    """True when every vertex coordinate is finite (NaN/Inf pass trimesh
    load silently and poison the extent math — checked before it)."""
    return bool(np.isfinite(mesh.vertices).all())


def parse_and_repair(
    data: bytes, part_format: str
) -> tuple[trimesh.Trimesh, dict[str, Any], str | None]:
    """Parse ``data`` (STL or 3MF), apply the face cap + finiteness gates,
    run the repair chain (merge_vertices + pymeshfix + fix_normals — NO
    decimation of the user's part), and measure.

    Returns ``(mesh, report, file_unit)`` where ``mesh`` is the repaired
    mesh in FILE units (the bbox is measured in file units BEFORE any unit
    conversion), ``report`` is the repair report
    (``{triangles, bodies, watertight, gaps_closed, bbox_file_units}``),
    and ``file_unit`` is the 3MF's declared unit string (``None`` for STL —
    unitless) so the caller can derive the mm scale.

    Raises ``PartUploadError`` (the 422) on any failure: unparseable,
    empty, over the face cap, non-finite (pre- or post-repair), or an
    unconvertible 3MF unit.
    """
    import io as _io

    # Load via a file object with an EXPLICIT loader (``file_type``) — the
    # bytes are never written to a path carrying the user's filename, and
    # the loader is chosen by the DETECTED format, not a filename suffix.
    try:
        if part_format == "3mf":
            _check_zip_bomb(data)
            loaded = trimesh.load(_io.BytesIO(data), file_type="3mf")
        else:
            loaded = trimesh.load(_io.BytesIO(data), file_type="stl")
    except PartUploadError:
        raise
    except Exception as e:
        raise PartUploadError(f"unparseable {part_format}: {e}") from e

    # Face cap: the TOTAL across all geometries, checked right after load
    # and before repair (bounds parse cost).
    total_faces = _total_faces(loaded)
    if total_faces > MAX_PART_FACES:
        raise PartUploadError(
            f"mesh has {total_faces} faces; max {MAX_PART_FACES}"
        )

    # Flatten to a single mesh (a Scene is a multi-body import — the v1
    # contract is single-part, but the code must not crash or silently drop
    # bodies; ``to_mesh`` concatenates).
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise PartUploadError("3MF has no geometry")
        merged = loaded.to_mesh()
    else:
        merged = loaded
    if len(merged.faces) == 0:
        raise PartUploadError("mesh is empty")

    # Finiteness BEFORE any extent math (NaN/Inf poison the bbox and the
    # JSON report).
    if not _is_finite(merged):
        raise PartUploadError("mesh has non-finite vertices")

    # Body count: connected components after merging (the honest count the
    # report carries — a multi-body import is reported, not dropped).
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    bodies = len(merged.split(only_watertight=True))
    if len(merged.faces) == 0:
        raise PartUploadError("mesh is empty after cleanup")

    # The bbox in FILE units, BEFORE repair (the unit conversion multiplies
    # this; pymeshfix preserves the bounding box for a gapped mesh).
    file_bbox = tuple(float(e) for e in merged.extents)
    if not all(math.isfinite(e) for e in file_bbox) or max(file_bbox) <= 0:
        raise PartUploadError("mesh has invalid extents")

    # The repair chain (print_validation's steps, minus decimation — the
    # render pipeline decimates for its own gates; the user's part is
    # preserved): merge → pymeshfix.repair → fix_normals.
    gaps_before = _boundary_loops(merged)
    import pymeshfix as _pmf

    try:
        fix = _pmf.MeshFix(
            merged.vertices.astype(np.float64), merged.faces.astype(np.int32)
        )
        fix.repair()
        repaired = trimesh.Trimesh(
            np.asarray(fix.points, dtype=np.float64),
            np.asarray(fix.faces, dtype=np.int32),
            process=False,
        )
    except PartUploadError:
        raise
    except Exception as e:
        raise PartUploadError(f"repair failed: {e}") from e

    # Finiteness AGAIN after pymeshfix (it can emit NaN from degenerate
    # input) — the same 422, nothing persisted.
    if not _is_finite(repaired):
        raise PartUploadError("mesh has non-finite vertices after repair")
    if len(repaired.faces) == 0:
        raise PartUploadError("mesh is empty after repair")

    trimesh.repair.fix_normals(repaired)
    gaps_after = _boundary_loops(repaired)

    # The 3MF's declared unit (``None`` for STL — unitless). A 3MF unit
    # trimesh cannot convert is a clean failure (the ``_force_mm``
    # precedent), never a silent mm assumption.
    file_unit: str | None = None
    if part_format == "3mf":
        file_unit = _mesh_units(merged) if merged is not None else None
        # Read the unit from the ORIGINAL (pre-repair) geometry when
        # possible — repair does not change units, and the original load
        # carried it.
        if file_unit is None:
            file_unit = _mesh_units(
                next(iter(loaded.geometry.values()))
                if isinstance(loaded, trimesh.Scene)
                else loaded
            )
        if file_unit is not None and _mm_factor_for_3mf_unit(file_unit) is None:
            raise PartUploadError(f"unconvertible 3MF unit {file_unit!r}")

    report = {
        "triangles": len(repaired.faces),
        "bodies": bodies,
        "watertight": bool(repaired.is_watertight),
        "gaps_closed": max(0, gaps_before - gaps_after),
        "bbox_file_units": file_bbox,
    }
    return repaired, report, file_unit


def _mesh_units(mesh: Any) -> str | None:
    """The mesh's declared unit string (``None`` when trimesh has no
    string unit — STL is unitless, a 3MF without a ``unit`` attribute
    defaults to millimeters)."""
    units = getattr(mesh, "units", None)
    if isinstance(units, str) and units:
        return units
    return None


# ---------------------------------------------------------------------------
# Part persistence (project columns + the v1 version row)
# ---------------------------------------------------------------------------


def _part_public(row: dict[str, Any]) -> dict[str, Any] | None:
    """The project row's part facts as a public object (``None`` when the
    project has no part — the NULL columns decode to ``None``, never a
    fabricated empty dict)."""
    if not row.get("part_filename"):
        return None
    report = row.get("part_report")
    options = row.get("part_options")
    return {
        "filename": row.get("part_filename"),
        "format": row.get("part_format"),
        "unit": row.get("part_unit"),
        "unit_status": row.get("part_unit_status"),
        "scale": row.get("part_scale"),
        "report": json.loads(report) if report else None,
        "options": json.loads(options) if options else None,
    }


def _decode_part_row(row: dict[str, Any]) -> dict[str, Any]:
    """Decode a raw project row's part JSON columns (the design-state route
    reads them — the ``_public_project_row`` masks git but passes these
    through as raw JSON strings, so the route needs the decoded shape)."""
    report = row.get("part_report")
    options = row.get("part_options")
    return {
        "filename": row.get("part_filename"),
        "format": row.get("part_format"),
        "unit": row.get("part_unit"),
        "unit_status": row.get("part_unit_status"),
        "scale": row.get("part_scale"),
        "report": json.loads(report) if report else None,
        "options": json.loads(options) if options else None,
    }


def _v1_for_part(conn: db_mod.Connection, project_id: int) -> dict[str, Any] | None:
    """The project's v1 (the import's) version row — the row the settle
    updates its mm bbox on, and the row the design-state route reads the
    measurement from. ``None`` when the project has no version."""
    row = conn.raw.execute(
        "SELECT * FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
        (project_id,),
    ).fetchone()
    if row is None:
        return None
    return dict(row)


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_part_router() -> APIRouter:
    """The part-upload + unit-settle router (mounted by ``create_app``)."""
    router = APIRouter(prefix="/api/projects", tags=["part-import"])

    # -- POST /{project_id}/part ------------------------------------------------

    @router.post("/{project_id}/part", status_code=201)
    async def upload_part(request: Request, project_id: int) -> dict[str, Any]:
        """Multipart part upload (STL or 3MF, ≤ 50 MB).

        Gate order (the operator decision — 413 BEFORE 400/422): the body
        is streamed from ``request.stream()`` and 413'd mid-read once it
        exceeds ``MAX_PART_UPLOAD_BYTES`` + the 1 MiB multipart allowance
        (an oversize body is never parsed); then 400 for an unsupported
        content type / extension (``partUpload.unsupported``); then 422
        unparseable (``partUpload.unparseable``). On any error nothing is
        persisted.
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        # Re-import: one part per project — a project that already has a
        # part is a 409 (checked BEFORE the size gate: a settled part does
        # not get a second upload regardless of body size).
        if row.get("part_filename"):
            raise HTTPException(
                status_code=409,
                detail={"code": "part_exists", "message": PART_EXISTS_DETAIL},
            )

        # 413 FIRST: stream the raw body; refuse mid-read on total bytes.
        # The oversize body is never parsed (the multipart is never built).
        cap = MAX_PART_UPLOAD_BYTES + _PART_MULTIPART_ALLOWANCE
        buf = bytearray()
        async for chunk in request.stream():
            buf.extend(chunk)
            if len(buf) > cap:
                buf.clear()
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {MAX_PART_UPLOAD_BYTES} byte limit",
                )
        body = bytes(buf)

        # 400: the multipart must carry a ``file`` field whose content type
        # + extension name a supported format (STL or 3MF). The octet-stream
        # allowance is combined with the filename extension (a .stl or
        # .3mf name with octet-stream is accepted).
        if "multipart/form-data" not in request.headers.get("content-type", ""):
            raise HTTPException(
                status_code=400,
                detail=PART_UPLOAD_UNSUPPORTED_DETAIL,
            )
        try:
            form = await _parse_multipart(request, body)
        except (LookupError, ValueError, OSError) as e:
            raise HTTPException(
                status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL
            ) from e
        file = form.get("file")
        if file is None:
            raise HTTPException(
                status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL
            )
        file_content_type = (getattr(file, "content_type", None) or "").lower()
        filename = getattr(file, "filename", None) or ""
        part_format = _detect_part_format(file_content_type, filename)
        if part_format is None:
            raise HTTPException(
                status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL
            )
        content = bytes(await file.read())
        if not content:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL
            )

        # 422: the decode gate — parse/repair/measure. The bytes are written
        # to a TEMP file (a ``tempfile`` path — never the user's filename)
        # and unlinked on every failure path (nothing persisted on error).
        try:
            _mesh, report, file_unit = parse_and_repair(content, part_format)
        except PartUploadError:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL
            )

        # Unit classification (STL: plausible→assumed / else unsettled
        # options; 3MF: the file's unit, converted to mm — always settled).
        file_bbox = report["bbox_file_units"]
        if part_format == "3mf":
            factor = _mm_factor_for_3mf_unit(file_unit)
            assert factor is not None  # parse_and_repair 422'd if unconvertible
            scale = float(factor)
            unit = "mm"  # stored in mm (the 3MF unit is converted, not assumed)
            unit_status = "settled"
            options = None
            mm_bbox = [e * scale for e in file_bbox]
        else:
            cls = classify_stl_units(file_bbox)
            scale = cls.get("scale")
            unit = cls.get("unit")
            unit_status = cls["status"]
            options = cls.get("options")
            mm_bbox = [e * scale for e in file_bbox] if unit_status == "assumed" else None

        # v1: "Imported {filename}" (the filename sanitised for display;
        # stored verbatim as data, never interpreted), recording
        # source_kind "import" + the mm bbox (file bbox × scale). Written
        # + committed under the shared write lock; the mesh file is
        # committed in the SAME commit as v1's params.json.
        svc = getattr(request.app.state, "versions", None)
        if svc is None:  # pragma: no cover - the app lifespan always wires it
            raise HTTPException(status_code=500, detail="version service not wired")

        v1_name = _import_version_name(filename)
        repo_path = Path(row["git_repo_path"])
        stored_name = PART_3MF_FILENAME if part_format == "3mf" else PART_FILENAME

        async def _commit_import() -> int:
            # Write the mesh to a temp file under the repo (the FIXED name
            # — the user's filename never a path component), commit it in
            # the same commit as v1's params.json.
            v1_id = svc._run_import_create(
                project_id,
                version_name=v1_name,
                params={},
                message=f"part import: {filename or 'part'}",
                bbox=(tuple(mm_bbox) if mm_bbox is not None else None),
                source_kind="import",
                mesh_bytes=content,
                stored_name=stored_name,
                repo_dir=repo_path,
            )
            return v1_id

        try:
            v1_id = await svc._with_project_lock(project_id, _commit_import)
        except ImportCommitFailed as e:
            # Nothing persisted: no project part columns, no version row
            # (the _run_create rollback deleted it), no committed file, no
            # temp file.
            raise HTTPException(status_code=500, detail=f"part commit failed: {e}") from e
        except (LookupError, ValueError) as e:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL
            ) from e

        # Persist the part facts on the PROJECT (one part per project).
        conn.raw.execute(
            "UPDATE projects SET part_filename = ?, part_format = ?, part_unit = ?,"
            " part_unit_status = ?, part_scale = ?, part_report = ?, part_options = ?"
            " WHERE id = ?",
            (
                filename or None,
                part_format,
                unit,
                unit_status,
                scale,
                json.dumps(report),
                json.dumps(options) if options is not None else None,
                project_id,
            ),
        )
        conn.commit()

        updated = conn.get_project(project_id)
        assert updated is not None
        return {
            "id": project_id,
            "version_id": v1_id,
            "part": _part_public(updated),
        }

    # -- POST /{project_id}/part/units ----------------------------------------

    @router.post("/{project_id}/part/units")
    async def settle_units(request: Request, project_id: int) -> dict[str, Any]:
        """Settle the part's units. Body: ``{"unit": "mm"|"cm"|"inch"}``
        (scale 1 / 10 / 25.4) or ``{"axis": "W"|"D"|"H", "mm": <positive
        float>}`` (one measurement derives the scale, unit ``"custom"``).
        A body matching neither shape is a 422. Settling UPDATES the
        project row and the v1's mm bbox in place under the shared write
        lock (never a new version) and is idempotent on an already-settled
        part."""
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not row.get("part_filename"):
            raise HTTPException(
                status_code=404, detail="project has no part to settle"
            )

        body = await _settle_body(request)
        part = _decode_part_row(row)
        report = part["report"] or {}
        file_bbox = tuple(report.get("bbox_file_units") or [])
        if len(file_bbox) != 3:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
            )

        if "unit" in body:
            unit = body["unit"]
            if unit not in ("mm", "cm", "inch"):
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            scale = _unit_scale(unit)
        else:
            axis = body.get("axis")
            mm = body.get("mm")
            if axis not in _AXIS_TO_INDEX or not _is_positive_number(mm):
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            unit = "custom"
            extent_file = file_bbox[_AXIS_TO_INDEX[axis]]
            if not extent_file or extent_file <= 0:
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            scale = float(mm) / extent_file

        mm_bbox = [e * scale for e in file_bbox]

        svc = getattr(request.app.state, "versions", None)
        if svc is None:  # pragma: no cover
            raise HTTPException(status_code=500, detail="version service not wired")

        async def _settle() -> None:
            # Update the project row's part facts AND the v1's mm bbox in
            # place (under the shared write lock — the same lock the
            # version writes hold, so a concurrent create never races the
            # settle's row update).
            conn.raw.execute(
                "UPDATE projects SET part_unit = ?, part_unit_status = 'settled',"
                " part_scale = ?, part_options = NULL WHERE id = ?",
                (unit, scale, project_id),
            )
            v1 = _v1_for_part(conn, project_id)
            if v1 is not None:
                conn.raw.execute(
                    "UPDATE versions SET bbox = ? WHERE id = ?",
                    (
                        json.dumps(
                            {
                                "x": mm_bbox[0],
                                "y": mm_bbox[1],
                                "z": mm_bbox[2],
                            }
                        ),
                        v1["id"],
                    ),
                )
            conn.commit()

        await svc._with_project_lock(project_id, _settle)

        updated = conn.get_project(project_id)
        assert updated is not None
        return {
            "id": project_id,
            "part": _part_public(updated),
            "bbox_mm": mm_bbox,
        }

    return router


def _detect_part_format(content_type: str, filename: str) -> str | None:
    """The part format from the content type + filename extension (``None``
    when neither names a supported format — the 400). The octet-stream
    allowance requires a matching extension (a bare octet-stream with no
    .stl/.3mf name is unsupported)."""
    name = Path(filename).name.lower() if filename else ""
    # octet-stream is in both sets — the extension disambiguates
    if content_type == "application/octet-stream":
        if name.endswith(".stl"):
            return "stl"
        if name.endswith(".3mf"):
            return "3mf"
        return None
    if content_type in _STL_CONTENT_TYPES:
        return "stl"
    if content_type in _3MF_CONTENT_TYPES:
        return "3mf"
    return None


async def _parse_multipart(request: Request, body: bytes):
    """Parse an already-streamed multipart body (the 413 already fired on
    the raw bytes — this never re-reads the stream)."""
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    async def _chunked(data: bytes):
        yield data

    headers = Headers(raw=[(k.lower().encode(), v.encode()) for k, v in request.headers.items()])
    parsed = MultiPartParser(headers, _chunked(body))
    return await parsed.parse()


def _import_version_name(filename: str) -> str:
    """The v1 display name: ``Imported {filename}`` with the filename
    sanitised for display (control chars stripped, whitespace collapsed,
    capped). The stored ``part_filename`` is the VERBATIM name — this is
    display-only, never a path or a re-interpreted value."""
    from d33d.versions import clean_name

    base = filename.strip() or "part"
    name = f"Imported {base}"
    return clean_name(name, existing_names=None)


def _is_positive_number(v: Any) -> bool:
    return (
        isinstance(v, (int, float))
        and not isinstance(v, bool)
        and math.isfinite(float(v))
        and float(v) > 0
    )


async def _settle_body(request: Request) -> dict[str, Any]:
    """Parse + validate the settle body (``{"unit"}`` or ``{"axis","mm"}``
    — a body matching neither is a 422 with the named detail)."""
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL)
    if "unit" in data:
        if not isinstance(data["unit"], str):
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
            )
        return {"unit": data["unit"]}
    if "axis" in data and "mm" in data:
        return {"axis": data["axis"], "mm": data["mm"]}
    raise HTTPException(status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL)


__all__ = [
    "MAX_PART_FACES",
    "MAX_PART_UPLOAD_BYTES",
    "PART_UPLOAD_SETTLE_INVALID_DETAIL",
    "PART_UPLOAD_UNPARSEABLE_DETAIL",
    "PART_UPLOAD_UNSUPPORTED_DETAIL",
    "classify_stl_units",
    "create_part_router",
    "parse_and_repair",
]
