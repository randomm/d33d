"""The #351 hole-evidence gate for the fill-and-recut offer.

Issue #351 (operator decisions 1–4): the fill-and-recut offer is made
only when the import stored evidence the part actually has a hole. This
module is the small, self-contained half of that gate — the stored-fact
reader, the gated noun set, and the honest no-hole reply — factored out
of ``d33d.fill_recut`` (which owns the trigger, the offer lifecycle, and
the boundary copy). Both offer paths (the chat pre-route and the
region-edit seam) read the stored ``part_report.hole_count`` through
:func:`part_has_hole_evidence`; the mesh work happens once at import
(``d33d.part_mesh.parse_and_repair``), never here.
"""

from __future__ import annotations

from typing import Any

import trimesh

#: Issue #351 (operator decision 2) — the honest no-hole reply: a
#: hole/bore/counterbore resize request on a part the import measured to
#: carry ZERO holes (``part_report.hole_count == 0``, the stored fact
#: computed once at import — the pre-route reads the stored fact, never
#: re-parses the mesh). The offer is NOT made for a feature the part does
#: not have, and the reply says so plainly (a verbatim copy of the
#: copy.ts sentence — the parity test in ``tests/test_projects.py`` pins
#: the two-way agreement against ``web/src/copy.ts``). The loop must not
#: run — this is an answer, not a change request.
FILL_RECUT_NO_HOLE_REPLY = (
    "I don't see a hole on the part you brought — want me to drill one?"
)


def part_has_hole_evidence(part: dict[str, Any] | None) -> bool | None:
    """Issue #351 (operator decisions 1–4) — the stored hole evidence for
    a part: the ``hole_count`` key of the part's stored import report
    (``part_report``), read through the ``part_public`` dict's ``report``
    field.

    The gate the caller applies: a fresh fill-and-recut trigger on a
    hole-family noun (hole/bore/counterbore) is offered ONLY when this
    returns ``True`` (the import measured at least one hole). ``0`` →
    ``False`` (the honest no-hole reply, no offer). ``None`` (no report
    — legacy row or a part imported before the key existed — or an
    unparseable/corrupt report that ``part_public`` already reduced to
    ``None``; the stored fact was never re-parsed here) → ``None``
    (UNKNOWN — keep today's behaviour: the offer fires, as it does today
    for every part row without a report).

    A negative count is a corrupt value and degrades to ``None`` (unknown)
    — never ``False`` (a false honest-no-hole reply on a possibly-holey
    part is a lie; a false OFFER on a known-holey part is today's
    behaviour, which the gate preserves for unknowns). The trigger itself
    stays purely lexical; this is the separate caller-side evidence check
    that consumes the stored fact (the trigger is never passed mesh
    state). The hole-count computation itself lives at import
    (``d33d.part_mesh``, task-a) — this reader does no mesh work.
    """
    report = (part or {}).get("report")
    if not isinstance(report, dict):
        return None
    count = report.get("hole_count")
    if count is None:
        return None
    if isinstance(count, bool) or not isinstance(count, int):
        return None
    if count < 0:
        return None
    return count > 0


#: The hole-family feature nouns the #351 evidence gate applies to
#: (operator decision 4: the gate is hole/bore/counterbore only — the
#: mesh signal can only speak about holes, and other nouns keep current
#: behaviour regardless of the stored fact).
HOLE_NOUNS = frozenset({"hole", "holes", "bore", "counterbore"})


def watertight_genus(mesh: trimesh.Trimesh) -> int:
    """Total genus across the watertight connected bodies of ``mesh``.

    Genus is the closed-body hole count: for a closed orientable body,
    ``genus = (2 - euler_number) / 2`` (sphere 0, ring 1, torus-with-2 2).
    Summed over every watertight component (``split(only_watertight=False)``
    so a gapped remainder still contributes its closed bodies, not the whole
    mesh as one). Cheap — an euler_number is a vertex/edge/face count, no
    geometry passes — but the caller wraps it in a guard (any exception
    falls back to the boundary-loop count alone, never a crash).
    """
    genus = 0
    for body in mesh.split(only_watertight=False):
        if body.is_watertight:
            body.merge_vertices()
            g = (2 - int(body.euler_number)) // 2
            genus += max(0, g)
    return genus


__all__ = [
    "FILL_RECUT_NO_HOLE_REPLY",
    "HOLE_NOUNS",
    "part_has_hole_evidence",
    "watertight_genus",
]
