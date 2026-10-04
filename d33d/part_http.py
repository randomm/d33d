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
PART_UPLOAD_COMMIT_FAILED_DETAIL = "The part couldn't be saved. Nothing was changed."
PART_EXISTS_DETAIL = "This project already has a part."


# ---------------------------------------------------------------------------
# Part persistence (project columns + the v1 version row)
# ---------------------------------------------------------------------------


def part_envelope(row: dict[str, Any] | None) -> dict[str, Any] | None:
    """The design-loop's part facts for ONE project row (issue #332, sub-issue 3).

    The single source both loop seams read (``d33d.design_loop_events``'s
    chat adapter and ``d33d.versions_routes``'s finalize ``_finalize_loop_
    kwargs``) — never two divergent copies. ``None`` (no part row, or a
    part whose units are NOT assumed/settled — an unsettled part never
    runs a loop, and a part-less project keeps today's prompt byte-
    verbatim) renders nothing; a positive ``scale`` returns
    ``{"scale": part_scale, "bbox_mm": <the v1's measured mm extents>,
    ...}`` — the loop's ``part_scale`` (the prompt's ``scale(...)`` and
    the import guard's settled factor) and ``part_bbox_mm`` (the bbox
    gate's ground-truth baseline, the v1 row's persisted bbox — file bbox
    × scale as recorded at import/settle — read from the V1 row, never
    from the latest version's). A part with a positive scale whose v1 row
    carries no bbox (a measurement the write path could not obtain)
    degrades ``bbox_mm`` to ``None`` (the gate abstains on the part's
    baseline — the stated dims still apply).
    """
    if row is None or not row.get("part_filename"):
        return None
    status = row.get("part_unit_status")
    if status not in ("assumed", "settled"):
        return None
    scale = row.get("part_scale")
    if not isinstance(scale, (int, float)) or isinstance(scale, bool) or scale <= 0:
        return None
    return {"scale": float(scale), "bbox_mm": None, "filename": row.get("part_filename")}


def part_envelope_with_bbox(
    row: dict[str, Any] | None,
    conn: db_mod.Connection | None,
    versions: Any = None,  # legacy seam parameter (the v1 row is read directly now)
) -> dict[str, Any] | None:
    """:func:`part_envelope` with the v1's measured mm bbox resolved (issue #332, sub-issue 3).

    The seams that have the project row AND the app's connection use this
    variant: the v1 row's persisted ``bbox`` (the file bbox × scale, as
    recorded at import/settle) fills ``bbox_mm`` — the bbox gate's ground-
    truth baseline. The v1 row is read explicitly through
    :func:`_v1_for_part` (the import's own row — NOT ``versions.
    latest_version``: once a v2+ exists, the latest row's bbox is the
    previous candidate's own extents, not the import's) and its ``bbox``
    column is the RAW sqlite JSON string, decoded here. ``None`` row /
    part / unsettled / non-positive scale returns ``None`` (the no-part
    and unsettled regressions — byte-identical prompt, no loop, never an
    error). A part with a positive scale whose v1 row is missing or
    carries no bbox degrades ``bbox_mm`` to ``None`` (the gate abstains
    on the part's baseline; the stated dims still apply — never a
    fabricated measurement).
    """
    env = part_envelope(row)
    if env is None or conn is None:
        return env
    # The ground-truth baseline is the V1 row's bbox — read the v1 row
    # explicitly (the import's own measurement). ``versions.
    # latest_version`` would be WRONG here: once the user adds a feature
    # (v2+), the latest row's recorded bbox is the previous candidate's
    # own extents, and the gate's baseline would silently become the
    # candidate it is supposed to check (issue #332 fix round).
    v1 = _v1_for_part(conn, row.get("id"))
    if v1 is None:
        return env
    # The v1 row comes back from sqlite raw: ``bbox`` is a JSON string
    # (a dict if the row ever arrives pre-decoded). Decode exactly once.
    raw = v1.get("bbox")
    if raw is None:
        return env
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return env
    if not isinstance(raw, dict):
        return env
    axes = [raw.get(name) for name in ("x", "y", "z")]
    if not all(
        isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 for v in axes
    ):
        return env
    env = dict(env)
    env["bbox_mm"] = (float(axes[0]), float(axes[1]), float(axes[2]))
    return env


def part_bbox_mm(
    row: dict[str, Any] | None, conn: db_mod.Connection | None
) -> list[float] | None:
    """The project's imported part's measured mm bbox as ``[w, d, h]``
    (issue #338, decision 2) — the design-state part block's ``bbox_mm``.

    Reuses :func:`part_envelope_with_bbox`'s v1-read/decode and its guards
    (the v1 row is the import's own measurement, read through
    :func:`_v1_for_part`; its raw sqlite JSON string is decoded; every axis
    must be a finite positive number — a zero, negative, missing, or
    non-numeric axis degrades to ``None``). ``None`` while the part is
    unsettled (a file-unit bbox is not a meaningful mm measurement until
    the unit is settled — the Brief shows "waiting on units", never a
    confident number), and ``None`` when the project has no part. An
    "assumed" part IS a valid mm measurement (issue #350: an assumed STL
    is read as mm — a positive scale and a v1 mm bbox written at import),
    so its ``bbox_mm`` is the ``[w, d, h]`` mm list, not ``None``.
    """
    if row is None or conn is None or not row.get("part_filename"):
        return None
    if row.get("part_unit_status") not in ("assumed", "settled"):
        return None
    env = part_envelope_with_bbox(row, conn)
    if env is None or env["bbox_mm"] is None:
        return None
    w, d, h = env["bbox_mm"]
    return [float(w), float(d), float(h)]


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
        "report": _loads_or_none(report),
        "options": _loads_or_none(options),
    }


def _loads_or_none(blob: str | None) -> Any | None:
    """Decode a stored JSON column, degrading a CORRUPT (unparseable) blob
    to ``None`` instead of raising (issue #351): a corrupt ``part_report``
    is no evidence either way — every reader (the fill-and-recut hole
    gate included) must degrade to the unknown behaviour, never a 500.
    Valid JSON (including ``null``) decodes normally; ``None``/empty
    stays ``None``."""
    if not blob:
        return None
    try:
        return json.loads(blob)
    except (ValueError, TypeError):
        return None


def resolve_part_paths(
    row: dict[str, Any] | None, conn: db_mod.Connection | None
) -> tuple[Path | None, Path | None]:
    """The design-side part's ``(part_path, repo_dir)`` wiring (issue #330)
    — the one decision both production render closures (``d33d.app``'s
    ``_loop`` and ``d33d.versions_routes``'s finalize ``_render_fn``) used
    to duplicate. ``row`` is the ALREADY-ACQUIRED project row (``None``
    when the caller could not read it — the caller owns the acquisition and
    the unreadable-row WARNING). The part is wired ONLY when the project
    has a part whose units are settled or assumed (``part_unit_status`` in
    ``{"assumed", "settled"}``): ``part_path`` is the committed part file
    (``{git_repo_path}/versions/{v1}/{part.stl|part.3mf}`` — the v1 version
    row via ``_v1_for_part``; the stored name is the ``part_format``'s fixed
    constant, never re-derived) and ``repo_dir`` is the git repo path (the
    worker's containment boundary). No part, unsettled units, a missing v1
    row, or no repo path degrade ``part_path`` to ``None`` — ``repo_dir``
    is still set (``None`` only for a missing row or repo path). The
    caller does NOT scale the part (``part_scale`` is sub-issue 3's
    domain, not the worker's).
    """
    repo_dir: Path | None = (
        Path(row["git_repo_path"]) if row is not None and row.get("git_repo_path") else None
    )
    if (
        row is None
        or conn is None
        or not row.get("part_filename")
        or row.get("part_unit_status") not in ("assumed", "settled")
        or not row.get("git_repo_path")
    ):
        return None, repo_dir
    v1 = _v1_for_part(conn, row["id"])
    if v1 is None:
        return None, repo_dir
    return _v1_part_path(repo_dir, v1, row), repo_dir


def resolve_v1_part_path(
    row: dict[str, Any] | None, conn: db_mod.Connection | None
) -> tuple[Path | None, Path | None]:
    """resolve_part_paths`` minus the unit-status gate — the ONE place the
    ``{repo}/versions/{v1}/{part.stl|part.3mf}`` layout is constructed, so a
    layout change lands in one function no matter who derives it.

    ``row`` is the ALREADY-ACQUIRED project row (``None`` → ``(None, None)``).
    A project with no part, no repo path, or a missing v1 row degrades
    ``part_path`` to ``None`` (``repo_dir`` still set); an UNSETTLED part's
    committed file still resolves (the part.stl endpoint serves it verbatim
    — the render path's settled/assumed-only gate is ``resolve_part_paths``,
    not this)."""
    repo_dir: Path | None = (
        Path(row["git_repo_path"])
        if row is not None and row.get("git_repo_path")
        else None
    )
    if row is None or conn is None or not row.get("part_filename") or repo_dir is None:
        return None, repo_dir
    v1 = _v1_for_part(conn, row["id"])
    if v1 is None:
        return None, repo_dir
    return _v1_part_path(repo_dir, v1, row), repo_dir


def _v1_part_path(repo_dir: Path, v1: dict[str, Any], row: dict[str, Any]) -> Path:
    """The committed part file's in-repo path for the project's v1 row.

    The fixed layout — ``{repo}/versions/{v1_id}/{part.stl|part.3mf}`` — the
    stored name is the ``part_format``'s fixed constant, never re-derived.
    Shared by ``resolve_part_paths`` and ``resolve_v1_part_path``."""
    name = PART_3MF_FILENAME if row.get("part_format") == "3mf" else PART_FILENAME
    return repo_dir / "versions" / str(v1["id"]) / name


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
    "PART_UPLOAD_COMMIT_FAILED_DETAIL",
    "PART_UPLOAD_SETTLE_INVALID_DETAIL",
    "PART_UPLOAD_UNPARSEABLE_DETAIL",
    "PART_UPLOAD_UNSUPPORTED_DETAIL",
    "_detect_part_format",
    "_import_version_name",
    "_is_positive_number",
    "_parse_multipart",
    "_v1_for_part",
    "part_bbox_mm",
    "part_envelope",
    "part_envelope_with_bbox",
    "part_public",
    "resolve_part_paths",
    "resolve_v1_part_path",
]
