"""Part mesh decode: parse / repair / measure for imported STL/3MF (issue #325).

The CPU-bound half of the part import: ``parse_and_repair`` loads the
uploaded bytes in-process with trimesh (no shell, no subprocess — the bytes
are never written to a path carrying the user's filename), applies the
face-cap and finiteness gates, runs the repair chain (merge_vertices +
pymeshfix + fix_normals — NO decimation of the user's part), and measures.

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

from d33d.part_holes import _boundary_loops, watertight_genus
from d33d.part_repair import repair_with_pmf

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
        # Covers symlink (S_IFLNK), directory, and special files in one
        # check — a symlink lstat never reports REG.
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
    # repo_dir on the RAW (unresolved) path; any symlink whose target
    # resolves outside repo_dir is rejected (a symlinked parent escaping
    # the containment boundary); components at/above repo_depth are the
    # caller's filesystem prefix (on macOS /var → /private/var sits there)
    # — out of boundary.
    repo_depth = len(repo_dir.parts)
    raw = part_path if part_path.is_absolute() else repo_dir / part_path
    for i in range(1, len(raw.parts)):
        if i < repo_depth and raw.is_absolute():
            continue  # strictly above the repo's own depth — out of boundary
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


class PartUploadError(ValueError):
    """A part upload failed the decode gate (unparseable, empty,
    non-finite, over the face cap, zip-bomb, or an unconvertible 3MF
    unit). The route maps it to the 422 with the verbatim
    ``partUpload.unparseable`` detail — nothing is persisted on the way
    out."""


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


def _assert_face_cap(loaded: Any) -> None:
    """The ``MAX_PART_FACES`` gate, shared by the upload's
    ``parse_and_repair`` and the render's ``load_part_geometry``: the total
    face count across ALL geometries of the loaded result, checked right
    after ``trimesh.load`` and before any flatten/repair (it bounds parse
    cost, so both paths must refuse an oversized load)."""
    total_faces = _total_faces(loaded)
    if total_faces > MAX_PART_FACES:
        raise PartUploadError(
            f"mesh has {total_faces} faces; max {MAX_PART_FACES}"
        )


def load_part_geometry(data: bytes, part_format: str) -> trimesh.Trimesh:
    """Guarded load + flatten for the RENDER staging path (issue #330).

    The render worker's 3MF→STL staging needs only the guarded load
    (zip-bomb guard for 3MF, explicit file_type loader), the ``MAX_PART_FACES``
    cap (the same shared check the upload's ``parse_and_repair`` applies),
    and the Scene→``to_mesh`` flatten — NOT the upload's full repair chain
    (``parse_and_repair`` runs merge/pymeshfix/fix_normals and the unit
    validation, which is the upload route's concern, not the worker's).
    ``PartUploadError`` is the closed failure type: the staging caller maps
    it to an ``artifact_error`` RenderResult.
    """
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

    # Face cap: the SAME shared gate as the upload path (issue #330 — a
    # hand-committed oversized part is refused at render time, not just at
    # upload time).
    _assert_face_cap(loaded)

    # Flatten to a single mesh (a Scene is a multi-body import —
    # ``to_mesh`` concatenates; the worker never seeds or emits a 3MF).
    if isinstance(loaded, trimesh.Scene):
        if not loaded.geometry:
            raise PartUploadError("3MF has no geometry")
        return loaded.to_mesh()
    return loaded


def read_part_file_atomic(part_path, max_bytes: int) -> bytes:
    """Read the validated committed part file via an O_NOFOLLOW fd.

    The atomic half of the shared containment standard (the lstat-based
    :func:`validate_part_path` is the pre-open check; THIS is what makes it
    mean something under a concurrent swap): the file is opened with
    ``O_RDONLY | O_NOFOLLOW`` and the size cap is checked against
    ``os.fstat(fd)`` — the actually-opened file, not the path — so a symlink
    swapped in between validation and the read cannot redirect it to a file
    outside the repo. ``OSError`` (vanished path, or a symlink that slipped
    in — the O_NOFOLLOW refusal) propagates to the caller, which maps it to
    its own error contract."""
    fd = os.open(str(part_path), os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise OSError(f"part is not a regular file at read time: {part_path.name}")
        if st.st_size > max_bytes:
            raise PartFileTooLargeError(
                28,  # EDQUOT — file too large
                f"part file is {st.st_size} bytes; max {max_bytes}",
                str(part_path),
            )
        return os.read(fd, st.st_size) if st.st_size > 0 else b""
    finally:
        os.close(fd)


def parse_and_repair(
    data: bytes, part_format: str
) -> tuple[trimesh.Trimesh, dict[str, Any], str | None]:
    """Parse ``data`` (STL or 3MF), apply the face cap + finiteness gates,
    run the repair chain (merge_vertices + pymeshfix + fix_normals — NO
    decimation of the user's part), and measure.

    Returns ``(mesh, report, file_unit)`` where ``mesh`` is the repaired
    mesh in FILE units (the bbox is measured in file units BEFORE any unit
    conversion), ``report`` is the repair report
    (``{triangles, bodies, watertight, gaps_closed, bbox_file_units,
    hole_count}``, plus the OPTIONAL ``bodies_before`` — present ONLY when
    repair still dropped a body, i.e. post-repair bodies < pre-repair
    bodies, so the import report can state "2 bodies → 1 after repair"),
    and ``file_unit`` is the 3MF's declared unit string read
    from the ORIGINAL (pre-repair) loaded geometry (``None`` for STL —
    unitless, or a 3MF with no ``unit`` attribute — the 3MF default is
    millimeters).

    Repair is per-body for multi-body imports: ``bodies`` counts the
    pre-repair watertight components, and a single-body import takes the
    unchanged one-call pymeshfix path (byte-for-byte identical to the
    historical behaviour). A multi-body import repairs EACH watertight
    component separately and concatenates, so the stored mesh keeps all
    N watertight bodies (issue #375 — the one-call repair on the merged
    mesh kept only one component). ``report["bodies"]`` is recomputed
    from the STORED (post-repair) mesh and asserted to equal the counted
    bodies. Non-watertight debris shells are excluded exactly as before
    (the watertight filter on the single split) — the per-body loop
    repairs only the watertight components; the debris is not resurrected,
    not double-counted, and not rejected on new grounds.

    ``hole_count`` is the number of holes the imported part actually has,
    computed ONCE here at import and stored with the part, as
    ``gaps_before + genus``:

    - ``gaps_before`` — the PRE-REPAIR merged mesh's boundary loops
      (``_boundary_loops``), one per OPEN hole opening. The pre-repair
      merged mesh is the right surface for these: the repair chain
      (``pymeshfix``) CLOSES those loops before the stored mesh exists, so
      a post-repair count would read 0 for a holey part (the loops are the
      signal, not a bug to fix).
    - ``genus`` — the total closed-body genus (``watertight_genus``, from
      ``d33d.part_holes``) of the PRE-REPAIR merged mesh's watertight
      components (the same one split that counts the bodies — no second
      split), one per closed through-hole. A
      CAD-exported part with a real drilled through-bore is WATERTIGHT:
      0 boundary loops but genus 1. Gaps alone would count it 0 and the
      fill-recut gate would refuse the very part it exists for, so closed
      holes are counted too.

    A plain watertight box has neither → ``0``; an open holey part has one
    boundary loop per opening (and the closed body under each contributes
    the matching genus); a watertight ring has genus 1 → ``1``. The genus
    computation is wrapped so any exception falls back to ``gaps_before``
    alone — the count degrades, never crashes, never ``None``.

    Raises ``PartUploadError`` (the 422) on any failure: unparseable,
    empty, over the face cap, non-finite (pre- or post-repair), or a 3MF
    unit that is not a string (or a string outside the mm/cm/inch synonym
    sets).
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

    # Face cap: the SAME shared gate as the render path (issue #330).
    _assert_face_cap(loaded)

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

    # ONE split serves both the bodies count and the genus: the bodies
    # are the watertight components (== ``len(split(only_watertight=True))``,
    # pinned by a fixture test), and the genus reads the SAME subset.
    # ``merge_vertices`` is kept — the component split needs merged
    # vertices and the euler count reads the merged topology.
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    components = merged.split(only_watertight=False)
    watertight_bodies = [c for c in components if c.is_watertight]
    bodies_before = len(watertight_bodies)
    if len(merged.faces) == 0:
        raise PartUploadError("mesh is empty after cleanup")

    # The bbox in FILE units, BEFORE repair (the unit conversion multiplies
    # this; pymeshfix preserves the bounding box for a gapped mesh).
    file_bbox = tuple(float(e) for e in merged.extents)
    if not all(math.isfinite(e) for e in file_bbox) or max(file_bbox) <= 0:
        raise PartUploadError("mesh has invalid extents")

    # The 3MF's declared unit, read from the ORIGINAL (pre-repair)
    # geometry — the only place the ``unit`` attribute is guaranteed
    # present. Only an ABSENT unit (``None``) means the 3MF default
    # (millimeters); a non-string unit, or a string outside the mm/cm/inch
    # synonym sets, is a 422 — never a silent mm assumption.
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

    # Genus over the SAME split (measured pre-repair — see docstring); a
    # failure degrades to the boundary-loop signal alone, never a 422.
    try:
        holes = gaps_before + watertight_genus(watertight_bodies)
    except (ArithmeticError, ValueError, TypeError, RuntimeError):
        logger.warning(
            "parse_and_repair: watertight_genus failed on the pre-repair "
            "merged mesh — falling back to the boundary-loop count alone",
            exc_info=True,
        )
        holes = gaps_before

    if bodies_before <= 1:
        # Single body (or zero — a debris-only import repairs the merged
        # mesh as it always has; non-watertight debris is excluded from the
        # bodies count, never resurrected or double-counted): the unchanged
        # one-call pymeshfix path — byte-for-byte identical to the
        # historical repair (issue #375 operator decision 1).
        repaired = repair_with_pmf(merged)
    else:
        # Multi-body: repair EACH watertight body separately and
        # concatenate — one ``MeshFix`` per body, no cap beyond the
        # MAX_PART_FACES gate already applied above (issue #375). A
        # one-call repair on the merged mesh keeps only one component and
        # silently drops the rest.
        repaired = trimesh.util.concatenate(
            [repair_with_pmf(body) for body in watertight_bodies]
        )

    # Finiteness AGAIN after pymeshfix (it can emit NaN from degenerate
    # input) — the same 422, nothing persisted.
    if not _is_finite(repaired):
        raise PartUploadError("mesh has non-finite vertices after repair")
    if len(repaired.faces) == 0:
        raise PartUploadError("mesh is empty after repair")
    # ``bodies`` is the number of watertight components in the STORED
    # (post-repair) mesh — recomputed, never the pre-repair count, so the
    # report describes what was actually kept (issue #375).
    repaired.merge_vertices()
    repaired.update_faces(repaired.nondegenerate_faces())
    bodies = len(repaired.split(only_watertight=True))
    if bodies != len(repaired.split(only_watertight=False)):
        raise PartUploadError("mesh has non-finite vertices after repair")
    # invariant: every stored component is watertight (pinned by the
    # leaky-repair test)
    assert bodies == len(
        [c for c in repaired.split(only_watertight=False) if c.is_watertight]
    )
    gaps_after = _boundary_loops(repaired)

    # ``hole_count``: open-mesh gaps + closed through-holes, both measured
    # on the PRE-REPAIR merged mesh (``gaps_before + genus`` — the pre-repair
    # mesh is the signal for both terms; see docstring). Computed above,
    # before the repair call; the fill-recut gate at chat time only reads
    # this stored fact, never re-parsing the mesh.
    report: dict[str, Any] = {
        "triangles": len(repaired.faces),
        "bodies": bodies,
        "watertight": bool(repaired.is_watertight),
        "gaps_closed": max(0, gaps_before - gaps_after),
        "hole_count": int(holes),
        "bbox_file_units": file_bbox,
    }
    # OPTIONAL field (issue #375 operator decision 1): present ONLY when
    # repair still dropped a body, so the import report can state
    # "2 bodies → 1 after repair". Absent on every input where repair
    # kept all bodies — including single-body imports.
    if bodies < bodies_before:
        report["bodies_before"] = bodies_before
    return repaired, report, file_unit




__all__ = [
    "MAX_PART_FACES",
    "MAX_PART_ZIP_ENTRIES",
    "MAX_PART_ZIP_UNCOMPRESSED",
    "PartFileTooLargeError",
    "PartUploadError",
    "load_part_geometry",
    "mesh_units",
    "parse_and_repair",
    "read_part_file_atomic",
    "repair_with_pmf",
    "validate_part_path",
]
