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

__all__ = ["is_through_request", "through_hole_check"]

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
    """
    from d33d.part_mesh_topology import mesh_topology

    path = Path(stl)
    if not path.is_file():
        return None
    try:
        components = _load_and_split(stl)
    except (OSError, ValueError, RuntimeError):
        logger.info("through-hole check abstained: STL %r unreadable", stl)
        return None
    if not components:
        logger.info("through-hole check abstained: no watertight components in %r", stl)
        return None
    try:
        watertight_bodies = sum(1 for c in components if c.is_watertight)
        if watertight_bodies == 0:
            logger.info(
                "through-hole check abstained: zero watertight components in %r",
                stl,
            )
            return None
        # ``mesh_topology`` consults the ``merged`` argument only for the
        # boundary-loop count and winding consistency (both degrade
        # safely on an open mesh); the genus it returns comes straight
        # from ``components``, so any component is a valid merged-mesh
        # stand-in (the split pieces share the merged mesh's vertices).
        return mesh_topology(merged=components[0], components=components)["genus"]
    except (OSError, ValueError, RuntimeError):
        logger.info("through-hole check abstained: topology measurement failed for %r", stl)
        return None


def through_hole_check(
    request: str,
    stl: str | None,
    baseline_genus: int | None,
) -> tuple[int, int] | None:
    """The through-hole post-check (issue #386, operator decision 2026-10-05).

    Runs on the ok-render branch of the loop, BEFORE the ``score.perfect``
    pass return. Returns ``None`` (abstain — nothing is fed to the next
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
