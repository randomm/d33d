"""Near-hollow post-check (issue #436).

An ok render whose solid volume is under 10% of its bounding-box volume is
almost always a shell the model did not ask for (a box that came out as a
thin skin, or a lid that lost its slab). The check fires unless the
request names a shape that is legitimately thin or hollow.

The request exemption is a word list (case-insensitive, plurals and -es
forms included). A request naming no shape at all ("a box") is NOT exempt.
The check reads the rendered STL only; it abstains when the STL cannot be
loaded or the bounding box is degenerate.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from d33d.evals.failure_capture import HOLLOW_SOLID_REASON
from d33d.unchanged_mesh_check import _load_mesh, _volume

logger = logging.getLogger(__name__)

__all__ = [
    "HOLLOW_INSTRUCTION",
    "HOLLOW_RATIO",
    "HOLLOW_SOLID_REASON",
    "hollow_solid_check",
    "is_hollow_request",
]

#: Solid volume / bounding-box volume below this is a near-hollow result.
HOLLOW_RATIO = 0.10

_HOLLOW_WORDS_RE = re.compile(
    r"\b(shell|frame|ring|tube|wire|mesh|lattice|bracket|hook|spring|hollow)"
    r"(s|es)?\b",
    re.IGNORECASE,
)

HOLLOW_INSTRUCTION = (
    "The rendered solid is almost empty compared with its bounding box — "
    "most of the volume is missing, so the part is a thin skin rather than "
    "the solid the request describes. Make the body solid: check that the "
    "main shape is a filled primitive or a union, not a difference() that "
    "removed most of the material, and that no wall or floor was dropped. "
    "Only keep the part thin or hollow if the request asks for a shell, "
    "frame, ring, tube, wire, mesh, lattice, bracket, hook, spring or "
    "hollow form."
)


def is_hollow_request(request: str) -> bool:
    """True when the request names a thin or hollow form (exempt)."""
    return bool(_HOLLOW_WORDS_RE.search(request or ""))


def _bbox_volume(mesh: Any) -> float | None:
    try:
        extents = [float(e) for e in mesh.bounding_box.extents]
    except (AttributeError, TypeError, ValueError):
        return None
    vol = extents[0] * extents[1] * extents[2]
    return vol if vol > 0 else None


def hollow_solid_check(request: str, stl: str | None) -> tuple[str, str] | None:
    """``(evidence, instruction)`` when the rendered solid is near-hollow
    and the request is not exempt; ``None`` when the check passes or
    abstains."""
    if is_hollow_request(request):
        return None
    mesh = _load_mesh(stl) if stl else None
    if mesh is None:
        return None
    solid = _volume(mesh)
    box = _bbox_volume(mesh)
    if solid is None or box is None or solid <= 0:
        return None
    ratio = solid / box
    if ratio >= HOLLOW_RATIO:
        return None
    evidence = (
        f"solid volume {solid:,.0f} mm³ is {ratio * 100:.1f}% of the "
        "bounding box"
    )
    return evidence, HOLLOW_INSTRUCTION
