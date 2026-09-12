"""3MF validation pipeline for the QIDI Plus 5 (ticket #4).

Host-side Python pipeline that takes the STL produced by the render worker
(ticket #2), repairs it with trimesh and pymeshfix into a watertight,
winding-consistent mesh, checks it against the user's stated dimensions,
runs the seven deterministic gates, and writes a single-part 3MF in
millimetres.

This module is the SOLE OWNER of mesh repair, printability gating, and
3MF authoring. The render worker never emits 3MF.

Pipeline order (pinned by docs/backlog/03-print-validation.md):
    trimesh load, force mm, centre
    → merge_vertices, remove_degenerate
    → decimate  (BEFORE repair, never after)
    → pymeshfix.repair
    → trimesh.repair.fix_normals  (AFTER repair)
    → assert is_watertight AND is_winding_consistent  (separately!)
    → thin-feature audit: features ≥1mm for a 0.4mm nozzle
    → auto-orient
    → export 3MF

Seven-gate order (deterministic, human-free):
    1. compiles (from #1)
    2. STL export succeeds
    3. watertight AND winding-consistent
    4. bbox matches stated dimensions
    5. volume > 0, face count sane
    6. slice dry run succeeds  (its own gate)
    7. fits the build plate
"""

from __future__ import annotations

import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

import numpy as np
import pymeshfix as pmf
import trimesh

from d33d import slicer

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

#: QIDI Plus 5 build envelope (X, Y, Z) in millimetres.
#:
#: ⚠️ UNVERIFIED — sources disagree. The real values must be confirmed
#: against the machine before this ticket completes. Pinned here as a
#: named constant so both the centring transform and the acceptance gate
#: read the same value. 320×320×300 mm is the most commonly cited value
#: for the QIDI Plus 5.
QIDI_PLUS_5_ENVELOPE_MM: tuple[float, float, float] = (320.0, 320.0, 300.0)

#: Minimum feature size for a 0.4 mm nozzle, in millimetres.
#: Features below this threshold may not print reliably.
MIN_FEATURE_MM: float = 1.0

#: Dimension tolerance rule: error <= max(1% of stated, 0.5mm), applied
#: per axis. Pinned as a single expression so both sub-clauses (1% vs
#: 0.5mm) are exercisable by distinct fixtures.
DIMENSION_TOLERANCE_PCT: float = 0.01
DIMENSION_TOLERANCE_MIN_MM: float = 0.5

#: Sane face count bounds for gate 5. A mesh with fewer than
#: MIN_FACES faces is degenerate; one with more than MAX_FACES is
#: suspiciously large for a single part.
MIN_FACES: int = 4
MAX_FACES: int = 5_000_000

#: The closed enum of error classes. Each gate reports independently with
#: its own diagnosable class.
ErrorClass = Literal[
    "ok",
    "load_error",
    "watertight",
    "winding",
    "dimension",
    "volume",
    "slice",
    "envelope",
    "export_error",
]

#: Every possible error class. The table is TOTAL — every validation
#: failure lands in exactly one class.
ALL_ERROR_CLASSES: frozenset[str] = frozenset(
    {
        "ok",
        "load_error",
        "watertight",
        "winding",
        "dimension",
        "volume",
        "slice",
        "envelope",
        "export_error",
    }
)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Part:
    """A single part in the validation result.

    v1 emits exactly one part. The shape is designed for N parts later
    so multi-part output is not a retrofit.
    """

    name: str
    bbox_mm: tuple[float, float, float] | None
    volume_mm3: float
    face_count: int
    mesh: trimesh.Trimesh


@dataclass(frozen=True)
class ValidationResult:
    """The return contract of the validation pipeline.

    Shaped for N parts now:
    {"parts": [Part],
     "assembly": {"joints": [], "layout": []},
     "export": {"3mf": path}}
    """

    ok: bool
    error_class: ErrorClass | None
    parts: list[Part]
    assembly_joints: list[Any]  # reserved, empty in v1
    assembly_layout: list[Any]  # reserved, empty in v1
    export_3mf: str | None


# ---------------------------------------------------------------------------
# Dimension gate
# ---------------------------------------------------------------------------


def dimension_error_ok(actual: float, stated: float) -> bool:
    """True iff the error is within the dimension tolerance.

    The rule is pinned as a single expression:
        error <= max(1% of stated, 0.5mm)
    applied per axis.
    """
    error = abs(actual - stated)
    threshold = max(DIMENSION_TOLERANCE_PCT * stated, DIMENSION_TOLERANCE_MIN_MM)
    return error <= threshold


def _check_dimensions(
    bbox_mm: tuple[float, float, float],
    stated_mm: tuple[float, float, float] | None,
) -> bool:
    """Check all three axes against the stated dimensions.

    Returns True iff all three axes satisfy the dimension tolerance rule.
    If stated_mm is None, the check is skipped (returns True).
    """
    if stated_mm is None:
        return True
    for i in range(3):
        if not dimension_error_ok(bbox_mm[i], stated_mm[i]):
            return False
    return True


# ---------------------------------------------------------------------------
# Pipeline stages
# ---------------------------------------------------------------------------


def _force_mm(
    mesh: trimesh.Trimesh,
) -> tuple[trimesh.Trimesh | None, str | None]:
    """Force the mesh to millimetres. STL is unitless; if the mesh has
    non-mm units, convert them. If units are None (unitless), assume mm.

    Returns ``(mesh, None)`` on success, or ``(None, message)`` when a
    string unit that is not mm cannot be converted by trimesh — the
    caller must treat that as a clean ``load_error`` pipeline failure
    rather than silently assuming mm, which would let a genuinely
    non-mm mesh pass every downstream gate at the wrong physical scale.

    ``mesh.units`` is not strictly type-validated at the Python level, so
    the read is guarded with ``isinstance`` — a non-string ``units``
    (trimesh's own ``units`` setter stringifies before storing, but a
    mesh built from raw metadata can hold anything) skips conversion and
    falls back to assuming mm, a known accepted simplification.
    """
    units = mesh.units
    if units is None or not isinstance(units, str):
        mesh.units = "millimeter"
        return mesh, None
    if units.lower() in ("mm", "millimeter", "millimetres"):
        mesh.units = "millimeter"
        return mesh, None
    try:
        mesh.convert_units("mm")
        mesh.units = "millimeter"
    except (ValueError, LookupError, KeyError) as e:
        # A string unit trimesh cannot convert from: fail cleanly so the
        # downstream gates never operate on a mesh at the wrong scale.
        logger.error("Unconvertible mesh unit %r: %s", units, e)
        return None, f"Mesh has unconvertible unit {units!r}: {e}"
    return mesh, None


def _merge_and_clean(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """merge_vertices, remove_degenerate."""
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    return mesh


def _merge_and_clean_preserve_winding(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """merge_vertices, remove_degenerate, then fix_normals to restore
    winding consistency.

    The STL format stores 3 vertices per face (no shared topology). After
    export, the loaded mesh has duplicate vertices and broken edge
    pairing, so ``is_winding_consistent`` is not a reliable indicator of
    the original winding. ``fix_normals`` re-establishes consistent
    winding from the face ordering.

    This must run BEFORE the winding gate check so that a genuinely
    inverted mesh (where the original face ordering is inverted) is
    caught, while a mesh that merely lost its winding in STL export is
    restored.
    """
    mesh.merge_vertices()
    mesh.update_faces(mesh.nondegenerate_faces())
    trimesh.repair.fix_normals(mesh)
    return mesh


def _decimate(mesh: trimesh.Trimesh, factor: float = 0.5) -> trimesh.Trimesh:
    """Decimate the mesh. MUST run BEFORE repair, never after.

    Aggressive decimation creates exactly the degenerate geometry that
    repair is meant to remove. If decimation ran after repair, the
    post-repair fix_normals would never see the degenerate geometry.
    """
    if not hasattr(mesh, "decimate"):
        # trimesh version without decimate — return as-is
        return mesh
    try:
        decimated = mesh.decimate(factor)
        if isinstance(decimated, trimesh.Trimesh) and len(decimated.faces) >= MIN_FACES:
            return decimated
        return mesh
    except (ValueError, AttributeError, RuntimeError) as e:
        # trimesh decimation is optional tooling; on failure (missing
        # meshio-voxels backend, degenerate input) keep the original mesh.
        logger.warning("Decimation failed, keeping original mesh: %s", e)
        return mesh


def _pymeshfix_repair(
    mesh: trimesh.Trimesh,
) -> tuple[np.ndarray, np.ndarray]:
    """Run pymeshfix.repair. Returns (vertices, faces) arrays.

    PyMeshFix can invert normals during repair. fix_normals MUST run
    after this, and is_winding_consistent MUST be asserted SEPARATELY
    from is_watertight.
    """
    fix = pmf.MeshFix(
        mesh.vertices.astype(np.float64),
        mesh.faces.astype(np.int32),
    )
    fix.repair()
    verts = np.asarray(fix.points, dtype=np.float64)
    faces = np.asarray(fix.faces, dtype=np.int32)
    return verts, faces


def _fix_normals(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """trimesh.repair.fix_normals. MUST run AFTER pymeshfix.repair."""
    trimesh.repair.fix_normals(mesh)
    return mesh


def _thin_feature_audit(mesh: trimesh.Trimesh) -> bool:
    """Check that no feature is smaller than MIN_FEATURE_MM.

    A 0.4 mm nozzle cannot reliably print features smaller than ~1 mm.
    We check the minimum edge length as a proxy for feature size.
    """
    if len(mesh.edges) == 0:
        return True
    edge_lengths = mesh.edges_unique_length
    if len(edge_lengths) == 0:
        return True
    return float(np.min(edge_lengths)) >= MIN_FEATURE_MM


def _auto_orient(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Auto-orient the mesh for printing.

    v1: a simple heuristic — orient the shortest bounding-box axis to
    Z (up) for a stable base. More sophisticated orientation (e.g.
    minimising support material) is out of scope for v1.
    """
    # For v1, we leave orientation as-is and rely on the slicer's
    # auto-orient. This is a placeholder.
    return mesh


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------


def validate_stl(
    stl_path: str,
    stated_mm: tuple[float, float, float] | None = None,
    output_dir: str | None = None,
    slice_dry_run_fn: Callable[
        [str, str | None], slicer.SliceDryRunResult
    ] = slicer.slice_dry_run,
) -> ValidationResult:
    """Run the full validation pipeline on an STL file.

    Args:
        stl_path: Path to the input STL file (from the render worker).
        stated_mm: The user's stated dimensions (X, Y, Z) in mm. If None,
            the dimension gate is skipped.
        output_dir: Directory to write the 3MF output. If None, a
            temporary directory is used.
        slice_dry_run_fn: Gate 6's slice driver, injected for testability
            (same pattern as the render worker). Defaults to the real
            ``d33d.slicer.slice_dry_run``; on a machine without a slicer
            binary it fails closed (ok=False) rather than passing stubs.
            Fast tests inject a stub so wiring and gate order stay
            exercisable without a real slicer binary.

    Returns:
        ValidationResult with ok/error_class, parts, assembly, export.
    """
    # Input validation: stated_mm must be exactly 3 axes. A 2- or 4-tuple
    # would otherwise escape as an uncaught IndexError in _check_dimensions.
    if stated_mm is not None and len(stated_mm) != 3:
        return _fail(
            "dimension",
            None,
            f"stated_mm must be a 3-tuple (X, Y, Z), got length {len(stated_mm)}",
        )

    # Determine output directory
    if output_dir is None:
        output_dir = tempfile.mkdtemp(prefix="d33d_3mf_")
    os.makedirs(output_dir, exist_ok=True)
    output_3mf = os.path.join(output_dir, "model.3mf")

    # Gate 1: compiles (from #1) — the STL was produced by the render
    # worker, so it already compiled. We just need to load it.
    # Gate 2: STL export succeeds — we load the STL.
    try:
        loaded = trimesh.load(stl_path, process=False)
        if isinstance(loaded, trimesh.Scene):
            # Extract the first geometry
            geos = list(loaded.geometry.values())
            if not geos:
                return _fail("load_error", None)
            mesh = geos[0]
        else:
            mesh = loaded
        if not isinstance(mesh, trimesh.Trimesh):
            return _fail("load_error", None)
    except ValueError as e:
        # trimesh.load raises ValueError for a missing file ("string is
        # not a file") and for parse failures; the loader dispatches on
        # extension and the STL backend itself raises ValueError on
        # malformed data. OSError covers the edge case where the file
        # exists at dispatch time but fails on read.
        return _fail("load_error", None, f"Failed to load STL: {e}")
    except OSError as e:
        return _fail("load_error", None, f"Failed to load STL: {e}")

    # Force mm. A string unit trimesh cannot convert from is a clean
    # load_error, not a silent fall-back to mm (which would let a
    # genuinely non-mm mesh pass every downstream gate at the wrong
    # physical scale).
    mesh, unit_error = _force_mm(mesh)
    if mesh is None:
        return _fail("load_error", None, unit_error or "Unconvertible mesh unit")

    # Centre the mesh within the build envelope
    _centre_mesh(mesh)

    # Check for empty/degenerate mesh early
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return _fail(
            "load_error", _part_from_mesh(mesh), "Mesh has no faces or vertices"
        )

    # Gate 3: watertight AND winding-consistent (separate assertions)
    #
    # The STL format stores 3 vertices per face (no shared topology), so
    # ``is_winding_consistent`` on a freshly-loaded STL is not a reliable
    # indicator of the original winding — the edge pairing is broken by
    # the export. We must run ``merge_vertices`` + ``fix_normals`` first
    # to restore a valid topology and consistent winding, THEN check the
    # winding gate.
    #
    # Strategy:
    # 1. merge_vertices + fix_normals on the loaded mesh → restores
    #    winding consistency for meshes that merely lost it in STL export.
    # 2. Check watertight. If NOT watertight (holey), run pymeshfix to
    #    close holes, then fix_normals again (AFTER repair).
    # 3. Check winding. If still not winding-consistent, fail the gate.

    # Step 1: restore topology and winding from STL export
    mesh = _merge_and_clean_preserve_winding(mesh)

    # Re-check for empty mesh after cleanup (remove_degenerate may have
    # removed all faces from a degenerate mesh)
    if len(mesh.faces) == 0 or len(mesh.vertices) == 0:
        return _fail("load_error", _part_from_mesh(mesh), "Mesh is empty after cleanup")

    # Step 2: check watertight
    if not mesh.is_watertight:
        # Holey mesh — run the repair pipeline
        mesh = _decimate(mesh, 0.5)
        try:
            verts, faces = _pymeshfix_repair(mesh)
            mesh = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
            mesh = _merge_and_clean_preserve_winding(mesh)
        except (ValueError, RuntimeError) as e:
            # pymeshfix failed (degenerate input / repair failure) — use
            # the pre-repair mesh; the watertight re-check below decides.
            logger.warning("pymeshfix repair failed, using pre-repair mesh: %s", e)
        # Re-check watertight after repair
        if not mesh.is_watertight:
            return _fail(
                "watertight",
                _part_from_mesh(mesh),
                "Mesh is not watertight after repair",
            )

    # Step 3: check winding (SEPARATE assertion from watertight)
    if not mesh.is_winding_consistent:
        return _fail(
            "winding",
            _part_from_mesh(mesh),
            "Mesh is not winding-consistent after repair (inverted normals)",
        )

    # Thin-feature audit
    if not _thin_feature_audit(mesh):
        return _fail("volume", _part_from_mesh(mesh), "Feature below 1mm minimum")

    # Auto-orient
    mesh = _auto_orient(mesh)

    # Get bounding box in mm
    bbox_mm = tuple(float(x) for x in mesh.extents)

    # Gate 4: dimension check
    if not _check_dimensions(bbox_mm, stated_mm):
        return _fail(
            "dimension",
            _part_from_mesh(mesh),
            f"Dimensions {bbox_mm} don't match stated {stated_mm}",
        )

    # Gate 5: volume > 0, face count sane
    if mesh.volume <= 0:
        return _fail("volume", _part_from_mesh(mesh), f"Volume is {mesh.volume}")
    if len(mesh.faces) < MIN_FACES:
        return _fail(
            "volume",
            _part_from_mesh(mesh),
            f"Face count {len(mesh.faces)} < {MIN_FACES}",
        )
    if len(mesh.faces) > MAX_FACES:
        return _fail(
            "volume",
            _part_from_mesh(mesh),
            f"Face count {len(mesh.faces)} > {MAX_FACES}",
        )

    # Gate 6: slice dry run (its own gate, driven by the real slicer via
    # slice_dry_run_fn; fails closed on an unconfigured machine).
    # Export the repaired mesh to a temp STL first: the slicer binary
    # reads a mesh file, not a trimesh object.
    slice_model = os.path.join(output_dir, "slice_model.stl")
    try:
        mesh.export(slice_model)
    except (OSError, NotImplementedError, ValueError) as e:
        # The intermediate STL export is an export step like the final 3MF
        # export: a disk-full or permissions error here is a diagnosable
        # export failure, not an unhandled pipeline crash.
        return _fail(
            "export_error", _part_from_mesh(mesh), f"STL export failed: {e}"
        )
    try:
        slice_result = slice_dry_run_fn(slice_model, output_dir)
    except (OSError, RuntimeError) as e:
        # An injected stub is never expected to raise; the real driver
        # returns a fail result rather than raising, so a raise means the
        # driver itself crashed — fail the gate, don't crash the pipeline.
        # OSError covers a missing/unreadable slicer binary; RuntimeError
        # covers a driver crash.
        return _fail("slice", _part_from_mesh(mesh), f"Slice dry run crashed: {e}")
    if not slice_result.ok:
        return _fail(
            "slice",
            _part_from_mesh(mesh),
            f"Slice dry run failed: {slice_result.error_string or slice_result.detail}",
        )

    # Gate 7: fits the build plate
    env = QIDI_PLUS_5_ENVELOPE_MM
    for i in range(3):
        if bbox_mm[i] > env[i]:
            return _fail(
                "envelope",
                _part_from_mesh(mesh),
                f"Dimension {i} ({bbox_mm[i]}mm) exceeds envelope {env[i]}mm",
            )

    # Export 3MF
    try:
        mesh.export(output_3mf)
    except (OSError, NotImplementedError, ValueError) as e:
        # trimesh export can raise OSError (disk/IO), NotImplementedError
        # for an unsupported export format, or ValueError for a bad
        # file object — any of these is a diagnosable export failure, not
        # a pipeline crash.
        return _fail("export_error", _part_from_mesh(mesh), f"3MF export failed: {e}")

    part = Part(
        name="part_0",
        bbox_mm=bbox_mm,
        volume_mm3=float(mesh.volume),
        face_count=len(mesh.faces),
        mesh=mesh,
    )

    return ValidationResult(
        ok=True,
        error_class=None,
        parts=[part],
        assembly_joints=[],
        assembly_layout=[],
        export_3mf=output_3mf,
    )


def _centre_mesh(mesh: trimesh.Trimesh) -> None:
    """Centre the mesh within the QIDI Plus 5 build envelope.

    The envelope constant is read by both this centring transform and
    the acceptance gate (gate 7).
    """
    env = QIDI_PLUS_5_ENVELOPE_MM
    extents = mesh.extents
    # If the mesh exceeds the envelope, we can't centre it — gate 7
    # will fail. We still centre the mesh within the envelope for the
    # parts that fit.
    # Centre: shift the mesh so its centre is at the envelope centre.
    # For a single part, this means shifting by (env/2 - mesh_centroid).
    # But we only shift if the mesh fits; otherwise leave it and let
    # gate 7 fail.
    fits = all(extents[i] <= env[i] for i in range(3))
    if fits:
        target_centre = np.array(env) / 2.0
        current_centre = mesh.centroid
        mesh.apply_translation(target_centre - current_centre)


def _part_from_mesh(mesh: trimesh.Trimesh) -> Part:
    """Create a Part dataclass from a mesh."""
    extents = mesh.extents
    bbox_mm = (
        tuple(float(x) for x in extents) if extents is not None else (0.0, 0.0, 0.0)
    )
    vol = float(mesh.volume) if mesh.volume > 0 else 0.0
    return Part(
        name="part_0",
        bbox_mm=bbox_mm,
        volume_mm3=vol,
        face_count=len(mesh.faces),
        mesh=mesh,
    )


def _fail(
    error_class: ErrorClass,
    part: Part | None,
    message: str = "",
) -> ValidationResult:
    """Create a failed ValidationResult."""
    return ValidationResult(
        ok=False,
        error_class=error_class,
        parts=[part] if part else [],
        assembly_joints=[],
        assembly_layout=[],
        export_3mf=None,
    )


# ---------------------------------------------------------------------------
# Region-selection containment gate (ticket #6)
# ---------------------------------------------------------------------------
#
# Scope: the containment METRIC only — the fraction of changed triangle
# area falling outside a selected region's 3D bounding volume. How that
# volume is resolved (module registry lookup, lasso-to-3D lift) is owned
# by other workstreams of ticket #6; this module treats the volume as an
# opaque axis-aligned box (bbox_min, bbox_max) in mm, matching the mesh's
# own coordinate space.

#: Named env var carrying the containment threshold, as a percentage
#: (e.g. "5.0" means 5%). Per spec this MUST be configurable (env/YAML)
#: and never a hardcoded code constant — it is an initial guess to be
#: tuned once the golden set produces real spillover data.
CONTAINMENT_THRESHOLD_ENV_VAR: str = "D33D_CONTAINMENT_THRESHOLD_PCT"

#: Fallback used when the env var is absent or unparseable. The spec's
#: own initial guess, to be tuned once the golden set produces real data.
DEFAULT_CONTAINMENT_THRESHOLD_PCT: float = 5.0

BBoxCorner = tuple[float, float, float]


@dataclass(frozen=True)
class ContainmentResult:
    """The containment gate's verdict for one region-scoped edit."""

    ok: bool
    spillover_pct: float
    threshold_pct: float


def containment_threshold_pct() -> float:
    """Read the containment threshold (percent) from the named env var.

    Falls back to :data:`DEFAULT_CONTAINMENT_THRESHOLD_PCT` when the env
    var is unset or holds a value that cannot be parsed as a float, so a
    malformed override fails closed to the documented default rather than
    crashing the gate.
    """
    raw = os.environ.get(CONTAINMENT_THRESHOLD_ENV_VAR)
    if raw is None:
        return DEFAULT_CONTAINMENT_THRESHOLD_PCT
    try:
        return float(raw)
    except ValueError:
        logger.warning(
            "Invalid %s=%r, falling back to default %.2f%%",
            CONTAINMENT_THRESHOLD_ENV_VAR,
            raw,
            DEFAULT_CONTAINMENT_THRESHOLD_PCT,
        )
        return DEFAULT_CONTAINMENT_THRESHOLD_PCT


#: Decimal places vertex coordinates are rounded to before signature
#: comparison — enough to survive floating-point noise from mesh I/O
#: while still distinguishing genuinely different vertex positions.
_CHANGED_FACES_DECIMALS = 6


def _face_signature_rows(mesh: trimesh.Trimesh, decimals: int) -> np.ndarray:
    """Per-face signature rows: each face's 3 vertices, rounded and taken
    in ascending VERTEX-INDEX order (matching the original per-face
    ``sorted(face)`` canonicalisation — sorted by index, not by
    coordinate value), flattened to one row of 9 floats per face.

    Vectorized: one bulk ``np.round`` over all vertices, one fancy-index
    gather of per-face vertex blocks (``vertices[faces_sorted]``) — no
    per-face Python loop.
    """
    faces_sorted = np.sort(mesh.faces, axis=1)
    rounded_vertices = np.round(mesh.vertices, decimals)
    blocks = rounded_vertices[faces_sorted]  # (n_faces, 3, 3)
    return blocks.reshape(blocks.shape[0], -1)


def _as_void_rows(rows: np.ndarray) -> np.ndarray:
    """View each row of a 2D float array as one opaque structured record,
    so ``np.isin``/``np.unique`` treat whole rows as atomic values for
    fast set-membership without a per-row Python loop."""
    contiguous = np.ascontiguousarray(rows)
    return contiguous.view([("", contiguous.dtype)] * contiguous.shape[1]).reshape(-1)


def changed_faces(pre_mesh: trimesh.Trimesh, post_mesh: trimesh.Trimesh) -> np.ndarray:
    """Return the indices (into ``post_mesh.faces``) of triangles that are
    new relative to ``pre_mesh``.

    A post-edit triangle counts as "changed" when no triangle in the
    pre-edit mesh occupies the same position — compared by each
    triangle's vertex coordinates, rounded to survive floating-point
    noise from mesh I/O, rather than by face-index identity (which is not
    meaningful across two independently-generated meshes with different
    topology/vertex ordering).

    Vectorized with numpy end to end (no per-face Python loop): vertices
    are rounded in bulk, gathered per-face via fancy indexing, and
    membership against the pre-mesh's signatures is computed with
    ``np.isin`` over an opaque per-row view — behaviourally identical to
    (not merely approximating) the original set-based comparison,
    including the index-order canonicalisation.
    """
    decimals = _CHANGED_FACES_DECIMALS

    if len(post_mesh.faces) == 0:
        return np.array([], dtype=np.int64)

    pre_rows = _face_signature_rows(pre_mesh, decimals)
    post_rows = _face_signature_rows(post_mesh, decimals)

    if len(pre_rows) == 0:
        # Nothing in pre_mesh to match against — every post face changed.
        return np.arange(len(post_rows), dtype=np.int64)

    pre_void = _as_void_rows(pre_rows)
    post_void = _as_void_rows(post_rows)

    is_new = ~np.isin(post_void, pre_void)
    return np.nonzero(is_new)[0].astype(np.int64)


def spillover_fraction(
    mesh: trimesh.Trimesh,
    changed_face_indices: np.ndarray,
    bbox_min: BBoxCorner,
    bbox_max: BBoxCorner,
) -> float:
    """Fraction of changed triangle area falling outside the region bbox.

    Returns 0.0 when there are no changed faces (nothing to be outside).
    The denominator is the total area of the changed triangles only — the
    metric measures spillover of the EDIT, not of the whole mesh.

    Vectorized with numpy end to end (no per-face Python loop): all
    triangle areas are computed at once via cross products over
    ``mesh.vertices[mesh.faces[changed_face_indices]]``, the bbox-
    containment test (a triangle is inside/outside decided by its
    centroid, matching the original per-triangle behaviour) is a single
    broadcast comparison over all centroids, and the result is reduced
    with numpy sums.
    """
    if len(changed_face_indices) == 0:
        return 0.0

    changed_idx = np.asarray(changed_face_indices, dtype=np.int64)
    faces = mesh.faces[changed_idx]
    tri_vertices = mesh.vertices[faces]  # (n_changed, 3, 3)

    a = tri_vertices[:, 0, :]
    b = tri_vertices[:, 1, :]
    c = tri_vertices[:, 2, :]
    cross = np.cross(b - a, c - a)
    areas = np.linalg.norm(cross, axis=1) / 2.0

    centroids = tri_vertices.mean(axis=1)
    lo = np.asarray(bbox_min, dtype=np.float64)
    hi = np.asarray(bbox_max, dtype=np.float64)
    inside = np.all(centroids >= lo, axis=1) & np.all(centroids <= hi, axis=1)
    outside_area = np.where(inside, 0.0, areas)

    total_area = float(areas.sum())
    if total_area <= 0.0:
        return 0.0
    return float(outside_area.sum() / total_area)


def check_containment(
    pre_mesh: trimesh.Trimesh,
    post_mesh: trimesh.Trimesh,
    bbox_min: BBoxCorner,
    bbox_max: BBoxCorner,
) -> ContainmentResult:
    """Run the containment gate for one region-scoped edit.

    Computes the fraction of changed triangle area (``post_mesh`` vs.
    ``pre_mesh``) falling outside the selected region's 3D bounding
    volume, compares it against the named configurable threshold (see
    :func:`containment_threshold_pct`), and logs the measured fraction
    for this run regardless of the pass/fail outcome.
    """
    changed = changed_faces(pre_mesh, post_mesh)
    fraction = spillover_fraction(post_mesh, changed, bbox_min, bbox_max)
    spillover_pct = fraction * 100.0
    threshold_pct = containment_threshold_pct()
    ok = spillover_pct <= threshold_pct

    logger.info(
        "containment gate: spillover=%.4f%% threshold=%.4f%% changed_faces=%d ok=%s",
        spillover_pct,
        threshold_pct,
        len(changed),
        ok,
    )

    return ContainmentResult(
        ok=ok, spillover_pct=spillover_pct, threshold_pct=threshold_pct
    )
