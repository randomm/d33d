"""Part mesh decode: parse / repair / measure for imported STL/3MF (issue #325).

The CPU-bound half of the part import: ``parse_and_repair`` loads the
uploaded bytes in-process with trimesh (no shell, no subprocess — the bytes
are never written to a path carrying the user's filename), applies the
face-cap and finiteness gates, runs the repair chain (merge_vertices +
pymeshfix + fix_normals — NO decimation of the user's part), and measures.

The 3MF (a ZIP) zip-bomb guard runs from the central directory BEFORE
extraction (entry count + declared-uncompressed total) — a bomb checked
after extraction has already decompressed. Non-finite vertices are
rejected both before any extent math and again after pymeshfix (which can
emit NaN).

Run it OFF the event loop: the route calls it via ``asyncio.to_thread`` —
a mesh this size takes seconds to parse, and the upload route must not
stall the app's other requests while it does.
"""

from __future__ import annotations

import io
import math
import zipfile
from typing import Any

import numpy as np
import trimesh

#: The named face cap for an import (a distinct constant from
#: ``print_validation.MAX_FACES`` — that bound belongs to the render
#: pipeline's gate 5 and must not be conflated with the import's parse-cost
#: bound). Checked right after ``trimesh.load``, on the total across ALL
#: geometries of the loaded scene, before repair.
MAX_PART_FACES = 2_000_000

#: 3MF zip-bomb guards (checked from the ZIP central directory BEFORE any
#: extraction — a bomb checked after extraction has already decompressed).
MAX_PART_ZIP_ENTRIES = 10_000
MAX_PART_ZIP_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB (MAX_PART_UPLOAD_BYTES)


class PartUploadError(ValueError):
    """A part upload failed the decode gate (unparseable, empty,
    non-finite, over the face cap, zip-bomb, or an unconvertible 3MF
    unit). The route maps it to the 422 with the verbatim
    ``partUpload.unparseable`` detail — nothing is persisted on the way
    out."""


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
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
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


def mesh_units(mesh: Any) -> str | None:
    """The mesh's declared unit as carried by trimesh (``None`` when it is
    absent — an STL is unitless, a 3MF without a ``unit`` attribute loads
    with no string unit).

    ``None`` is the ONLY value that means "no unit declared" (the 3MF
    default is millimeters); any other non-string value is a failure the
    caller must 422 — never a silent mm assumption."""
    units = getattr(mesh, "units", None)
    if units is None:
        return None
    if isinstance(units, str) and units:
        return units
    raise PartUploadError(f"mesh has a non-string unit {units!r}")


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
    and ``file_unit`` is the 3MF's declared unit string read from the
    ORIGINAL (pre-repair) loaded geometry (``None`` for STL — unitless, or
    a 3MF with no ``unit`` attribute — the 3MF default is millimeters).

    Raises ``PartUploadError`` (the 422) on any failure: unparseable,
    empty, over the face cap, non-finite (pre- or post-repair), or a 3MF
    unit that is not a string (or a string outside the mm/cm/inch
    synonym sets).
    """
    # Load via a file object with an EXPLICIT loader (``file_type``) — the
    # bytes are never written to a path carrying the user's filename, and
    # the loader is chosen by the DETECTED format, not a filename suffix.
    try:
        if part_format == "3mf":
            _check_zip_bomb(data)
            loaded = trimesh.load(io.BytesIO(data), file_type="3mf")
        else:
            loaded = trimesh.load(io.BytesIO(data), file_type="stl")
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

    # The 3MF's declared unit, read from the ORIGINAL (pre-repair)
    # geometry — pymeshfix does not change units, but reading it from the
    # original load is the only place the ``unit`` attribute is guaranteed
    # to be present. Only an ABSENT unit (``None``) means the 3MF default
    # (millimeters); a non-string unit, or a string outside the closed
    # mm/cm/inch synonym sets, is a 422 — never a silent mm assumption.
    file_unit: str | None = None
    if part_format == "3mf":
        file_unit = mesh_units(
            next(iter(loaded.geometry.values()))
            if isinstance(loaded, trimesh.Scene)
            else loaded
        )

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

    report = {
        "triangles": len(repaired.faces),
        "bodies": bodies,
        "watertight": bool(repaired.is_watertight),
        "gaps_closed": max(0, gaps_before - gaps_after),
        "bbox_file_units": file_bbox,
    }
    return repaired, report, file_unit


__all__ = [
    "MAX_PART_FACES",
    "MAX_PART_ZIP_ENTRIES",
    "MAX_PART_ZIP_UNCOMPRESSED",
    "PartUploadError",
    "mesh_units",
    "parse_and_repair",
]
