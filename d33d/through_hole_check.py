"""Deterministic through-hole genus post-check (issue #386).

The post-check runs on the ok-render branch of the design loop, BEFORE
the ``score.perfect`` pass return — a blind pocket in place of a
requested through-hole is invisible to all five gate bits (the QA
2026-10-05 repro: a 5 mm pocket over an 80 mm block passed as "built
and checked"), so without this check a pocket passes silently.

Operator decision 2026-10-05 (binding): the rendered mesh's total genus
must EXCEED the parent version's baseline genus — ``0`` for a new
design, the imported part's own stored hole count for v1 on an import.
This REPLACES "genus ≥ 1", which would let a pocket pass on any part
that already has a hole (the plate has genus 3, the knob 1).

The module is deliberately separate from ``d33d.design_loop`` (the loop
orchestrates; this module owns the check's logic), mirroring
``d33d.screw_hole_check`` (the #317 pattern: a detection-only module
returning a tuple or ``None``, the caller folding it into the repair
dict). The shared mesh-topology helper from issue #395
(``d33d.part_mesh_topology.mesh_topology``) supplies the genus
measurement — one helper for both the import-path clean-skip and this
check.

The trigger runs against the ``request`` kwarg EXACTLY as the design
loop receives it (the operator decision 2026-10-05: the user message
plus any appended system instruction — the fill-and-recut instruction
contains no "through", so no stripping is needed).
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from d33d.part_holes import HOLE_NOUNS

logger = logging.getLogger(__name__)

__all__ = [
    "THROUGH_HOLE_INSTRUCTION",
    "is_through_request",
    "resolve_baseline_genus",
    "route_through_hole_repair",
    "through_hole_check",
]

#: The hyphenated through-hole tokens that fire on their own (no hole
#: noun required) — whole-word, case-insensitive.
_HYPHENATED_TOKENS = ("through-hole", "thru-hole")


def is_through_request(request: str) -> bool:
    """Does the CURRENT user message ask for a through-hole?

    Fires (case-insensitive, whole-word) when the message contains
    ``through`` together with a hole noun from ``part_holes.HOLE_NOUNS``
    (hole, holes, bore, counterbore), or the hyphenated token
    ``through-hole`` / ``thru-hole``.

    Never fires on ``through`` alone (``throughput``, ``thorough``,
    ``throughout`` — whole-word matching), nor on a hole noun without
    ``through`` (``blind hole``, ``pocket``, ``counterbore 5 mm deep``).
    SCAD comments and parameter names are never a trigger surface — the
    caller passes the loop's ``request`` argument and nothing else.
    """
    if not request:
        return False
    lower = request.lower()
    for token in _HYPHENATED_TOKENS:
        if token in lower:
            # Whole-word: the character immediately BEFORE the token (if
            # any) must not be ALPHANUMERIC — ``x-through-hole`` is a
            # different (non-through-hole) word, ``3-through-hole`` a
            # dimension, and a plain hyphenated compound (``a
            # through-hole``) fires like the bare token. The token's own
            # internal hyphen is not a boundary: it is part of the
            # token, not a separator between words.
            idx = lower.find(token)
            if idx == 0 or not lower[idx - 1].isalnum():
                return True
    words = set(lower.split())
    if "through" not in words:
        return False
    return any(noun in words for noun in HOLE_NOUNS)


def _load_and_split(stl: str) -> list[Any]:
    """Load ``stl`` and split it ONCE (``only_watertight=False``).

    ``merge_vertices()`` is MANDATORY before the split — production
    OpenSCAD STLs are face-disconnected and an unmerged split yields
    ZERO components (the established contract, see
    ``d33d.design_loop_events.bbox_from_render``). Load or split
    failures RAISE — the caller (``_rendered_genus``) wraps the call in
    its own ``try/except`` and abstains there (it is the sole caller).
    """
    import trimesh

    mesh = trimesh.load(stl, process=False)
    merged = mesh.to_mesh() if isinstance(mesh, trimesh.Scene) else mesh
    merged.merge_vertices()
    merged.update_faces(merged.nondegenerate_faces())
    return merged.split(only_watertight=False)


def _rendered_genus(stl: str) -> int | None:
    """The rendered mesh's total genus, via the #395 shared helper.

    ``None`` on a missing/unreadable STL, a split failure, or a split
    with zero watertight components (the abstain cases — the caller
    logs the single line and the candidate proceeds).

    ``except Exception`` (adversarial round 1, 2026-10-05): ANY
    unexpected exception (``TypeError`` / ``IndexError`` / ``MemoryError``
    from a malformed mesh) makes the check ABSTAIN — the design loop
    must never crash on a mesh the render worker already accepted.
    """
    from d33d.part_mesh_topology import mesh_topology

    path = Path(stl)
    if not path.is_file():
        return None
    try:
        components = _load_and_split(stl)
        watertight_bodies = sum(1 for c in components if c.is_watertight)
        if watertight_bodies == 0:
            logger.info("through-hole check abstained: no watertight components in %r", stl)
            return None
        # ``mesh_topology`` consults the ``merged`` argument only for the
        # boundary-loop count and winding consistency (both degrade
        # safely on an open mesh); the genus it returns comes straight
        # from ``components``, so any component is a valid merged-mesh
        # stand-in (the split pieces share the merged mesh's vertices).
        return mesh_topology(merged=components[0], components=components)["genus"]
    except Exception:
        logger.warning(
            "through-hole check abstained: STL %r could not be measured",
            stl,
            exc_info=True,
        )
        return None


#: The through-hole post-check's (issue #386, operator decision
#: 2026-10-05) repair instruction: the hole the user asked for does not
#: pass — the rendered mesh's genus did not rise above the baseline — so
#: the next iteration must cut it through the full thickness.
THROUGH_HOLE_INSTRUCTION = (
    "the hole does not pass all the way through the part — it must cut "
    "through the full thickness (a blind pocket or partial depth does "
    "not satisfy a through request; verify the cutting solid extends "
    "beyond both faces of the part)."
)


def resolve_baseline_genus(through_baseline_genus: int | None) -> int | None:
    """The baseline genus for the through-hole check (issue #386, operator
    decision 2026-10-05).

    * ``0`` — no explicit seam value: a NEW design (a baseline-less
      candidate; the ``design_loop_events`` chat seam omits the kwarg
      when the project has no part).
    * the explicit seam value — the PARENT's baseline genus, resolved by
      the caller: for a v1 on an import the GENUS OF THE STORED, REPAIRED
      PART MESH (``{repo}/versions/{v1}/part.stl`` — the mesh the render
      imports, never the report's ``hole_count``, which overstates the
      baseline whenever the import had open gaps), or the measured genus
      of the parent version's rendered mesh for v2+ edits (the seam
      measures the version's rendered STL). A missing/unreadable mesh is
      passed as ``-1`` (the unknown sentinel).
    * ``None`` (the check abstains) when the explicit value is corrupt —
      a fabricated baseline would make the gate lie. A non-integer (or
      bool) or negative count (the ``-1`` unknown sentinel) abstains the
      same way :func:`d33d.part_holes.part_has_hole_evidence` degrades
      it.
    """
    if through_baseline_genus is None:
        return 0
    if isinstance(through_baseline_genus, bool) or not isinstance(
        through_baseline_genus, int
    ):
        return None
    if through_baseline_genus < 0:
        return None
    return through_baseline_genus


def route_through_hole_repair(
    request: str,
    stl: str | None,
    through_baseline_genus: int | None,
    scad_source: str,
) -> tuple[str, str] | None:
    """Run the through-hole post-check and, on a miss, route the repair.

    The ok-render-branch seam the design loop calls (off the event loop):
    resolves the baseline, runs :func:`through_hole_check`, and on a
    detection tuple ``(baseline, genus)`` routes the EXISTING
    ``geometrically_wrong`` class through ``route_repair`` (no new class,
    no new error_class) — the #317 screw-hole pattern. Returns
    ``(evidence, directive.instruction)`` on a routed repair, ``None``
    when the check abstains or the hole passed (the candidate proceeds).

    A ``route_repair`` directive of ``None`` (unrepairable — the class is
    repairable by contract, so only a routing failure) also yields
    ``None``: the candidate proceeds rather than crashing the loop.
    """
    from d33d.failure_classes import ClassifiedFailure, route_repair

    baseline = resolve_baseline_genus(through_baseline_genus)
    det = through_hole_check(request, stl, baseline)
    if det is None:
        return None
    base_g, genus = det
    evidence = (
        f"rendered mesh genus {genus} does not exceed the "
        f"baseline genus {base_g} — the hole did not pass through"
    )
    classified = ClassifiedFailure(
        failure_class="geometrically_wrong",
        evidence=evidence,
        repairable=True,
    )
    directive = route_repair(classified=classified, scad_source=scad_source)
    if directive is None:
        return None
    return (evidence, THROUGH_HOLE_INSTRUCTION)


def through_hole_check(
    request: str,
    stl: str | None,
    baseline_genus: int | None,
) -> tuple[int, int] | None:
    """The through-hole post-check (issue #386, operator decision 2026-10-05).

    Runs on the ok-render branch of the loop, BEFORE the ``score.perfect``
    pass return (the loop calls :func:`route_through_hole_repair` — the
    thin wrapper that resolves the baseline and routes the repair).
    Returns ``None`` (abstain — nothing is fed to the next
    iteration) unless ALL of the following hold:

    * the ``request`` (the current user message, exactly as the loop
      receives it) asks for a through-hole (:func:`is_through_request`);
    * ``stl`` loads and splits with at least one watertight component;
    * ``baseline_genus`` is known (``None`` → abstain).

    Otherwise returns the DETECTION tuple ``(baseline_genus, genus)``
    when the rendered mesh's total genus does NOT exceed the baseline
    (a pocket over a new design: 0 ≤ 0; a pocket over a part whose own
    hole count already matched the render: the hole did not pass
    through). ``genus > baseline`` → ``None`` (the pass condition).
    The caller (``d33d.design_loop``) folds this into the repair dict —
    the EXISTING ``geometrically_wrong`` class, no new class — and routes
    it through ``route_repair``.
    """
    if not is_through_request(request):
        return None
    if baseline_genus is None:
        logger.info("through-hole check abstained: no baseline genus")
        return None
    if not isinstance(stl, str) or not stl:
        logger.info("through-hole check abstained: render carries no STL path")
        return None
    genus = _rendered_genus(stl)
    if genus is None:
        return None
    if genus > baseline_genus:
        return None
    return (baseline_genus, genus)
