"""Post-check repair reasons: the neutral vocabulary shared by the checks,
the design loop, the failure archive and the identical-repair tracker.

Imports nothing from ``d33d.evals`` (issue #436): the checks and the loop
depend on this module, and the evals package depends on them, so the
constants must live below both.
"""

from __future__ import annotations

__all__ = [
    "HOLLOW_SOLID_REASON",
    "MESH_UNCHANGED_REASON",
    "POST_CHECK_REASONS",
    "SCREW_CLEARANCE_REASON",
    "STACK_HEIGHT_REASON",
    "THROUGH_HOLE_REASON",
    "VISUAL_REVIEW_FAILED_REASON",
]

# Issue #432: loop-level reasons for the deterministic post-checks. Each
# rides the existing ``geometrically_wrong`` class on the iteration's repair
# (no new class, no new error_class).
THROUGH_HOLE_REASON = "through_hole_missing"
SCREW_CLEARANCE_REASON = "screw_clearance_wrong"
STACK_HEIGHT_REASON = "stack_height_mismatch"
HOLLOW_SOLID_REASON = "hollow_solid"

# Issue #419: the candidate's rendered mesh is GEOMETRICALLY identical to the
# parent's. The detection lives in ``d33d.unchanged_mesh_check``.
MESH_UNCHANGED_REASON = "mesh_unchanged"

# Issue #435: the visual review (request versus render, critique role) judged
# a gate-passing candidate not to match the request. Rides the existing
# ``geometrically_wrong`` class; the reason is the loop-level exhaustion code
# when the last iteration is the one judged. NOT a post-check: it is absent
# from POST_CHECK_REASONS, so the #432 identical-repair stop ignores it.
VISUAL_REVIEW_FAILED_REASON = "visual_review_failed"

#: Every post-check repair reason the loop surfaces as a terminal reason
#: (the visual review reason is deliberately excluded; see above).
POST_CHECK_REASONS: frozenset[str] = frozenset(
    {
        MESH_UNCHANGED_REASON,
        THROUGH_HOLE_REASON,
        SCREW_CLEARANCE_REASON,
        STACK_HEIGHT_REASON,
        HOLLOW_SOLID_REASON,
    }
)
