"""The checks a passing candidate actually ran (issue #435).

The pass card says what was really checked. Each key below is listed only when
its check EXECUTED on the kept candidate — an abstaining post-check (no
applicable request, no declared stack, no parent mesh, unreadable STL) is never
listed. The predicates mirror each check's own abstain ladder without deciding
anything; the decision stays with the check.

Order is the closed tuple :data:`CHECK_KEYS` — the card and the done frame
both read it in this order.
"""

from __future__ import annotations

from typing import Any

from d33d.hollow_check import hollow_check_ran
from d33d.screw_hole_check import screw_check_ran
from d33d.stack_height_check import declared_stack_sum
from d33d.through_hole_check import through_hole_check_ran

__all__ = ["CHECK_KEYS", "checks_that_ran"]

#: The closed, ordered set of check keys a pass can record.
CHECK_KEYS: tuple[str, ...] = (
    "size",
    "watertight",
    "screw_clearance",
    "through_holes",
    "stack_height",
    "change_detected",
    "hollow",
)


def checks_that_ran(
    *,
    request: str | None,
    stl: str | None,
    render_ok: bool,
    bbox: Any | None,
    bbox_abstained: bool,
    scad_source: str,
    params: dict[str, float],
    param_meta: dict[str, Any],
    part_scale: Any | None,
    parent_mesh_stl: str | None,
    through_baseline_genus: int | None,
) -> tuple[str, ...]:
    """The keys of every check that executed on this passing candidate.

    * ``size`` — the bbox gate measured the render and did not abstain.
    * ``watertight`` — the render is ``ok`` (the render worker validates
      watertightness before it reports ``ok``).
    * ``screw_clearance`` / ``through_holes`` / ``hollow`` — the check's own
      applicability and readable-input conditions hold.
    * ``stack_height`` — a declared stack is readable and a measured Z exists
      (the loop skips it for scaled-import parts).
    * ``change_detected`` — a parent mesh exists and a candidate STL exists.
      (The check may still abstain if the parent fingerprint is unreadable;
      that edge is not mirrored here.)
    """
    request_text = request or ""
    ran: list[str] = []
    if bbox is not None and not bbox_abstained:
        ran.append("size")
    if render_ok:
        ran.append("watertight")
    if screw_check_ran(request_text, params, param_meta):
        ran.append("screw_clearance")
    if through_hole_check_ran(request_text, stl, through_baseline_genus):
        ran.append("through_holes")
    if (
        part_scale is None
        and bbox is not None
        and declared_stack_sum(scad_source) is not None
    ):
        ran.append("stack_height")
    if parent_mesh_stl is not None and bool(stl):
        ran.append("change_detected")
    if part_scale is None and hollow_check_ran(request_text, stl):
        ran.append("hollow")
    return tuple(key for key in CHECK_KEYS if key in ran)
