"""The fill-and-recut pre-route for the REGION-EDIT seam (issue #338,
operator decisions 5–6).

``d33d.app``'s ``POST /api/projects/{id}/region-edits`` calls
:func:`fill_recut_region_edit` BEFORE the loop when the project has an
imported part whose units are assumed or settled — the SAME trigger the
chat route uses (``fill_recut.fill_recut_trigger``), evaluated with the
SAME caller-side rules.

The module hosts the region-edit-only pieces of the fill-recut
pre-route — the region seam entry point, the no-normal degradation
copy, and the face-normal length tolerance the wire validator reads.
Everything shared with the chat route stays in :mod:`d33d.fill_recut`.
"""

from __future__ import annotations

from typing import Any

from d33d.fill_recut import (
    FRILL_DECLINE_REPLY,
    boundary_sentence,
    fill_and_recut_instruction,
    fill_recut_trigger,
    is_clean_no,
    is_clean_yes,
    own_feature_names,
)

#: Issue #338 (operator decision 6) — the region-edit no-normal
#: degradation copy (issue #332's axis-dependent offer is impossible
#: without a face normal; the reply says plainly that the feature came
#: with the file and that the axis cannot be read from the pick). A
#: verbatim copy of the copy.ts sentence — the parity test pins the
#: two-way agreement (the #332 pin's pattern).
FILL_RECUT_NO_NORMAL_REPLY = (
    "That feature came with your file, so I can't resize it directly — "
    "the file has no parameters for me to change. I can't tell that "
    "feature's axis from where you pointed — pin a flat face on it and "
    "I'll offer to fill it and recut it on the same axis."
)

#: The region-edit wire's face-normal length tolerance (issue #338's
#: operator decision 4): the SPA sends a unit world-space normal; a
#: length within 1±this tolerance is accepted, outside it is a 422
#: (a normal that is not unit is a client defect the loop cannot use).
NORMAL_LENGTH_TOLERANCE = 0.01


def fill_recut_region_edit(
    app: Any,
    project_id: int,
    instruction: str,
    face_normal: tuple[float, float, float] | None = None,
) -> dict[str, Any] | None:
    """The fill-and-recut pre-route for the REGION-EDIT seam (issue
    #338's operator decisions 5–6).

    ``d33d.app``'s ``POST /api/projects/{id}/region-edits`` calls this
    BEFORE the loop when the project has an imported part whose units
    are assumed or settled — the SAME trigger the chat route uses
    (``fill_recut_trigger`` over the instruction text), evaluated with
    the SAME caller-side rules: the part exists and is assumed/settled
    (a) and the noun is not a token of the design's own current params
    or labels (d).

    Returns ``{"kind": "answer", "answer": <sentence>, "run_loop":
    bool, ...}`` when the pre-route handles the turn (the caller
    registers ``answer`` as a ``kind: "answer"`` done frame and, when
    ``run_loop`` is true, runs the design loop with the ``instruction``
    field appended to the request text), else ``None`` (the caller
    proceeds to the design loop exactly as today).

    The handled cases mirror the chat route's:

    * a LIVE fill-recut offer (``kind: "fill_recut"``): a clean
      acceptance clears the offer (the caller's job — it clears once
      the event source is registered, like the chat route) and runs
      the loop with the explicit fill-and-recut instruction; a clean
      decline clears the offer and replies quietly; anything else
      supersedes the offer (cleared) and re-evaluates the instruction
      as a fresh trigger;
    * a fresh trigger with a face normal: the boundary sentence (the
      same ``boundary_sentence`` template) and the offer recorded
      server-side — the offer carries the pick's face normal as its
      ``axis`` so the accepted turn's instruction names the axis
      (operator decision 5: "…on the same axis");
    * a fresh trigger WITHOUT a face normal: the axis-dependent offer
      is impossible — the reply is :data:`FILL_RECUT_NO_NORMAL_REPLY`
      (the #338 operator decision 6 copy) and NO offer is stored, NO
      loop runs (operator decision 6: no offer, no buttons, no loop).
    """
    from d33d.part_http import part_public

    row = app.state.conn.get_project(project_id)
    if row is None:
        return None
    part = part_public(row) if row.get("part_filename") else None
    if part is None or part.get("unit_status") not in ("assumed", "settled"):
        return None

    versions = app.state.versions
    pending = versions.get_pending_offer(project_id)
    if pending is not None and pending.get("kind") == "fill_recut":
        if is_clean_yes(instruction):
            return {
                "kind": "answer",
                "answer": None,
                "run_loop": True,
                "instruction": fill_and_recut_instruction(pending),
                "accepted_offer": pending,
            }
        if is_clean_no(instruction):
            versions.set_pending_offer(project_id, None)
            return {
                "kind": "answer",
                "answer": FRILL_DECLINE_REPLY,
                "run_loop": False,
            }
        versions.set_pending_offer(project_id, None)
        pending = None

    if pending is None:
        trigger = fill_recut_trigger(instruction)
        if trigger is not None:
            own = own_feature_names(versions.latest_version(project_id))
            if trigger["noun"] not in own:
                axis = (
                    (float(v) for v in face_normal)
                    if face_normal is not None
                    else None
                )
                if axis is None:
                    return {
                        "kind": "answer",
                        "answer": FILL_RECUT_NO_NORMAL_REPLY,
                        "run_loop": False,
                    }
                versions.set_pending_offer(
                    project_id,
                    {
                        "kind": "fill_recut",
                        "noun": trigger["noun"],
                        "size": trigger["size"],
                        "axis": list(axis),
                    },
                )
                sentence = boundary_sentence(
                    trigger["noun"],
                    trigger["size"],
                    move=trigger["move"],
                    move_distance_mm=trigger.get("move_distance"),
                    move_direction=trigger.get("direction"),
                )
                return {"kind": "answer", "answer": sentence, "run_loop": False}
    return None


__all__ = [
    "FILL_RECUT_NO_NORMAL_REPLY",
    "NORMAL_LENGTH_TOLERANCE",
    "fill_recut_region_edit",
]
