"""The #351 hole-evidence gate for the fill-and-recut offer.

Issue #351 (operator decisions 1–4): the fill-and-recut offer is made
only when the import stored evidence the part actually has a hole. This
module is the small, self-contained half of that gate — the stored-fact
reader, the gated noun set, and the honest no-hole reply — factored out
of ``d33d.fill_recut`` (which owns the trigger, the offer lifecycle, and
the boundary copy). It also hosts the import-time topology helper
:func:`watertight_genus` (the closed-body half of ``d33d.part_mesh``'s
``hole_count``, measured on the pre-repair merged mesh). Both offer
paths (the chat pre-route and the region-edit seam) read the stored
``part_report.hole_count`` through :func:`part_has_hole_evidence`; the
mesh work happens once at import (``d33d.part_mesh.parse_and_repair``),
never at offer time.
"""

from __future__ import annotations

from typing import Any

import numpy as np
import trimesh

#: Issue #351 (operator decision 2) — the honest no-hole reply TEMPLATE:
#: a hole/bore/counterbore resize request on a part the import measured to
#: carry ZERO holes (``part_report.hole_count == 0``, the stored fact
#: computed once at import — the pre-route reads the stored fact, never
#: re-parses the mesh). The offer is NOT made for a feature the part does
#: not have, and the reply says so plainly. The ``{noun}`` slot is the
#: TRIGGERING noun the user asked about (``no_hole_reply(trigger["noun"]``)
#: — "a bore" and "a counterbore" are both fine), never a hardcoded
#: "hole". A verbatim template copy of the copy.ts ``fillRecut.noHole``
#: sentence — the parity test in ``tests/test_projects.py`` pins the
#: two-way agreement against ``web/src/copy.ts``. The loop must not run —
#: this is an answer, not a change request.
_NO_HOLE_REPLY_TEMPLATE = (
    "I don't see a {noun} on the part you brought — want me to drill one?"
)


def no_hole_reply(noun: str) -> str:
    """The honest no-hole reply for the TRIGGERING feature ``noun`` (the
    user asked about a hole/bore/counterbore, the part was measured with
    zero — say so with the noun the user used, never a hardcoded
    "hole")."""
    return _NO_HOLE_REPLY_TEMPLATE.format(noun=noun)


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


def watertight_genus(components: list[trimesh.Trimesh]) -> int:
    """Total genus across the WATERTIGHT bodies of an already-split mesh.

    ``components`` is the caller's ``mesh.split(only_watertight=False)``
    list (the caller splits ONCE — the bodies count and this genus both
    read the same split, never two). Genus is the closed-body hole count:
    for a closed orientable body, ``genus = (2 - euler_number) / 2`
    (sphere 0, ring 1, torus-with-2 2). Only watertight components count
    (a gapped remainder is an OPEN edge the boundary-loop count owns, not
    a closed hole). Cheap — an euler_number is a vertex/edge/face count, no
    geometry passes — but the caller wraps the call in a guard (any
    exception falls back to the boundary-loop count alone, never a crash).
    """
    genus = 0
    for body in components:
        if body.is_watertight:
            g = (2 - int(body.euler_number)) // 2
            genus += max(0, g)
    return genus


def _boundary_loops(mesh: trimesh.Trimesh) -> int:
    """The number of boundary loops (connected open-edge components) on a
    mesh — one loop per gap. The repair report's ``gaps_closed`` is the
    count BEFORE repair minus AFTER (pymeshfix closes them). The OPEN-hole
    half of ``d33d.part_mesh``'s ``hole_count`` (``gaps_before + genus``),
    measured on the PRE-REPAIR merged mesh."""
    edges, counts = np.unique(mesh.edges_sorted, axis=0, return_counts=True)
    open_edges = edges[counts == 1]
    if len(open_edges) == 0:
        return 0
    adj: dict[int, set[int]] = {i: set() for i in range(len(open_edges))}
    pos: dict[tuple[int, int], int] = {}
    for i, (a, b) in enumerate(open_edges):
        pos[(int(a), int(b))] = i
    for i, (a, b) in enumerate(open_edges):
        j = pos.get((int(b), int(a)))
        if j is not None:
            adj[i].add(j)
            adj[j].add(i)
    seen: set[int] = set()
    n = 0
    for i in range(len(open_edges)):
        if i in seen:
            continue
        n += 1
        stack = [i]
        while stack:
            x = stack.pop()
            if x in seen:
                continue
            seen.add(x)
            for y in adj[x]:
                if y not in seen:
                    stack.append(y)
    return n


__all__ = [
    "HOLE_NOUNS",
    "no_hole_reply",
    "part_has_hole_evidence",
    "watertight_genus",
]
