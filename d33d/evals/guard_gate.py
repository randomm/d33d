"""The ``import_guard`` pre-check gate's failure row (issue #340).

The imported-part pre-check gate (run in
``d33d.evals.harness.run_case_gates``) only ever fails on a good render
(the candidate violates the import contract), so its only taggable class
is ``artifact_error`` — declared in ``d33d.evals.gates``'
``GATE_TAGGABLE_CLASSES`` table and checked through the same
``assert_taggable`` invariant as every other gate.

This module is deliberately separate from ``gates.py``: it imports the
invariant and the result container from ``gates`` (``gates`` does not
import this module back, so there is no cycle).
"""

from __future__ import annotations

from d33d.evals.gates import GateResult, assert_taggable

#: The pre-check gate's name (matches the ``GATE_TAGGABLE_CLASSES`` key
#: in ``d33d.evals.gates``).
IMPORT_GUARD_GATE = "import_guard"


def import_guard_result(reason: str, detail: str) -> GateResult:
    """The ``import_guard`` gate's failure row (issue #340).

    The guard's violation (the candidate failed to import the seeded
    part at the settled scale, or resized it) is an artifact-quality
    defect on a successfully-rendered candidate — always tagged
    ``artifact_error``, checked at construction time via
    :func:`assert_taggable` against this gate's own
    ``GATE_TAGGABLE_CLASSES["import_guard"]`` entry in the ``gates``
    module. ``import_guard`` is a pre-check gate (enforced before the
    remaining declared gates run); it is NOT one of the
    ``IMPLEMENTED_GATE_NAMES`` that ``gates.py`` dispatches. The design loop
    (``d33d/design_loop.py``) routes the same violation as
    ``geometrically_wrong``; the eval tags it ``artifact_error`` because the
    eval's closed report taxonomy has no ``geometrically_wrong``.
    """
    assert_taggable(IMPORT_GUARD_GATE, "artifact_error")
    return GateResult(
        gate=IMPORT_GUARD_GATE,
        status="fail",
        failure_class="artifact_error",
        detail=f"{reason}: {detail}",
    )
