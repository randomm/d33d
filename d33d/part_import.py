"""Part import router: upload, unit settlement, export gate (issue #325).

The HTTP half of "start a design from an existing file" (epic #283,
sub-issue 1). The decode (parse/repair/measure) and the unit math live in
``d33d.part_mesh`` / ``d33d.part_units``; this module owns the routes, the
upload bounds, and the multipart handling.

- ``POST /api/projects/{id}/part`` — multipart upload of an STL (binary
  or ASCII) or 3MF mesh, stored as the project's part. Gate order per the
  operator decision: **413 first** (the body is streamed from
  ``request.stream()`` and accumulated in memory up to
  ``MAX_PART_UPLOAD_BYTES`` + a 1 MiB multipart allowance, then refused —
  an oversize body is never parsed), then **400** (content type /
  extension), then **422** (unparseable, empty, non-finite, over the face
  cap, zip-bomb, unconvertible 3MF unit — ``detail`` is the verbatim
  ``web/src/copy.ts`` ``partUpload.unparseable`` string, the #299 way).
  The decode runs OFF the event loop (``asyncio.to_thread``) — the mesh
  parse is CPU-bound and must not stall the app's other requests. On ANY
  error nothing is persisted: no project part columns, no version row, no
  committed file (the commit-failure rollback deletes the row and the
  mesh file; the part columns are written in the SAME transaction as the
  version row, so a failed import cannot leave half a row behind).
- ``POST /api/projects/{id}/part/units`` — settle the part's units:
  ``{"unit": "mm"|"cm"|"inch"}`` (fixed scale 1 / 10 / 25.4) or
  ``{"axis": "W"|"D"|"H", "mm": <positive float>}`` (one real measurement
  derives the scale, unit ``"custom"``). Settling UPDATES the project row
  and the v1 row's mm bbox in place in ONE transaction under the shared
  write lock — it never creates a version, and is idempotent on an
  already-settled part.

Security (untrusted input, in the backend process only): trimesh parses in
process — no shell, no subprocess, and the user's filename is never a path
component (the bytes are parsed in memory and, on success, written only to
the fixed in-repo name ``versions/{v1_id}/part.stl`` / ``part.3mf`` —
there is no temp file). Parse cost is bounded by the named
``MAX_PART_FACES`` cap (checked after ``trimesh.load``, before repair).
3MF (a ZIP) is guarded against zip bombs from the central directory BEFORE
extraction (entry count and declared-uncompressed total). Non-finite
vertices are rejected both before any extent math and again after
pymeshfix (which can emit NaN).

The import facts live on the PROJECT (one part per project — a re-upload
is a 409 ``part_exists``): ``part_filename`` / ``part_format`` /
``part_unit`` / ``part_unit_status`` / ``part_scale`` / ``part_report`` /
``part_options``. The v1 version row records ``source_kind = "import"``
and its own mm bbox (``versions.source_kind``); the design-state route
surfaces the facts via its ``"part"`` envelope key and passes the v1 mm
bbox as the measurement once settled, so W/D/H render ``measured``
(``d33d.design_state`` untouched — the signature stays).
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request

from d33d import db as db_mod
from d33d.part_mesh import (
    MAX_PART_FACES,
    PartUploadError,
    parse_and_repair,
)
from d33d.part_units import (
    axis_to_index,
    classify_stl_units,
    mm_factor_for_unit,
    settle_scale,
    settle_unit_choices,
)
from d33d.versions import ImportCommitFailed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

#: Max part upload body (50 MB — larger than the photo's 20 MB; a mesh is
#: bigger than a photo). The 413 fires once the accumulated body exceeds
#: this (plus the multipart allowance) — before any parse.
MAX_PART_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB

#: A multipart body carries framing overhead around the file field — the
#: 413 applies to the TOTAL body, so the allowance keeps a file just under
#: the cap from tripping on framing bytes alone.
_PART_MULTIPART_ALLOWANCE = 1024 * 1024  # 1 MiB

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
# Part persistence (project columns + the v1 version row)
# ---------------------------------------------------------------------------


def part_public(row: dict[str, Any]) -> dict[str, Any] | None:
    """The project row's part facts as a public object (``None`` when the
    project has no part — the NULL columns decode to ``None``, never a
    fabricated empty dict). The design-state route reads the same JSON
    columns through this helper."""
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
        "options": json.loads(options) if options is not None else None,
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
        is streamed from ``request.stream()`` and accumulated up to
        ``MAX_PART_UPLOAD_BYTES`` + the 1 MiB multipart allowance, then
        refused with a 413 (an oversize body is never parsed); then 400
        for an unsupported content type / extension
        (``partUpload.unsupported``); then 422 unparseable
        (``partUpload.unparseable``). The decode (parse/repair/measure,
        including the zip-bomb check) runs off the event loop
        (``asyncio.to_thread``). On any error nothing is persisted.
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

        # 413 FIRST: stream the raw body, refusing once the accumulated
        # total exceeds the cap (the oversize body is never parsed — the
        # multipart is never built, the mesh is never decoded).
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
            logger.warning("part upload multipart parse failed: %r", e)
            raise HTTPException(
                status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL
            ) from e
        file = form.get("file")
        if file is None:
            raise HTTPException(status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL)
        file_content_type = (getattr(file, "content_type", None) or "").lower()
        filename = getattr(file, "filename", None) or ""
        part_format = _detect_part_format(file_content_type, filename)
        if part_format is None:
            raise HTTPException(status_code=400, detail=PART_UPLOAD_UNSUPPORTED_DETAIL)
        content = bytes(await file.read())
        if not content:
            raise HTTPException(status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL)

        # 422: the decode gate — parse/repair/measure, run OFF the event
        # loop (``asyncio.to_thread`` — the mesh parse is CPU-bound and
        # must not stall the app's other requests; the zip-bomb check runs
        # inside the same thread). The bytes are parsed in memory — there
        # is no temp file, and the user's filename never touches a path.
        try:
            _mesh, report, file_unit = await asyncio.to_thread(
                parse_and_repair, content, part_format
            )
        except PartUploadError:
            raise HTTPException(status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL)

        # Unit handling (STL: plausible→assumed / else unsettled options;
        # 3MF: the file's unit, converted to mm — always settled). Only an
        # ABSENT unit (``None``) is the 3MF default (mm); a non-string or
        # unconvertible unit 422s here (the decode returns the declared
        # unit string and does not validate it — the conversion is the
        # unit layer's job).
        file_bbox = report["bbox_file_units"]
        if part_format == "3mf":
            try:
                scale = float(mm_factor_for_unit(file_unit))
            except PartUploadError:
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL
                )
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
            mm_bbox = (
                [e * scale for e in file_bbox] if unit_status == "assumed" else None
            )

        # v1: "Imported {filename}" (the filename sanitised for display;
        # stored verbatim as data, never interpreted), recording
        # source_kind "import" + the mm bbox (file bbox × scale). Written
        # + committed under the shared write lock; the mesh file is
        # committed in the SAME commit as v1's params.json. The part
        # columns are written in the SAME transaction as the version row
        # (see ``_run_import_create``) — any failure (git commit, sqlite)
        # rolls back the version row, the part columns, the files, and the
        # newly created ``versions/`` dirs together, and the retry upload
        # succeeds (no stuck 409).
        svc = getattr(request.app.state, "versions", None)
        if svc is None:  # pragma: no cover - the app lifespan always wires it
            raise HTTPException(status_code=500, detail="version service not wired")

        v1_name = _import_version_name(filename)
        repo_path = Path(row["git_repo_path"])
        stored_name = PART_3MF_FILENAME if part_format == "3mf" else PART_FILENAME

        async def _commit_import() -> int:
            # No project lock is held across the ``to_thread`` call above
            # (the decode finished before the lock was acquired); this
            # write-lock region covers only the version row + files +
            # part columns, all in one transaction.
            return svc._run_import_create(
                project_id,
                version_name=v1_name,
                params={},
                message=f"part import: {filename or 'part'}",
                bbox=(tuple(mm_bbox) if mm_bbox is not None else None),
                source_kind="import",
                mesh_bytes=content,
                stored_name=stored_name,
                repo_dir=repo_path,
                part_values={
                    "part_filename": filename or None,
                    "part_format": part_format,
                    "part_unit": unit,
                    "part_unit_status": unit_status,
                    "part_scale": scale,
                    "part_report": json.dumps(report),
                    "part_options": json.dumps(options)
                    if options is not None
                    else None,
                },
            )

        try:
            v1_id = await svc._with_project_lock(project_id, _commit_import)
        except ImportCommitFailed as e:
            # Nothing persisted: no project part columns, no version row,
            # no committed file, no leftover versions/ dir (the
            # _run_import_create rollback rolled back the row + the part
            # columns + the files in one transaction).
            raise HTTPException(
                status_code=500, detail=f"part commit failed: {e}"
            ) from e
        except (LookupError, ValueError) as e:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_UNPARSEABLE_DETAIL
            ) from e

        updated = conn.get_project(project_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "id": project_id,
            "version_id": v1_id,
            "part": part_public(updated),
        }

    # -- POST /{project_id}/part/units ----------------------------------------

    @router.post("/{project_id}/part/units")
    async def settle_units(request: Request, project_id: int) -> dict[str, Any]:
        """Settle the part's units. Body: ``{"unit": "mm"|"cm"|"inch"}``
        (scale 1 / 10 / 25.4) or ``{"axis": "W"|"D"|"H", "mm": <positive
        float>}`` (one measurement derives the scale, unit ``"custom"``).
        A body matching neither shape is a 422. Settling UPDATES the
        project row and the v1's mm bbox in place in ONE transaction under
        the shared write lock (never a new version) and is idempotent on an
        already-settled part."""
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        if not row.get("part_filename"):
            raise HTTPException(status_code=404, detail="project has no part to settle")

        body = await _settle_body(request)
        part = part_public(row) or {}
        report = part.get("report") or {}
        file_bbox = tuple(report.get("bbox_file_units") or [])
        if len(file_bbox) != 3:
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
            )

        axis_idx = axis_to_index()
        if "unit" in body:
            unit = body["unit"]
            if unit not in settle_unit_choices():
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            scale = settle_scale(unit)
        else:
            axis = body.get("axis")
            mm = body.get("mm")
            if axis not in axis_idx or not _is_positive_number(mm):
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            unit = "custom"
            extent_file = file_bbox[axis_idx[axis]]
            if not extent_file or extent_file <= 0:
                raise HTTPException(
                    status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
                )
            scale = float(mm) / extent_file

        mm_bbox = [e * scale for e in file_bbox]

        svc = getattr(request.app.state, "versions", None)
        if svc is None:  # pragma: no cover
            raise HTTPException(status_code=500, detail="version service not wired")

        def _settle_sync() -> None:
            # Update the project row's part facts AND the v1's mm bbox in
            # ONE transaction (under the shared write lock — the same lock
            # the version writes hold, so a concurrent create never races
            # the settle's row update). On any exception the transaction
            # rolls back and the error re-raises — the project and the v1
            # row are both left unchanged, never half-settled.
            try:
                conn.raw.execute("BEGIN")
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
            except BaseException:
                conn.rollback()
                raise

        try:
            await svc._with_project_lock(project_id, lambda: _settle_sync())
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"settle failed: {e}") from e

        updated = conn.get_project(project_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "id": project_id,
            "part": part_public(updated),
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

    headers = Headers(
        raw=[(k.lower().encode(), v.encode()) for k, v in request.headers.items()]
    )
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
    — a body matching neither, or with a non-string unit / non-numeric axis
    or mm, is a 422 with the named detail). The type checks run in the
    same place as the value checks, so a shape that passes the dispatch is
    always safe to read."""
    data = await request.json()
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL)
    if "unit" in data:
        unit = data["unit"]
        if not isinstance(unit, str):
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
            )
        return {"unit": unit}
    if "axis" in data and "mm" in data:
        axis = data["axis"]
        mm = data["mm"]
        if not isinstance(axis, str) or not _is_positive_number(mm):
            raise HTTPException(
                status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL
            )
        return {"axis": axis, "mm": mm}
    raise HTTPException(status_code=422, detail=PART_UPLOAD_SETTLE_INVALID_DETAIL)


__all__ = [
    "MAX_PART_FACES",
    "MAX_PART_UPLOAD_BYTES",
    "PART_UPLOAD_SETTLE_INVALID_DETAIL",
    "PART_UPLOAD_UNPARSEABLE_DETAIL",
    "PART_UPLOAD_UNSUPPORTED_DETAIL",
    "classify_stl_units",
    "create_part_router",
    "part_public",
]
