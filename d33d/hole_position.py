"""Positional hole resolution for the fill-and-recut offer (issue #437).

"the top right hole", "bottom left", "the left one", "the corner hole"
name a measured hole by where it sits on the part's XY bounds. The
reference is the bounds' corner point (both a vertical and a horizontal
word), the bounds' edge line (one word), or the nearest of the four
corners (the word "corner" alone). The nearest hole by that metric wins;
when the best two are within :data:`AMBIGUITY_MM` of each other the pick
is a guess, so the caller falls back to the point-at reply.

The #414 centre threshold does NOT apply here: a positional phrase always
names the nearest hole, however far it is.
"""

from __future__ import annotations

import math
import re
from typing import Any

#: Two best candidates closer than this (mm) are a tie → point-at.
AMBIGUITY_MM = 1.0

#: A viewer pick counts as ON a hole when its XY distance from the hole's
#: centre is within the hole's radius plus this tolerance (mm). Further
#: away the pick names no hole → point-at, never a Yes for a distant one.
PICK_TOLERANCE_MM = 2.0

_VERTICAL_RE = re.compile(r"\b(top|upper|bottom|lower)\b", re.IGNORECASE)
_HORIZONTAL_RE = re.compile(r"\b(left|right)\b", re.IGNORECASE)
_CORNER_RE = re.compile(r"\bcorner\b", re.IGNORECASE)


def positional_words(message: str) -> tuple[str | None, str | None, bool] | None:
    """The positional words in ``message`` as ``(vertical, horizontal,
    corner_word)``, or ``None`` when the message names no position.

    ``vertical`` is ``"top"`` / ``"bottom"`` (``upper``/``lower`` are
    synonyms); ``horizontal`` is ``"left"`` / ``"right"``; ``corner_word``
    is True when the word "corner" appears.
    """
    v = _VERTICAL_RE.search(message)
    h = _HORIZONTAL_RE.search(message)
    corner = _CORNER_RE.search(message) is not None
    if v is None and h is None and not corner:
        return None
    vertical = None
    if v is not None:
        vertical = "top" if v.group(1).lower() in ("top", "upper") else "bottom"
    horizontal = h.group(1).lower() if h is not None else None
    return vertical, horizontal, corner


def _finite_bounds(bounds_mm: Any) -> tuple[float, float, float, float] | None:
    """``[lo, hi]`` (XY used) as ``(xmin, ymin, xmax, ymax)``; ``None``
    when the bounds are missing or malformed (never a guessed box)."""
    if not isinstance(bounds_mm, (list, tuple)) or len(bounds_mm) != 2:
        return None
    try:
        lo = [float(bounds_mm[0][0]), float(bounds_mm[0][1])]
        hi = [float(bounds_mm[1][0]), float(bounds_mm[1][1])]
    except (TypeError, ValueError, IndexError):
        return None
    values = lo + hi
    if not all(math.isfinite(v) for v in values):
        return None
    return lo[0], lo[1], hi[0], hi[1]


def _distance(
    point: tuple[float, float],
    vertical: str | None,
    horizontal: str | None,
    box: tuple[float, float, float, float],
) -> float:
    """The hole-centre metric: distance to the reference corner point
    (both words), the perpendicular distance to the edge line (one word),
    or the distance to the nearest bounds corner (no side word)."""
    x, y = point
    xmin, ymin, xmax, ymax = box
    if vertical is not None and horizontal is not None:
        cx = xmax if horizontal == "right" else xmin
        cy = ymax if vertical == "top" else ymin
        return math.hypot(x - cx, y - cy)
    if vertical is not None:
        return abs(ymax - y) if vertical == "top" else abs(y - ymin)
    if horizontal is not None:
        return abs(xmax - x) if horizontal == "right" else abs(x - xmin)
    return min(
        math.hypot(x - cx, y - cy) for cx in (xmin, xmax) for cy in (ymin, ymax)
    )


def select_positional_hole(
    holes: list[dict[str, Any]],
    message: str,
    bounds_mm: Any,
) -> dict[str, Any] | None:
    """The measured hole a positional phrase in ``message`` names, or
    ``None`` (the point-at fallback) when the phrase names no position,
    the bounds are unusable, no hole has a usable centre, or the best two
    candidates are within :data:`AMBIGUITY_MM` of each other.

    ``holes`` are in MM (``d33d.hole_select.holes_in_mm``); ``bounds_mm``
    is the part's real XY bounds in MM (``[lo, hi]``).
    """
    words = positional_words(message)
    box = _finite_bounds(bounds_mm)
    if words is None or box is None:
        return None
    vertical, horizontal, _corner = words
    ranked: list[tuple[float, dict[str, Any]]] = []
    for hole in holes:
        center = hole.get("center")
        if not isinstance(center, (list, tuple)) or len(center) < 2:
            continue
        try:
            point = (float(center[0]), float(center[1]))
        except (TypeError, ValueError):
            continue
        if not all(math.isfinite(v) for v in point):
            continue
        ranked.append((_distance(point, vertical, horizontal, box), hole))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[0])
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < AMBIGUITY_MM:
        return None
    return ranked[0][1]


def nearest_hole_to_point(
    holes: list[dict[str, Any]], point: Any
) -> dict[str, Any] | None:
    """The measured hole nearest a viewer pick (issue #437): the XY distance
    from the pick's ``[x, y, z]`` to each hole's centre (``holes`` in MM).

    ``None`` when there is no usable pick, no hole with a usable centre, the
    two nearest holes are within :data:`AMBIGUITY_MM` (a pick between two
    holes is a point-at, not a guess), or the nearest hole's centre is more
    than its radius + :data:`PICK_TOLERANCE_MM` away (the pick is not on a
    hole at all).
    """
    if not isinstance(point, (list, tuple)) or len(point) < 2:
        return None
    try:
        px, py = float(point[0]), float(point[1])
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(px) and math.isfinite(py)):
        return None
    ranked: list[tuple[float, dict[str, Any]]] = []
    for hole in holes:
        center = hole.get("center")
        if not isinstance(center, (list, tuple)) or len(center) < 2:
            continue
        ranked.append((math.hypot(float(center[0]) - px, float(center[1]) - py), hole))
    if not ranked:
        return None
    ranked.sort(key=lambda r: r[0])
    if len(ranked) > 1 and ranked[1][0] - ranked[0][0] < AMBIGUITY_MM:
        return None
    distance, best = ranked[0]
    try:
        radius = float(best.get("diameter_mm") or 0.0) / 2.0
    except (TypeError, ValueError):
        radius = 0.0
    if not math.isfinite(radius) or distance > radius + PICK_TOLERANCE_MM:
        return None
    return best


__all__ = [
    "AMBIGUITY_MM",
    "PICK_TOLERANCE_MM",
    "nearest_hole_to_point",
    "positional_words",
    "select_positional_hole",
]
