"""Run report aggregator (issue #9, workstream task-harness).

Aggregates the per-case :class:`d33d.evals.harness.CaseOutcome` rows
(gate results + judge verdicts) into a single run report, and
implements :func:`select_best_candidate` — the "best-candidate +
reason" shape the design loop's FINALIZE uses (the loop's terminal
outcome is the best-scoring candidate + a structured failure reason,
never a silent last-attempt).

Design rules:

* **The report is a pure aggregation.** It never re-runs a gate,
  re-calls the judge, or re-derives a failure class — every value
  comes from the per-case outcome the harness produced (the closed
  ``error_class`` enum is respected as-is; the report maps outcome
  classes back onto the design-loop taxonomy via
  :func:`d33d.evals.gates.map_to_design_classes`).
* **The best candidate is the best SCORING candidate** — a case that
  passed its gates AND its judge (when one ran) outranks a case that
  passed gates only, which outranks a failed case. Ties break on
  (gates-passed count, then case_id) for determinism.
* **JSON-serialisable.** ``to_dict`` / ``to_json`` produce the shape
  the runner writes to stdout / a file; no ``Path`` or dataclass
  objects leak into the payload.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from d33d.evals.case_schema import KIND_PRECHECK_GATES
from d33d.evals.gates import map_to_design_classes
from d33d.evals.harness import CaseOutcome

if TYPE_CHECKING:
    from d33d.evals.gates import GateResult

#: The base gate labels, in the pinned 1->7 order — the gates a case
#: may DECLARE in ``gate_expectations`` (see
#: ``d33d.evals.case_schema.GATE_NAMES``).
BASE_GATE_ORDER: tuple[str, ...] = (
    "compile",
    "stl_export",
    "watertight_winding",
    "bbox_dims",
    "volume_faces",
    "slice_dry_run",
    "region_containment",
)


def case_gate_order(case_kind: str) -> tuple[str, ...]:
    """The ordered gate labels for a case's report row (issue #340).

    The kind's pre-check gate (``import_guard`` for ``imported_part``
    cases — see ``d33d.evals.case_schema.KIND_PRECHECK_GATES``) is
    ordered FIRST, before the base 1->7 gates it precedes in the gate
    phase; kinds without a pre-check get the base order unchanged. The
    base order is :data:`BASE_GATE_ORDER` (the 1->7 gates; the kind's
    pre-check is prepended when present, so the kind's full order is
    longer than the base). The
    report's per-case row carries at most these keys (absent gates
    were not reached — a short-circuit).
    """
    pre = KIND_PRECHECK_GATES.get(case_kind)
    return (pre, *BASE_GATE_ORDER) if pre is not None else BASE_GATE_ORDER


@dataclass(frozen=True)
class BestCandidate:
    """The run's best candidate — the "best-candidate + reason" shape.

    ``case_id`` is the winning case (``None`` when no case passed).
    ``reason`` is a one-line human summary: the best case's judge
    reason when one ran (or its gate summary when it did not), and for
    a no-pass run the failing case's failure class + detail (so the
    report always names *why* nothing passed, mirroring the design
    loop's ``failure_reason`` — structured, never free text).
    """

    case_id: str | None
    reason: str
    failure_class: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case_id": self.case_id,
            "reason": self.reason,
            "failure_class": self.failure_class,
            "detail": self.detail,
        }


def _case_sort_key(outcome: CaseOutcome) -> tuple[int, int, str]:
    """The best-candidate ranking key.

    Primary: passed (a full pass beats a gates-only pass beats a fail).
    Secondary: the count of gates that passed (more evidence, better).
    Tertiary: case_id (deterministic tiebreak — no set-order, no
    wall-clock).
    """
    full_pass = 1 if outcome.ok else 0
    if outcome.ok:
        passed = 2
    elif outcome.gates_ok:
        passed = 1
    else:
        passed = 0
    gates_passed = sum(1 for g in outcome.gates.values() if g.status == "pass")
    return (passed, full_pass, gates_passed, outcome.case_id)


def select_best_candidate(outcomes: list[CaseOutcome]) -> BestCandidate:
    """Pick the run's best candidate (the "best-candidate + reason"
    shape the design loop uses).

    The winner is the highest-scoring case: a full pass (gates + judge)
    outranks a gates-only pass, which outranks a fail. Among ties the
    case with more gates passing wins, then case_id (deterministic).

    When no case passed, the best candidate is ``None`` and the reason
    names the *weakest* failure — the case with the most gates passing
    (i.e. the closest to a pass) with its failure class + detail — so
    the report always answers "why did nothing pass" with a structured
    class, never free text.
    """
    if not outcomes:
        return BestCandidate(
            case_id=None,
            reason="no cases ran",
            failure_class=None,
        )

    best = max(outcomes, key=_case_sort_key)
    if best.ok:
        reason = best.judge.reason if best.judge is not None else "gates passed"
        return BestCandidate(
            case_id=best.case_id,
            reason=reason,
            failure_class=None,
            detail=best.to_dict(),
        )

    # No full pass — the "best" is the closest-to-pass failing case.
    closest = max(
        (o for o in outcomes if not o.ok),
        key=lambda o: (
            sum(1 for g in o.gates.values() if g.status == "pass"),
            o.case_id,
        ),
    )
    return BestCandidate(
        case_id=closest.case_id,
        reason=f"{closest.failure_class or 'unclassified_syntax_error'}: {closest.detail}",
        failure_class=closest.failure_class,
        detail=closest.to_dict(),
    )


@dataclass(frozen=True)
class RunReport:
    """The aggregated run report — one per eval run.

    ``outcomes`` is the per-case table (case_id -> row). ``summary``
    carries the counts (total / passed / failed / na-gates) and the
    failure-class histogram (the closed-enum classes that fired, mapped
    onto the design-loop taxonomy via :func:`map_to_design_classes`
    so the report reads in the original vocabulary). ``best`` is the
    :class:`BestCandidate`. ``ts`` is the run's UTC timestamp
    (deterministic in tests via the ``ts`` argument).
    """

    ts: str
    total: int
    passed: int
    failed: int
    outcomes: dict[str, dict[str, Any]] = field(default_factory=dict)
    failure_class_counts: dict[str, int] = field(default_factory=dict)
    best: BestCandidate | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable report (the shape the runner writes)."""
        return {
            "ts": self.ts,
            "summary": {
                "total": self.total,
                "passed": self.passed,
                "failed": self.failed,
                "failure_class_counts": dict(self.failure_class_counts),
            },
            "best": self.best.to_dict() if self.best is not None else None,
            "cases": self.outcomes,
        }

    def to_json(self) -> str:
        """The report as a single JSON string (sorted keys, stable)."""
        return json.dumps(self.to_dict(), sort_keys=True, ensure_ascii=False, indent=2)


def _order_gate_row(outcome: CaseOutcome) -> None:
    """Re-order an outcome's ``gates`` mapping in the kind's gate order
    (in place; ``CaseOutcome`` is frozen but its ``gates`` dict is not)
    so :meth:`CaseOutcome.to_dict` emits the row in the order the gate
    phase ran."""
    ordered = case_gate_order(outcome.kind)
    by_name = dict(outcome.gates)
    ordered_gates: dict[str, GateResult] = {}
    for name in ordered:
        if name in by_name:
            ordered_gates[name] = by_name.pop(name)
    ordered_gates.update(by_name)  # any unexpected gate, last
    outcome.gates.clear()
    outcome.gates.update(ordered_gates)


def build_report(
    outcomes: list[CaseOutcome],
    *,
    ts: str | None = None,
) -> RunReport:
    """Aggregate per-case outcomes into a :class:`RunReport`.

    The summary counts a case as *passed* when its overall verdict
    (``outcome.ok``) is true (gates ok AND judge passed, when the judge
    ran); every other case is *failed*. The failure-class histogram
    maps each failing case's ``failure_class`` through
    :func:`map_to_design_classes` (the outcome classes collapse into
    ``unclassified_syntax_error`` in the design-loop vocabulary) so the
    report reads in the taxonomy the design loop already uses.

    Each row's ``gates`` mapping is ordered by :func:`case_gate_order`
    (the kind's pre-check gate first — ``import_guard`` for
    ``imported_part`` — then the base 1->7 order), so the row reads in
    the order the gate phase ran.

    ``ts`` defaults to the current UTC time (deterministic in tests via
    the argument).

    Side effect: each outcome's ``gates`` dict is reordered in place to
    the kind's gate order before the row is serialised (:func:`_order_gate_row`).
    """
    rows: dict[str, dict[str, Any]] = {}
    counts: dict[str, int] = {}
    passed = 0
    for outcome in outcomes:
        _order_gate_row(outcome)
        rows[outcome.case_id] = outcome.to_dict()
        if outcome.ok:
            passed += 1
            continue
        cls = outcome.failure_class or "unclassified_syntax_error"
        mapped = map_to_design_classes(frozenset({cls}))  # type: ignore[arg-type]
        mapped_cls = next(iter(mapped.values())) if mapped else cls
        counts[mapped_cls] = counts.get(mapped_cls, 0) + 1

    best = select_best_candidate(outcomes)
    return RunReport(
        ts=ts or datetime.now(UTC).isoformat(),
        total=len(outcomes),
        passed=passed,
        failed=len(outcomes) - passed,
        outcomes=rows,
        failure_class_counts=counts,
        best=best,
    )
