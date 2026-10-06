"""Part mesh decode: parse / repair / measure for imported STL/3MF (issue #325).

The CPU-bound half of the part import: ``parse_and_repair`` loads the
uploaded bytes in-process with trimesh (no shell — the bytes are never
written to a path carrying the user's filename), applies the
face-cap and finiteness gates, runs the repair chain (merge_vertices +
pymeshfix + fix_normals — NO decimation of the user's part below the
budget), and measures.

Issue #395: the repair chain now skips pymeshfix entirely when the mesh is
topologically clean (0 boundary loops, all bodies watertight, winding
consistent) — the stored mesh and report come from the merged mesh directly.
Above ``REPAIR_FACE_BUDGET`` the mesh is decimated BEFORE repair (ticket #3:
decimate before, fix_normals after). The repair itself runs in a separate
process with a timeout (see ``part_repair.REPAIR_TIMEOUT_SECONDS``).

The 3MF (a ZIP) zip-bomb guard runs from the central directory BEFORE
extraction (a bomb checked after extraction has already decompressed).
Non-finite vertices are rejected before any extent math and again after
pymeshfix (which can emit NaN). Run it OFF the event loop: the route calls
it via ``asyncio.to_thread`` — a mesh this size takes seconds to parse.
"""

from __future__ import annotations

import io
import logging
import math
import os
import stat
import zipfile
from typing import Any

import numpy as np
import trimesh

from d33d.part_errors import PartUploadError  # re-exported for #395 compat
from d33d.part_holes import _boundary_loops, measure_holes
from d33d.part_mesh_topology import MeshTopology, mesh_topology
from d33d.part_repair import (
    REPAIR_FACE_BUDGET,
    _decimate,
    repair_bodies_with_pmf,
    repair_with_pmf,
)

logger = logging.getLogger(__name__)

#: The named face cap for an import (a distinct constant from
#: ``print_validation.MAX_FACES`` — that bound belongs to the render
#: pipeline's gate 5 and must not be conflated with the import's parse-cost
#: bound). Checked right after ``trimesh.load``, on the total across ALL
#: geometries of the loaded scene, before repair.
MAX_PART_FACES = 2_000_000


#: 3MF zip-bomb guards (checked from the ZIP central directory BEFORE any
#: extraction — a bomb checked after extraction has already decompressed).
MAX_PART_ZIP_ENTRIES = 10_000
#: 3MF zip-bomb guard — deliberately distinct from the RAW-FILE size bound
#: (``part_http.MAX_PART_UPLOAD_BYTES`` is a single named constant shared
#: by the upload route and the render staging; this one bounds the
#: declared-uncompressed total of the 3MF's zip entries).
MAX_PART_ZIP_UNCOMPRESSED = 50 * 1024 * 1024  # 50 MB


def validate_part_path(part_path, repo_dir) -> str | None:
    """Validate the part path for containment and name. Returns an error
    message string on failure, or ``None`` when all checks pass.

    The SHARED containment check the render staging (``d33d.render_worker``)
    and the part.stl endpoint both use (the operator decision: "reuse the
    render-staging validation"), so the rule lives in ONE module — the
    worker and the API route both import from here, never from each other.

    Checks (all must pass):
    1. ``part_path`` must be a regular file (a missing path is rejected
       here — the lstat that proves it is a regular file doubles as the
       symlink check: a symlink lstat reports S_IFLNK, never the file mode).
    2. ``part_path`` itself must not be a symlink.
    3. ``part_path.resolve()`` must be inside ``repo_dir.resolve()``.
    4. No path component between ``repo_dir`` and ``part_path`` may be a
       symlink resolving outside ``repo_dir``.
    5. ``part_path``'s name must be exactly ``part.stl`` or ``part.3mf``.
    """
    # Name check (exact, case-sensitive).
    if part_path.name not in ("part.stl", "part.3mf"):
        return f"part filename must be exactly 'part.stl' or 'part.3mf', got '{part_path.name}'"

    # Regular-file + symlink check on the part itself (lstat — does not
    # follow links; a symlink reports S_IFLNK here, never a file mode, so
    # one lstat covers both the "regular file" and "not a symlink" rules).
    try:
        st = part_path.lstat()
    except OSError as e:
        return f"part path does not exist or is inaccessible: {e}"
    if not stat.S_ISREG(st.st_mode):
        if stat.S_ISLNK(st.st_mode):
            return f"part path is a symlink: {part_path.name}"
        return f"part path is not a regular file: {part_path.name}"

    # Containment: resolved part must be inside resolved repo_dir.
    try:
        resolved_repo = repo_dir.resolve()
        resolved_part = part_path.resolve()
    except OSError as e:
        return f"cannot resolve part or repo path: {e}"
    if not resolved_part.is_relative_to(resolved_repo):
        return "part path is outside the project repo directory"

    # Symlink component check: walk each path component of part_path BELOW
    # repo_dir on the RAW (unresolved) path.
    repo_depth = len(repo_dir.parts)
    raw = part_path if part_path.is_absolute() else repo_dir / part_path
    for i in range(1, len(raw.parts)):
        if i < repo_depth and raw.is_absolute():
            continue
        component = raw.joinpath(*raw.parts[:i])
        try:
            if component.is_symlink():
                target = component.resolve()
                if not target.is_relative_to(resolved_repo):
                    return (
                        f"path component {component.name} is a symlink "
                        "escaping the repo"
                    )
        except OSError:
            return f"path component {component.name} is inaccessible"

    return None


class PartFileTooLargeError(OSError):
    """The committed part file exceeds the size cap (``max_bytes``).

    A distinct ``OSError`` subtype so callers (the part.stl endpoint's
    413 mapping, the render worker's staging) can map by TYPE instead of
    string-matching the message. The message is informative but never
    parsed. Carries ``errno = 28`` (EDQUOT)."""


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
    from the ZIP central directory, BEFORE any extraction. Raises
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


def _assert_face_cap(loaded: Any) -> None:
    """The ``MAX_PART_FACES`` gate, shared by the upload's
    ``parse_and_repair`` and the render's ``load_part_geometry``."""
    total_faces = _total_faces(loaded)
    if total_faces > MAX_PART_FACES:
        raise PartUploadError(
            f"mesh has {total_faces} faces; max {MAX_PART_FACES}"
        )


def load_part_geometry(data: bytes, part_format: str) -> trimesh.Trimesh:
    """Guarded load + flatten for the RENDER staging path (issue #330)."""
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

    _assert_face_cap(loaded)

    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise PartUploadError("3MF has no geometry")
        return loaded.to_mesh()
    return loaded


def read_part_file_atomic(part_path, max_bytes: int) -> bytes:
    """Read the validated committed part file via an O_NOFOLLOW fd."""
    fd = os.open(str(part_path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"part is not a regular file at read time: {part_path.name}")
        if st.st_size > max_bytes:
            raise PartFileTooLargeError(
                28,
                f"part file is {st.st_size} bytes; max {max_bytes}",
                str(part_path),
            )
        return os.read(fd, st.st_size) if st.st_size > 0 else b""
    finally:
        os.close(fd)


def _is_clean(topo: MeshTopology) -> bool:
    """The clean-mesh predicate: 0 boundary loops, all bodies watertight,
    winding consistent. A genus ≥ 1 mesh (ring, torus) is ALSO clean —
    holes are the part's intent, not defects. A non-watertight debris
    shell makes the mesh NOT clean (bodies > watertight_bodies). A UNKNOWN
    boundary-loop count (``-1``) makes the mesh NOT clean — a failed count
    must never be treated as 0 (which would wrongly skip repair)."""
    return (
        topo["boundary_loops"] == 0
        and topo["watertight_bodies"] == topo["bodies"]
        and topo["winding_consistent"]
    )



def parse_and_repair(
    data: bytes, part_format: str
) -> tuple[trimesh.Trimesh, dict[str, Any], str | None]:
    """Parse ``data`` (STL or 3MF), apply the face cap + finiteness gates,
    run the repair chain (merge_vertices + pymeshfix + fix_normals —
    decimation BEFORE repair when above ``REPAIR_FACE_BUDGET``), and
    measure.

    Issue #395: if the merged mesh is topologically clean (0 boundary loops,
    all bodies watertight, winding consistent) repair is SKIPPED entirely —
    the stored mesh and report come from the merged mesh directly. Above
    ``REPAIR_FACE_BUDGET`` the mesh is decimated first (ticket #3).

    Returns ``(mesh, report, file_unit)`` where ``mesh`` is the repaired
    (or, for a clean mesh, the merged) mesh in FILE units, ``report`` is
    the repair report (``{triangles, bodies, watertight, gaps_closed,
    bbox_file_units, hole_count}``, plus the OPTIONAL ``bodies_before``),
    and ``file_unit`` is the 3MF's declared unit string.

    Repair is per-body for multi-body imports: ``bodies`` counts the
    pre-repair watertight components. A multi-body import repairs EACH
    watertight component separately and concatenates (issue #375). The
    clean-skip predicate applies per-body in the multi-body branch.

    ``hole_count`` is ``gaps_before + genus`` — the PRE-REPAIR merged mesh's
    boundary loops plus the closed-body genus. Computed on the merged mesh
    BEFORE any repair or decimation.

    Raises ``PartUploadError`` (the 422) on any failure.
    """
    # Load via a file object with an EXPLICIT loader.
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

    _assert_face_cap(loaded)

    # Flatten to a single mesh.
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise PartUploadError("3MF has no geometry")
        merged = loaded.to_mesh()
    else:
        merged = loaded
    if len(merged.faces) == 0:
        raise PartUploadError("mesh is empty")

    if not _is_finite(merged):
        raise PartUploadError("mesh has non-finite vertices")

    # ONE split serves both the bodies count and the genus.
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    components = merged.split(only_watertight=False, repair=False)
    watertight_bodies = [c for c in components if c.is_watertight]
    bodies_before = len(watertight_bodies)
    if len(merged.faces) == 0:
        raise PartUploadError("mesh is empty after cleanup")

    # The bbox in FILE units, BEFORE repair.
    file_bbox = tuple(float(e) for e in merged.extents)
    if not all(math.isfinite(e) for e in file_bbox) or max(file_bbox) <= 0:
        raise PartUploadError("mesh has invalid extents")

    # The 3MF's declared unit.
    file_unit: str | None = None
    if part_format == "3mf":
        file_unit = mesh_units(
            next(iter(loaded.geometry.values()))
            if isinstance(loaded, trimesh.Scene)
            else loaded
        )

    # Measure topology on the merged (pre-repair) mesh.
    topo = mesh_topology(merged, components)
    # A UNKNOWN boundary-loop count (``-1``) degrades to 0 gaps for the
    # hole count (as the genus fallback does) — never negative, never a crash.
    gaps_before = max(topo["boundary_loops"], 0)

    # hole_count: gaps_before + genus (both from the pre-repair mesh).
    holes = gaps_before + topo["genus"]

    # The clean-skip predicate: if the mesh is clean, skip repair entirely.
    if _is_clean(topo):
        # Clean mesh: skip repair. Decimate only if above the budget
        # (render safety — a clean mesh below the budget is stored as-is).
        if len(merged.faces) > REPAIR_FACE_BUDGET:
            repaired = _decimate(merged, REPAIR_FACE_BUDGET)
        else:
            repaired = merged
    elif bodies_before <= 1:
        # Single body, not clean: the unchanged one-call pymeshfix path,
        # with decimation BEFORE repair if above the budget (ticket #3).
        repair_input = merged
        if len(merged.faces) > REPAIR_FACE_BUDGET:
            repair_input = _decimate(merged, REPAIR_FACE_BUDGET)
        repaired = repair_with_pmf(repair_input)
    else:
        # Multi-body, not clean: repair EACH watertight body separately
        # and concatenate (issue #375). Decimate each body before repair
        # if above the budget. The whole call is bounded by the aggregate
        # wall-clock budget REPAIR_TIMEOUT_SECONDS (the part_repair
        # default — the total multi-body repair must not exceed the same
        # budget as a single body).
        repaired_bodies = []
        for body in watertight_bodies:
            if len(body.faces) > REPAIR_FACE_BUDGET:
                body = _decimate(body, REPAIR_FACE_BUDGET)
            repaired_bodies.append(body)
        repaired = trimesh.util.concatenate(
            repair_bodies_with_pmf(repaired_bodies)
        )

    # Finiteness AGAIN after pymeshfix (it can emit NaN from degenerate
    # input).
    if not _is_finite(repaired):
        raise PartUploadError("mesh has non-finite vertices after repair")
    if len(repaired.faces) == 0:
        raise PartUploadError("mesh is empty after repair")

    # Recount bodies on the stored (post-repair) mesh.
    repaired.merge_vertices()
    repaired.update_faces(repaired.nondegenerate_faces())
    comps = repaired.split(only_watertight=False, repair=False)
    watertight = [c for c in comps if c.is_watertight]
    bodies = len(watertight)
    gaps_after = _boundary_loops(repaired)

    report: dict[str, Any] = {
        "triangles": len(repaired.faces),
        "bodies": bodies,
        "watertight": bool(repaired.is_watertight),
        "gaps_closed": max(0, gaps_before - gaps_after),
        "hole_count": int(holes),
        "bbox_file_units": file_bbox,
    }
    if bodies < bodies_before:
        report["bodies_before"] = bodies_before

    # Issue #396: measure per-hole geometry (centre, axis, diameter) on
    # the PRE-REPAIR merged mesh. The measurement is in FILE units — the
    # same space as ``bbox_file_units`` — and is applied the part's scale
    # is NOT, so the stored ``holes`` and the stored ``bbox_file_units``
    # are directly comparable (the reader multiplies both by the same
    # ``part_scale`` to get mm). Unfittable holes are omitted (omit-not-
    # null: hole_count stays honest, the holes list is a subset). The
    # BROAD ``except Exception`` is deliberate and required: hole
    # measurement is an optional enrichment, and the import must never
    # fail because a hole was hard to measure (the ``never fail the
    # import`` contract). Any exception class — ``RuntimeError`` from a
    # trimesh/numpy C path, ``MemoryError`` on a huge body — is logged
    # with a full traceback and the holes list is simply omitted (a bare
    # narrow except would let the other classes propagate out of
    # ``parse_and_repair`` and fail the upload).
    #
    # Issue #396 lens fix: a missing ``scipy`` or ``shapely`` (the hard
    # runtime dependencies for ``trimesh.Trimesh.section``) is a
    # DEPLOYMENT ERROR, not a geometric failure — it must FAIL LOUDLY
    # (propagate the ``ImportError`` out of ``parse_and_repair``), not
    # silently omit the holes list. The ``except ImportError: raise``
    # BEFORE the broad ``except Exception`` ensures a missing dependency
    # is never swallowed; the broad guard still catches every other
    # exception class so the import never fails for a geometric reason.
    # (A missing scipy/shapely means the section cannot run at all —
    # the upload fails loudly with an ``ImportError`` rather than
    # silently omitting the holes list, which would mask the broken
    # environment.)
    try:
        measured_holes = measure_holes(merged, components)
        if measured_holes:
            report["holes"] = measured_holes
    except ImportError:
        # A missing scipy or shapely is a deployment error, not a
        # geometric failure — propagate loudly (the upload fails with
        # an ``ImportError``; the operator sees the missing dependency
        # rather than a silently-omitted holes list).
        raise
    except Exception:
        logger.exception("hole measurement failed, omitting holes list")

    return repaired, report, file_unit


__all__ = [
    "MAX_PART_FACES",
    "MAX_PART_ZIP_ENTRIES",
    "MAX_PART_ZIP_UNCOMPRESSED",
    "REPAIR_FACE_BUDGET",
    "PartFileTooLargeError",
    "PartUploadError",
    "_decimate",
    "load_part_geometry",
    "mesh_units",
    "parse_and_repair",
    "read_part_file_atomic",
    "repair_with_pmf",
    "validate_part_path",
]
