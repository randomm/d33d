"""Part-upload HTTP machinery (issue #325).

The transport half of the part-import route (the router and the unit math
live in ``d33d.part_import`` / ``d33d.part_units``): the upload bounds,
the copy.ts-verbatim detail strings, the multipart parsing, the
content-type/extension format detection, the v1 display name, and the
part-columns → public object.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any

from fastapi import Request

from d33d import db as db_mod

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
# Multipart + format detection (the 413 already fired on the raw bytes —
# parsing and detection never re-read the stream)
# ---------------------------------------------------------------------------


async def _parse_multipart(request: Request, body: bytes) -> dict[str, Any]:
    """Parse an already-streamed multipart body into form fields."""
    from starlette.datastructures import Headers
    from starlette.formparsers import MultiPartParser

    async def _chunked(data: bytes):
        yield data

    headers = Headers(
        raw=[(k.lower().encode(), v.encode()) for k, v in request.headers.items()]
    )
    parsed = MultiPartParser(headers, _chunked(body))
    return await parsed.parse()


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


__all__ = [
    "MAX_PART_UPLOAD_BYTES",
    "PART_3MF_FILENAME",
    "PART_EXISTS_DETAIL",
    "PART_FILENAME",
    "PART_UPLOAD_SETTLE_INVALID_DETAIL",
    "PART_UPLOAD_UNPARSEABLE_DETAIL",
    "PART_UPLOAD_UNSUPPORTED_DETAIL",
    "_detect_part_format",
    "_import_version_name",
    "_is_positive_number",
    "_parse_multipart",
    "_v1_for_part",
    "part_public",
]
