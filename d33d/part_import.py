"""Part import router: upload, unit settlement, export gate (issue #325).

The HTTP half of "start a design from an existing file" (epic #283,
sub-issue 1). The decode (parse/repair/measure) and the unit math live in
``d33d.part_mesh`` / ``d33d.part_units``; this module owns the routes, the
upload bounds, and the multipart handling.

- ``POST /api/projects/{id}/part`` — multipart upload of an STL (binary
  or ASCII) or 3MF mesh, stored as the project's part. Gate order: **413
  first** (the body is streamed and accumulated in memory up to
  ``MAX_PART_UPLOAD_BYTES`` + a 1 MiB multipart allowance, then refused),
  then **400** (content type / extension), then **422** (unparseable,
  empty, non-finite, over the face cap, zip-bomb, unconvertible 3MF unit).
  The decode runs OFF the event loop (``asyncio.to_thread``). On ANY error
  nothing is persisted (the commit-failure rollback deletes the row and
  the mesh file in the SAME transaction).
- ``POST /api/projects/{id}/part/units`` — settle the part's units:
  ``{"unit": "mm"|"cm"|"inch"}`` (fixed scale 1 / 10 / 25.4) or
  ``{"axis": "W"|"D"|"H", "mm": <positive float>}`` (one measurement
  derives the scale, unit ``"custom"``). Settling UPDATES the project row
  and the v1 row's mm bbox in place in ONE transaction under the shared
  write lock — never creates a version, idempotent on an already-settled
  part.

Security: trimesh parses in-process (no shell, no temp file); the user's
filename is never a path component. The pymeshfix repair runs in a separate
per-call process with a timeout (see ``part_repair.REPAIR_TIMEOUT_SECONDS``).
Parse cost is bounded by ``MAX_PART_FACES``. 3MF (a ZIP) is guarded against
zip bombs from the central directory BEFORE extraction. Non-finite vertices
are rejected before any extent math and again after pymeshfix.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response

from d33d import db as db_mod
from d33d.part_errors import REPAIR_TIMEOUT_DETAIL, RepairTimeoutError
from d33d.part_http import (
    _PART_MULTIPART_ALLOWANCE,
    MAX_PART_UPLOAD_BYTES,
    PART_3MF_FILENAME,
    PART_EXISTS_DETAIL,
    PART_FILENAME,
    PART_UPLOAD_COMMIT_FAILED_DETAIL,
    PART_UPLOAD_DECODE_FAILED_DETAIL,
    PART_UPLOAD_SETTLE_INVALID_DETAIL,
    PART_UPLOAD_UNPARSEABLE_DETAIL,
    PART_UPLOAD_UNSUPPORTED_DETAIL,
    _detect_part_format,
    _import_version_name,
    _is_positive_number,
    _parse_multipart,
    _v1_for_part,
    part_public,
    resolve_v1_part_path,
)
from d33d.part_mesh import (
    MAX_PART_FACES,
    PartFileTooLargeError,
    PartUploadError,
    load_part_geometry,
    parse_and_repair,
    read_part_file_atomic,
    validate_part_path,
)
from d33d.part_units import (
    axis_to_index,
    classify_stl_units,
    mm_factor_for_unit,
    settle_scale,
    settle_unit_choices,
)
from d33d.project_git import sanitize_commit_message
from d33d.versions import SOURCE_KIND_IMPORT, ImportCommitFailed

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared derive path: load → optional scale → binary STL (the part.stl
# endpoint's 3MF and scale-applied STL branches both run through this one
# function, so the guarded load and the export live in one place).
# ---------------------------------------------------------------------------


def _scaled_stl_sync(raw: bytes, fmt: str, scale: float | None) -> bytes:
    """Load the committed part bytes (shared guard: face-cap, zip-bomb),
    optionally scale by ``part_scale``, and re-export as binary STL.

    ``trimesh.Mesh.export(file_type="stl")`` returns ``bytes`` — never
    ``str`` — so the result goes straight into the response body. An
    export-stage failure re-raises as ``PartUploadError`` (the endpoint's
    409 ``source_missing`` contract covers it — never a raw 500)."""
    try:
        mesh = load_part_geometry(raw, fmt)
        if scale is not None:
            mesh = mesh.copy()
            mesh.apply_scale(scale)
        return mesh.export(file_type="stl")
    except PartUploadError:
        raise
    except Exception as e:
        raise PartUploadError(f"stl export failed: {e}") from e

# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def _run_parse_and_repair(
    content: bytes, part_format: str
) -> tuple[dict[str, Any], str | None]:
    """The upload's decode gate (``asyncio.to_thread``): ``parse_and_repair``
    → its 422 contract.

    ``RepairTimeoutError`` → the distinct ``REPAIR_TIMEOUT_DETAIL``; every
    other ``PartUploadError`` → the unparseable detail. ANY other exception
    (a MemoryError, a trimesh-internal failure) → the DISTINCT
    ``PART_UPLOAD_DECODE_FAILED_DETAIL`` (the mesh is presumed fine; the
    decode itself broke), logged with the stable prefix ``part decode
    failed unexpectedly``. The contract is total: a decode that cannot
    complete is a 422, never a raw 500. Nothing is persisted.
    """
    try:
        _mesh, report, file_unit = parse_and_repair(content, part_format)
    except PartUploadError as e:
        detail = (
            REPAIR_TIMEOUT_DETAIL
            if isinstance(e, RepairTimeoutError)
            else PART_UPLOAD_UNPARSEABLE_DETAIL
        )
        raise HTTPException(status_code=422, detail=detail) from e
    except Exception as e:
        # A non-PartUploadError (MemoryError, a trimesh-internal failure)
        # must not leak as a raw 500: the decode contract is total.
        logger.exception("part decode failed unexpectedly (%s)", type(e).__name__)
        raise HTTPException(
            status_code=422, detail=PART_UPLOAD_DECODE_FAILED_DETAIL
        ) from e
    return report, file_unit


def create_part_router() -> APIRouter:
    """The part-upload + unit-settle router (mounted by ``create_app``)."""
    router = APIRouter(prefix="/api/projects", tags=["part-import"])

    # -- POST /{project_id}/part ------------------------------------------------

    @router.post("/{project_id}/part", status_code=201)
    async def upload_part(request: Request, project_id: int) -> dict[str, Any]:
        """Multipart part upload (STL or 3MF, ≤ 50 MB).

        Gate order (the operator decision — 413 BEFORE 400/422): the body
        is streamed and accumulated up to ``MAX_PART_UPLOAD_BYTES`` + the
        1 MiB multipart allowance, then refused with a 413 (an oversize
        body is never parsed); then 400 for an unsupported content type /
        extension; then 422 unparseable. The decode (parse/repair/measure,
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
        # total exceeds the cap (the oversize body is never parsed).
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

        # Release the raw body bytes and the parsed multipart form BEFORE
        # the CPU-bound decode: only ``content`` (the extracted file field)
        # plus the decoded mesh then coexist in memory. The form is closed
        # (its UploadFile holds an open file handle) and both references
        # are dropped so the large buffers are reclaimable before the
        # parse/repair thread starts.
        try:
            await file.close()
        finally:
            form = None
            buf.clear()
            del buf, body, file

        # 422: the decode gate — parse/repair/measure, run OFF the event
        # loop (``asyncio.to_thread`` — CPU-bound; zip-bomb check in the
        # same thread). Parsed in memory — no temp file.
        report, file_unit = await asyncio.to_thread(
            _run_parse_and_repair, content, part_format
        )

        # Unit handling (STL: plausible→assumed / else unsettled options;
        # 3MF: the file's unit, converted to mm — always settled). Only an
        # ABSENT unit (``None``) is the 3MF default (mm); a non-string or
        # unconvertible unit 422s here (the conversion is the unit
        # layer's job).
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
        # + committed under the shared write lock in the SAME transaction
        # as the version row (see ``_run_import_create``) — any failure
        # (git commit, sqlite) rolls back the version row, the part
        # columns, the files, and the ``versions/`` dirs together (no
        # stuck 409 on retry).
        svc = getattr(request.app.state, "versions", None)
        if svc is None:  # pragma: no cover - the app lifespan always wires it
            raise HTTPException(status_code=500, detail="version service not wired")

        v1_name = _import_version_name(filename)
        repo_path = Path(row["git_repo_path"])
        stored_name = PART_3MF_FILENAME if part_format == "3mf" else PART_FILENAME

        async def _commit_import() -> int:
            # No project lock is held across the ``to_thread`` call above
            # (the decode finished before the lock was acquired); this
            # write-lock region covers only the version row + files + part
            # columns, in one transaction.
            return svc._run_import_create(
                project_id,
                version_name=v1_name,
                params={},
                message=f"part import: {sanitize_commit_message(filename)}" if filename else "part import",
                bbox=(tuple(mm_bbox) if mm_bbox is not None else None),
                source_kind=SOURCE_KIND_IMPORT,
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
            # Nothing persisted (the rollback rolled back the row + the
            # part columns + the files in one transaction). The detail is
            # a FIXED sentence (copy.ts partUpload.commitFailed) — the
            # exception text (paths, git output) stays in the log.
            logger.error(
                "part upload import commit failed (project_id=%s): %s",
                project_id,
                e,
            )
            raise HTTPException(
                status_code=500, detail=PART_UPLOAD_COMMIT_FAILED_DETAIL
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
        float>}`` (one measurement derives the scale, unit ``"custom"``);
        a body matching neither shape is a 422. Settling UPDATES the
        project row and the v1's mm bbox in place in ONE transaction under
        the shared write lock (never a new version), idempotent on an
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
            axis = body["axis"]
            mm = body["mm"]
            # The mm positivity check lives in ``_settle_body`` (it owns the
            # body's shape + value validation); only the axis key is
            # checked here.
            if axis not in axis_idx:
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
            # ONE transaction (under the shared write lock the version
            # writes hold). On any exception the transaction rolls back and
            # the error re-raises — neither row is left half-settled.
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
                # BaseException (not Exception): a cancellation (Keyboard
                # Interrupt / asyncio.CancelledError) mid-transaction would
                # otherwise leave the shared connection open inside the
                # transaction; the rollback is what must run there.
                conn.rollback()
                raise

        try:
            await svc._with_project_lock(project_id, lambda: _settle_sync())
        except Exception as e:
            # Fixed detail (copy.ts partUpload.commitFailed): the exception
            # text (paths, sqlite errors) stays in the server log only.
            logger.error(
                "part settle failed (project_id=%s): %s",
                project_id,
                e,
            )
            raise HTTPException(
                status_code=500, detail=PART_UPLOAD_COMMIT_FAILED_DETAIL
            ) from e

        updated = conn.get_project(project_id)
        if updated is None:
            raise HTTPException(status_code=404, detail="project not found")
        return {
            "id": project_id,
            "part": part_public(updated),
            "bbox_mm": mm_bbox,
        }

    # -- GET /{project_id}/part.stl ------------------------------------------

    @router.get("/{project_id}/part.stl", response_model=None)
    # Nominal annotation (file-wide looseness, pre-existing on the upload
    # route): the route always returns a ``Response`` on 200 — never a dict.
    async def download_part_stl(
        request: Request, project_id: int
    ) -> Response | dict[str, Any]:
        """Serve the project's committed part as binary STL, in mm.

        3MF parts are converted on the host via
        ``d33d.part_mesh.load_part_geometry`` (the shared guard: face-cap,
        zip-bomb, no new parsing path). When ``part_unit_status`` is assumed
        or settled the mesh's vertices are scaled by the project's
        ``part_scale`` (the same factor the render worker applies via
        ``scale(...) import("part.stl")``), so the served geometry is in mm;
        when the units are unsettled the file is served verbatim (file units)
        — the SPA hides the plate in that state, so the absolute scale is
        never shown alongside it.

        The 50 MB cap is checked against the file's ``fstat`` size on the
        opened fd BEFORE any read (no buffer churn on an oversize file),
        and the ETag is derived from the file's identity (``st_size`` /
        ``st_mtime_ns``) + scale — a repeat request with a matching
        If-None-Match answers a 304 with one stat and NO re-parse /
        re-scale / re-export / re-download. The read itself is the shared
        atomic O_NOFOLLOW pattern (``part_mesh.read_part_file_atomic``), so
        a symlink swapped in after the lstat-based validation cannot
        redirect it outside the repo (the render-staging standard).

        Response codes:
          - 404: the project does not exist, or has no part
          - 409: the repo or committed file is missing (the ``source_missing``
            shape, reusing the #295 error class)
          - 200: ``Response`` with binary STL bytes, ``model/stl`` content
            type, and a body size cap (50 MB — the same bound as upload).
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        if not row.get("part_filename"):
            raise HTTPException(status_code=404, detail="project has no part")

        part_path, repo_dir = resolve_v1_part_path(row, conn)
        if part_path is None:
            # No part, no repo path, or no v1 row — nothing on disk to
            # serve (the shared resolver is the one place the in-repo
            # layout is constructed; the unsettled part's committed file
            # resolves through the same layout, served verbatim below).
            raise HTTPException(
                status_code=409,
                detail={"code": "source_missing", "message": "the part file is not on disk"},
            )

        # Containment + symlink + name validation (the operator decision:
        # "Reuse the render-staging validation" — the shared helper both
        # paths import from ``part_mesh``, so the rule can't drift).
        validation_err = validate_part_path(part_path, repo_dir)
        if validation_err is not None:
            logger.error("part.stl validation failed (project_id=%s): %s", project_id, validation_err)
            raise HTTPException(
                status_code=409,
                detail={"code": "source_missing", "message": "the part file is not on disk"},
            )

        # The served bytes are deterministic in the file's identity + scale,
        # so the tag is derived from st_size + st_mtime_ns (one stat) and
        # checked BEFORE any read: an unchanged part's 304 costs that stat
        # alone — no parse / scale / export / hash of the payload.
        part_format = row.get("part_format") or "stl"
        scale: float | None = None
        if row.get("part_unit_status") in ("assumed", "settled"):
            candidate = row.get("part_scale")
            if isinstance(candidate, (int, float)) and not isinstance(candidate, bool) and candidate > 0:
                scale = float(candidate)
        try:
            st = part_path.stat()
        except OSError as e:
            logger.error("part.stl stat failed (project_id=%s): %s", project_id, e)
            raise HTTPException(
                status_code=409,
                detail={"code": "source_missing", "message": "the part file is not on disk"},
            ) from e
        etag = hashlib.sha1(
            str(st.st_size).encode("ascii")
            + b":"
            + str(st.st_mtime_ns).encode("ascii")
            + b":"
            + repr(scale).encode("ascii")
        ).hexdigest()
        etag_header = '"' + etag + '"'
        # A conditional GET (If-None-Match carrying this tag, optionally
        # comma-listed) answers 304 with no body before any read —
        # unchanged bytes cost a header round trip.
        if request.headers.get("if-none-match") is not None:
            candidates = {
                c.strip() for c in request.headers["if-none-match"].split(",")
            }
            if etag_header in candidates or "*" in candidates:
                return Response(status_code=304, headers={"ETag": etag_header})

        # Size cap on the fd's fstat + the atomic no-symlink read (the
        # render-staging standard, shared: ``read_part_file_atomic``). The
        # SPA loads this via the STL loader, so the cap must hold for a
        # hand-committed file too, not just uploads.
        try:
            raw = read_part_file_atomic(part_path, MAX_PART_UPLOAD_BYTES)
        except PartFileTooLargeError as e:
            logger.error("part.stl over cap (project_id=%s): %s", project_id, e)
            raise HTTPException(
                status_code=413,
                detail=f"part file exceeds {MAX_PART_UPLOAD_BYTES} byte limit",
            ) from e
        except OSError as e:
            logger.error("part.stl read failed (project_id=%s): %s", project_id, e)
            raise HTTPException(
                status_code=409,
                detail={"code": "source_missing", "message": "the part file is not on disk"},
            ) from e

        # Unit scaling (the operator decision): assumed or settled parts
        # are served with ``part_scale`` applied — the geometry IS in mm, so
        # the viewer renders it with the build plate and the report's mm
        # numbers agree. Unsettled (or a missing/invalid scale) serves the
        # file verbatim — the SPA hides the plate while unsettled.
        def _scaled_stl(raw: bytes, fmt: str) -> bytes:
            return _scaled_stl_sync(raw, fmt, scale)

        stl_bytes: bytes
        if part_format == "3mf":
            # Convert 3MF → STL on the host (shared load path, no new parser).
            try:
                stl_bytes = await asyncio.to_thread(_scaled_stl, raw, "3mf")
            except PartUploadError as e:
                logger.error("part.stl 3MF conversion failed (project_id=%s): %s", project_id, e)
                raise HTTPException(
                    status_code=409,
                    detail={"code": "source_missing", "message": "the part file could not be read"},
                ) from e
            if not stl_bytes:
                raise HTTPException(
                    status_code=409,
                    detail={"code": "source_missing", "message": "the part file is empty"},
                )
        elif scale is not None:
            # STL with a scale: re-derive through the shared guarded loader
            # (the face-cap + zip-bomb guards apply to a hand-committed file
            # as well — this path does not pass through the upload guards).
            try:
                stl_bytes = await asyncio.to_thread(_scaled_stl, raw, "stl")
            except PartUploadError as e:
                logger.error("part.stl scaling failed (project_id=%s): %s", project_id, e)
                raise HTTPException(
                    status_code=409,
                    detail={"code": "source_missing", "message": "the part file could not be read"},
                ) from e
        else:
            # Unsettled STL: serve the committed bytes verbatim (file units —
            # the SPA hides the plate, so no scale is ever shown alongside it).
            stl_bytes = raw

        return Response(
            content=stl_bytes,
            media_type="model/stl",
            headers={
                "ETag": etag_header,
                "Content-Disposition": 'attachment; filename="part.stl"',
            },
        )

    return router


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
    "PART_UPLOAD_COMMIT_FAILED_DETAIL",
    "PART_UPLOAD_DECODE_FAILED_DETAIL",
    "PART_UPLOAD_SETTLE_INVALID_DETAIL",
    "PART_UPLOAD_UNPARSEABLE_DETAIL",
    "PART_UPLOAD_UNSUPPORTED_DETAIL",
    "classify_stl_units",
    "create_part_router",
    "part_public",
]
