"""Deterministic gates 1–5 for the eval harness (issue #9, workstream
``task-gates``).

The eval harness scores LLM-generated OpenSCAD through seven deterministic
gates before any vision judge. This module owns gates 1–5 and the
per-gate failure-class tagging:

    1 compiles (exit 0)     → tagged by the OpenSCAD LLM failure class,
                               never collapsed into "compile failed"
    2 STL export succeeds
    3 watertight AND winding consistent (asserted separately)
    4 bbox within max(1%, 0.5mm) of the case's stated dims, per axis
    5 volume > 0 AND face count within the named sanity bounds

Gate 6 (slice dry run) and gate 7 (region containment) live in sibling
workstream modules (``slice_gate`` / ``region_gate``); the result
container here leaves a dedicated slot for each so the per-gate report
carries all seven columns from one call site.

Design rules
------------

* **Consumes, never re-implements.** The dimension expression reuses
  :func:`d33d.print_validation.dimension_error_ok` (the same
  ``error <= max(1% of stated, 0.5mm)`` expression the #4 pipeline
  pins); face-count sanity reuses the ``MIN_FACES`` / ``MAX_FACES``
  named constants from ``print_validation``; the failure-class
  superset reuses :mod:`d33d.failure_classes` (11 named LLM classes +
  5 non-repairable render-worker classes + 1 fallback), never a fork.
* **Failure classes are per-gate taggable.** The closed superset (the
  #5 taxonomy plus ``graceful_refusal`` and ``clearance_applied``,
  which only adversarial outcomes tag) is pinned as an enum, and
  :data:`GATE_TAGGABLE_CLASSES` declares, per gate, the subset of the
  superset that gate can actually tag. This resolves the issue's open
  question: ``geometrically_wrong`` is taggable only at gate 4 (bbox
  deviation is a deterministic proxy) and the judge stage — never
  collapsed into a generic failure bucket.
* **Superset → #5 mapping.** :func:`map_to_design_classes` projects the
  superset back onto the 11 named LLM classes of #5 for the regression
  report, so "prompt v7 fails case 12 which v5 passed" stays readable
  in the original taxonomy.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

import trimesh

from d33d import failure_classes as fc
from d33d import print_validation as pv
from d33d.render_worker import ERROR_CLASSES as RENDER_WORKER_ERROR_CLASSES

# ---------------------------------------------------------------------------
# Failure-class superset (reused, not forked)
# ---------------------------------------------------------------------------

#: The eval superset: the 11 named LLM failure classes of #5 (reused from
#: :data:`d33d.failure_classes.REPAIRABLE_CLASSES` — ``unclassified_
#: syntax_error`` included, as #5's list is the 10 stderr classes plus
#: ``geometrically_wrong``) plus the 5 non-repairable render-worker
#: classes the harness must still distinguish, plus the 1 unrecognised-
#: syntax fallback, PLUS the two outcome classes the eval context needs:
#: ``graceful_refusal`` and ``clearance_applied``.
#:
#: Adversarial cases are scored against those last two classes, never as
#: compile failures.
EvalFailureClass = Literal[
    # 11 named LLM classes (from #5 / failure_classes)
    "trailing_semicolon",
    "transform_order",
    "wrong_axis_rotation",
    "difference_inversion",
    "hull_miskowski_misuse",
    "zup_yup_confusion",
    "magic_numbers",
    "projection_offset_fragile",
    "text_missing_font",
    "hallucinated_bosl2",
    "geometrically_wrong",
    # 5 non-repairable render-worker classes
    "timeout",
    "oom",
    "container_error",
    "artifact_error",
    "empty_model",
    # Fallback for unrecognised syntax errors
    "unclassified_syntax_error",
    # Two eval-outcome classes (adversarial scoring)
    "graceful_refusal",
    "clearance_applied",
]

#: Closed enum of the superset.
EVAL_FAILURE_CLASSES: frozenset[EvalFailureClass] = frozenset(
    {c for c in fc.FAILURE_CLASSES}
    | frozenset({"graceful_refusal", "clearance_applied"})
)

#: The 11 named LLM classes of #5 — the superset's mapping target.
DESIGN_LOOP_CLASSES: frozenset[EvalFailureClass] = frozenset(fc.FAILURE_CLASSES)

#: The two outcome classes only adversarial cases score against.
OUTCOME_CLASSES: frozenset[EvalFailureClass] = frozenset(
    {"graceful_refusal", "clearance_applied"}
)


def map_to_design_classes(
    classes: set[EvalFailureClass],
) -> dict[EvalFailureClass, EvalFailureClass]:
    """Project the superset back onto #5's taxonomy, per class.

    The 17 classes of :data:`fc.FAILURE_CLASSES` (the 11 named LLM
    classes of #5 — including ``geometrically_wrong`` — plus the 5
    non-repairable render-worker classes and the unrecognised-syntax
    fallback) map to themselves — they *are* #5's vocabulary. The two eval outcome classes (``graceful_refusal`` /
    ``clearance_applied``) do not exist in #5's taxonomy; they map to
    ``unclassified_syntax_error``, the single #5 class they collapse
    into, so the report can always be read in the original vocabulary.
    """
    mapped: dict[EvalFailureClass, EvalFailureClass] = {}
    for c in classes:
        if c in fc.FAILURE_CLASSES:
            mapped[c] = c
        else:
            mapped[c] = "unclassified_syntax_error"
    return mapped


# ---------------------------------------------------------------------------
# Gate names
# ---------------------------------------------------------------------------

#: The five gates owned by this module, in pinned order.
GATE_1 = "compile"
GATE_2 = "stl_export"
GATE_3 = "watertight"
GATE_4 = "bbox"
GATE_5 = "volume"

#: All five gate names in order.
GATE_NAMES: tuple[str, ...] = (GATE_1, GATE_2, GATE_3, GATE_4, GATE_5)

#: Per-gate taggability: which superset classes each gate can tag.
#:
#: Gate 1 tags the OpenSCAD failure class that produced the compile
#: failure — the 11 named classes plus the fallback, and the
#: non-repairable render-worker classes (a run that timed out or OOMed
#: also fails to compile). ``geometrically_wrong`` is deliberately
#: excluded: an output that fails to compile is, by definition, not a
#: "compiles cleanly but geometrically wrong" outcome.
#:
#: Gates 2–5 run on a successfully-compiled STL. Their taggable classes
#: are the superset minus ``geometrically_wrong`` (only gate 4 can tag
#: it, as the deterministic bbox proxy) and minus the eval outcome
#: classes (adversarial verdicts, not gate failures).
_GATE_1_TAGGABLE: frozenset[EvalFailureClass] = frozenset(
    fc.FAILURE_CLASSES
) - frozenset({"geometrically_wrong"})
_POST_COMPILE_TAGGABLE: frozenset[EvalFailureClass] = frozenset(
    fc.FAILURE_CLASSES
) - frozenset({"geometrically_wrong"})
GATE_4_TAGGABLE: frozenset[EvalFailureClass] = _POST_COMPILE_TAGGABLE | {
    "geometrically_wrong",
}

#: Per-gate taggable-class table. Gate 7 (region containment, sibling
#: module) and gate 6 (slice dry run, sibling module) get their own
#: entries from their modules.
GATE_TAGGABLE_CLASSES: dict[str, frozenset[EvalFailureClass]] = {
    GATE_1: _GATE_1_TAGGABLE,
    GATE_2: _POST_COMPILE_TAGGABLE,
    GATE_3: _POST_COMPILE_TAGGABLE,
    GATE_4: GATE_4_TAGGABLE,
    GATE_5: _POST_COMPILE_TAGGABLE,
}


def assert_taggable(gate: str, failure_class: EvalFailureClass) -> None:
    """Raise ``ValueError`` if ``failure_class`` is not taggable at
    ``gate``.

    Keeps the per-gate tagging invariant testable: a caller that tries
    to tag ``geometrically_wrong`` at gate 1 (or any outcome class at
    any of gates 1–5) is a bug, not a silent bucket.
    """
    taggable = GATE_TAGGABLE_CLASSES.get(gate)
    if taggable is None:
        raise ValueError(f"unknown gate: {gate!r}")
    if failure_class not in taggable:
        raise ValueError(
            f"failure class {failure_class!r} is not taggable at gate "
            f"{gate!r}; taggable: {sorted(taggable)}"
        )


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateResult:
    """One gate's outcome.

    ``status`` is ``"pass"``, ``"fail"``, or ``"na"`` (the gate does
    not apply — e.g. gate 4 is skipped when the case declares no
    stated dimensions). ``failure_class`` is set only on ``"fail"``
    and is always a taggable class for this gate (see
    :data:`GATE_TAGGABLE_CLASSES`).
    """

    gate: str
    status: Literal["pass", "fail", "na"]
    failure_class: EvalFailureClass | None = None
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "gate": self.gate,
            "status": self.status,
            "failure_class": self.failure_class,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class GateReport:
    """The per-case outcome of gates 1–5 (with slots for 6–7).

    ``gates`` maps gate name → :class:`GateResult`. ``ok`` is True iff
    every present gate passed (``"na"`` gates are not failures).
    """

    case_id: str
    gates: dict[str, GateResult]

    def gate(self, name: str) -> GateResult | None:
        return self.gates.get(name)

    @property
    def ok(self) -> bool:
        return all(r.status != "fail" for r in self.gates.values())

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "ok": self.ok,
            "gates": {name: r.to_dict() for name, r in self.gates.items()},
        }


# ---------------------------------------------------------------------------
# Gate 1 — compiles, tagged by failure class
# ---------------------------------------------------------------------------


def gate1_compiles(
    error_class: str, stderr: str = "", scad_source: str = ""
) -> GateResult:
    """Gate 1: the render worker reported a successful run.

    ``error_class`` is the render worker's closed 7-class enum. On a
    non-``ok`` class the gate fails and is tagged with the specific
    OpenSCAD LLM failure class via
    :func:`d33d.failure_classes.classify_failure` — never a bare
    "compile failed".
    """
    if error_class == "ok":
        return GateResult(gate=GATE_1, status="pass")
    if error_class not in RENDER_WORKER_ERROR_CLASSES:
        raise ValueError(
            f"error_class {error_class!r} is not in the render worker's "
            f"closed enum: {sorted(RENDER_WORKER_ERROR_CLASSES)}"
        )
    classified = fc.classify_failure(
        error_class=error_class, stderr=stderr, scad_source=scad_source
    )
    assert_taggable(GATE_1, classified.failure_class)
    return GateResult(
        gate=GATE_1,
        status="fail",
        failure_class=classified.failure_class,
        detail=classified.evidence,
    )


# ---------------------------------------------------------------------------
# Gate 2 — STL export succeeds
# ---------------------------------------------------------------------------


def gate2_stl_export(stl_path: Any) -> GateResult:
    """Gate 2: the STL artifact exists and is a non-empty string path.

    The render worker only reports ``ok`` / valid artifacts when the STL
    export succeeded; this gate is the harness-side guard for the case
    where a caller supplies an absent or invalid path.
    """
    if isinstance(stl_path, str) and stl_path:
        return GateResult(gate=GATE_2, status="pass")
    assert_taggable(GATE_2, "artifact_error")
    return GateResult(
        gate=GATE_2,
        status="fail",
        failure_class="artifact_error",
        detail=f"STL export missing or invalid: {stl_path!r}",
    )


# ---------------------------------------------------------------------------
# Gate 3 — watertight AND winding consistent, separately
# ---------------------------------------------------------------------------


def gate3_watertight(mesh: trimesh.Trimesh) -> GateResult:
    """Gate 3: the mesh is watertight AND winding-consistent.

    The two properties are asserted separately (per the #4 spec) but
    reported as one gate: either failing fails the gate. The tag is
    ``artifact_error`` — a mesh that is not watertight/winding-
    consistent is an artifact-quality defect, not an LLM-generation
    defect.
    """
    if mesh.is_watertight and mesh.is_winding_consistent:
        return GateResult(gate=GATE_3, status="pass")
    problems = []
    if not mesh.is_watertight:
        problems.append("not watertight")
    if not mesh.is_winding_consistent:
        problems.append("not winding-consistent")
    assert_taggable(GATE_3, "artifact_error")
    return GateResult(
        gate=GATE_3,
        status="fail",
        failure_class="artifact_error",
        detail="; ".join(problems),
    )


# ---------------------------------------------------------------------------
# Gate 4 — bbox within max(1%, 0.5mm) of stated dims, per axis
# ---------------------------------------------------------------------------


def _axis_dim_tolerance(stated: float) -> float:
    """The per-axis dimension tolerance: ``max(1% of stated, 0.5mm)``."""
    return max(pv.DIMENSION_TOLERANCE_PCT * stated, pv.DIMENSION_TOLERANCE_MIN_MM)


def gate4_bbox(
    mesh: trimesh.Trimesh,
    expected_dims: dict[str, float] | None = None,
) -> GateResult:
    """Gate 4: the mesh bbox is within ``max(1%, 0.5mm)`` of the case's
    stated dimensions, per axis.

    ``expected_dims`` is the named per-case field (``{"x": ..., "y":
    ..., "z": ...}`` in mm) — the source of truth for this gate,
    independent of whether a versioned project exists. ``None`` skips
    the gate (status ``"na"``), because a case without stated
    dimensions has nothing to compare against.

    The tolerance expression is the one pinned in #4 (reused from
    :func:`d33d.print_validation.dimension_error_ok`'s constants),
    applied per axis. A failure is tagged ``geometrically_wrong`` —
    the output compiled fine, so the failure is geometric, and the
    bbox is the deterministic proxy for the vision-only class.
    """
    if expected_dims is None:
        return GateResult(
            gate=GATE_4, status="na", detail="no stated dimensions declared"
        )
    for axis in ("x", "y", "z"):
        if axis not in expected_dims:
            raise ValueError(
                f"expected_dims missing axis {axis!r}: {sorted(expected_dims)}"
            )
    extents = mesh.extents
    if extents is None:
        extents = (0.0, 0.0, 0.0)
    for i, axis in ((0, "x"), (1, "y"), (2, "z")):
        actual = float(extents[i])
        stated = float(expected_dims[axis])
        if not pv.dimension_error_ok(actual, stated):
            assert_taggable(GATE_4, "geometrically_wrong")
            return GateResult(
                gate=GATE_4,
                status="fail",
                failure_class="geometrically_wrong",
                detail=(
                    f"axis {axis}: actual {actual:.4f}mm vs stated "
                    f"{stated:.4f}mm (tolerance "
                    f"{_axis_dim_tolerance(stated):.4f}mm)"
                ),
            )
    return GateResult(gate=GATE_4, status="pass")


# ---------------------------------------------------------------------------
# Gate 5 — volume > 0 AND face count sane
# ---------------------------------------------------------------------------


def gate5_volume(mesh: trimesh.Trimesh) -> GateResult:
    """Gate 5: the mesh volume is positive AND the face count is within
    the named sanity bounds (``MIN_FACES``/``MAX_FACES`` from
    ``print_validation`` — reused, never redefined).

    A mesh that fails gate 3 (watertight/winding) is not a valid
    volume/face-count candidate; the tag is ``empty_model`` for a
    zero/negative volume and ``artifact_error`` for an out-of-bounds
    face count.
    """
    volume = float(mesh.volume) if mesh.volume > 0 else 0.0
    if volume <= 0:
        assert_taggable(GATE_5, "empty_model")
        return GateResult(
            gate=GATE_5,
            status="fail",
            failure_class="empty_model",
            detail=f"volume is {volume!r} mm³ (must be > 0)",
        )
    faces = len(mesh.faces)
    if not (pv.MIN_FACES <= faces <= pv.MAX_FACES):
        assert_taggable(GATE_5, "artifact_error")
        return GateResult(
            gate=GATE_5,
            status="fail",
            failure_class="artifact_error",
            detail=(
                f"face count {faces} outside sane bounds "
                f"[{pv.MIN_FACES}, {pv.MAX_FACES}]"
            ),
        )
    return GateResult(
        gate=GATE_5,
        status="pass",
        detail=f"volume {volume:.4f} mm³, {faces} faces",
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def run_gates_1_5(
    *,
    case_id: str,
    render_error_class: str,
    stderr: str = "",
    scad_source: str = "",
    stl_path: Any = None,
    mesh: trimesh.Trimesh | None = None,
    expected_dims: dict[str, float] | None = None,
) -> GateReport:
    """Run gates 1–5 in pinned order and return the per-case report.

    Gates short-circuit on the first failure (a mesh that does not
    compile cannot be checked for watertightness, etc.), but the
    report always carries a slot for every gate — gates not reached
    after a failure are absent from ``gates`` (the sibling 6/7 slots
    are added by the caller).

    ``expected_dims`` is the named per-case field (``{"x": ..., "y":
    ..., "z": ...}`` in mm). ``None`` makes gate 4 ``"na"``.
    """
    gates: dict[str, GateResult] = {}

    g1 = gate1_compiles(render_error_class, stderr, scad_source)
    gates[GATE_1] = g1
    if g1.status == "fail":
        return GateReport(case_id=case_id, gates=gates)

    g2 = gate2_stl_export(stl_path)
    gates[GATE_2] = g2
    if g2.status == "fail":
        return GateReport(case_id=case_id, gates=gates)

    if mesh is None:
        # No mesh to check gates 3–5 on: treat as an artifact defect at
        # gate 3 (the first mesh-consuming gate).
        gates[GATE_3] = GateResult(
            gate=GATE_3,
            status="fail",
            failure_class="artifact_error",
            detail="no mesh available for validation",
        )
        return GateReport(case_id=case_id, gates=gates)

    g3 = gate3_watertight(mesh)
    gates[GATE_3] = g3
    if g3.status == "fail":
        return GateReport(case_id=case_id, gates=gates)

    g4 = gate4_bbox(mesh, expected_dims)
    gates[GATE_4] = g4
    if g4.status == "fail":
        return GateReport(case_id=case_id, gates=gates)

    g5 = gate5_volume(mesh)
    gates[GATE_5] = g5

    return GateReport(case_id=case_id, gates=gates)
