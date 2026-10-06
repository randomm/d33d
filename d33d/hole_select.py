"""Measured-hole selection for the fill-and-recut offer (issue #396).

When a part carries measured holes (``part_report["holes"]``, the list the
import workstream stores at import — one ``{"center", "axis",
"diameter_mm"}`` entry per hole, in file units) and the user asks to
resize one of them, this module picks the hole the user means and builds
the instruction that carries the hole's numbers. Factored out of
``d33d.fill_recut`` (which owns the trigger, the offer lifecycle, and the
boundary copy) so the offer module stays under its size budget — the
selection logic and the measured-location instruction live here, and
``fill_recut`` imports them from this module.

Selection semantics (issue #396): "the center hole" picks the hole
nearest the part's XY bbox centre (in mm); a stated diameter picks the
hole with the CLOSEST diameter (a unique minimum); and when the choice is
ambiguous (two candidates within 0.1 mm of each other, or nothing
measured) the caller falls back to the point-at copy — never an
instruction without a location.
"""

from __future__ import annotations

import math
from typing import Any

#: The ambiguity threshold (mm) for the "nearest the centre" rule: two
#: candidate holes whose distances from the bbox centre differ by less
#: than this are treated as equally near (the point-at fallback fires
#: rather than a confident but arbitrary pick).
_AMBIGUITY_MM = 0.1


def select_measured_hole(
    holes: list[dict[str, Any]],
    message: str,
    bbox_mm: list[float] | None,
    trigger_size: float | None = None,
) -> dict[str, Any] | None:
    """Select the hole the user is referring to from the measured list.

    ``holes`` is the part's measured-hole list (each entry ``{"center":
    [x, y, z], "axis": [x, y, z], "diameter_mm": d}``); ``message`` the
    user's chat turn; ``bbox_mm`` the part's bounding box in MM (the
    file-unit bbox scaled by ``part_scale``); ``trigger_size`` the
    diameter (mm) the user stated for the resize, when they stated one.

    Selection rules (issue #396), in priority order:

    1. A single measured hole is picked trivially (it is "the" hole).
    2. A "center"/"centre"/"middle" qualifier picks the hole nearest the
       XY bbox centre; if two candidates are within :data:`_AMBIGUITY_MM`
       of each other the pick is ambiguous and ``None`` is returned (the
       point-at fallback).
    3. Otherwise, when the user stated a diameter, the hole with the
       CLOSEST diameter is picked (a unique minimum within the 0.1 mm
       ambiguity band — "the closest diameter", never a 5% band that
       would reject a unique nearest neighbour).
    4. Any other shape (no qualifier, no unique diameter match) is
       ambiguous: ``None`` (the point-at fallback).

    Returns the chosen hole dict or ``None`` (the caller uses the
    point-at copy — never an instruction without a location).
    """
    if not holes:
        return None
    if len(holes) == 1:
        return holes[0]

    msg_lower = message.lower()
    has_center_qualifier = any(
        w in msg_lower for w in ("center", "centre", "middle")
    )

    # Rule 2: "center hole" — nearest to the XY bbox centre (mm).
    # When a center qualifier is present, the center rule is the ONLY
    # rule that applies: if it cannot run (no usable bbox, or no hole
    # has a usable centre), the result is ``None`` (the point-at
    # fallback) — it must NOT silently switch to diameter matching
    # (a center qualifier that falls through to diameter matching
    # would pick a hole the user did not ask for).
    if has_center_qualifier:
        if bbox_mm is not None and len(bbox_mm) >= 2:
            cx = float(bbox_mm[0]) / 2.0
            cy = float(bbox_mm[1]) / 2.0
            dists: list[tuple[float, dict[str, Any]]] = []
            for h in holes:
                c = h.get("center")
                if not c or len(c) < 2:
                    continue
                d = math.hypot(float(c[0]) - cx, float(c[1]) - cy)
                dists.append((d, h))
            if dists:
                dists.sort(key=lambda x: x[0])
                # Two nearest candidates within the ambiguity band → the
                # pick is not defensible; fall back to point-at.
                if len(dists) > 1 and dists[1][0] - dists[0][0] < _AMBIGUITY_MM:
                    return None
                return dists[0][1]
        # No usable bbox or no hole had a usable centre → the center
        # rule cannot run; return None (point-at fallback), NOT the
        # diameter branch (the user asked for "the center hole",
        # not "the hole of this diameter").
        return None

    # Rule 3: no centre qualifier but a stated diameter — the CLOSEST
    # diameter (a unique minimum within the ambiguity band).
    if trigger_size is not None and trigger_size > 0:
        by_diameter: list[tuple[float, dict[str, Any]]] = []
        for h in holes:
            d = h.get("diameter_mm")
            if isinstance(d, (int, float)) and not isinstance(d, bool) and d > 0:
                by_diameter.append((abs(trigger_size - float(d)), h))
        if len(by_diameter) >= 1:
            by_diameter.sort(key=lambda x: x[0])
            if len(by_diameter) > 1 and by_diameter[1][0] - by_diameter[0][0] < _AMBIGUITY_MM:
                # Two holes within 0.1 mm of the target diameter →
                # ambiguous (the stated size fits more than one hole).
                return None
            return by_diameter[0][1]

    # Rule 4: no qualifier, no usable stated diameter → ambiguous.
    return None


def fill_recut_instruction_with_hole(
    noun: str,
    center: list[float],
    axis: list[float] | None,
    diameter_mm: float | None,
    size: float | None,
) -> str:
    """The fill-and-recut instruction for an offer that carries a MEASURED
    hole (issue #396): the instruction names the specific hole with its
    numbers — "fill the Ø30.0 mm hole at (60.0, 40.0), axis Z, then cut a
    Ø38.0 mm hole on the same axis" — so the loop knows exactly which hole
    to fill and what to cut in its place.

    ``noun`` the offer's noun (hole/bore/…); ``center`` the measured hole
    centre (``[x, y, …]`` — only the first two are used); ``axis`` the
    hole's axis (unit vector or ``None``); ``diameter_mm`` the hole's
    existing diameter (``None`` → no Ø clause for the fill); ``size`` the
    user's stated target diameter for the recut (``None`` → no size
    clause). The axis name (X/Y/Z) is derived from the dominant component;
    a non-cardinal axis is rendered as its components.
    """
    from d33d.part_http import PART_FILENAME

    cx = float(center[0])
    cy = float(center[1])
    dia_str = (
        f"Ø{diameter_mm:g} mm "
        if isinstance(diameter_mm, (int, float)) and not isinstance(diameter_mm, bool) and diameter_mm > 0
        else ""
    )
    size_str = (
        f"Ø{size:g} mm "
        if isinstance(size, (int, float)) and not isinstance(size, bool) and size > 0
        else ""
    )
    axis_clause = ""
    if axis is not None and len(axis) == 3:
        ax = [float(v) for v in axis]
        if abs(ax[0]) > 0.9:
            axis_name = "X"
        elif abs(ax[1]) > 0.9:
            axis_name = "Y"
        elif abs(ax[2]) > 0.9:
            axis_name = "Z"
        else:
            axis_name = f"({ax[0]:g}, {ax[1]:g}, {ax[2]:g})"
        axis_clause = f", axis {axis_name}"
    return (
        f"Fill-and-recut: fill the {dia_str}{noun} at ({cx:g}, {cy:g})"
        f"{axis_clause}, then cut a {size_str}{noun} on the same axis. "
        f"Never resize the imported mesh itself — "
        f"import(\"{PART_FILENAME}\") stays as brought."
    )


__all__ = [
    "fill_recut_instruction_with_hole",
    "holes_in_mm",
    "select_measured_hole",
]


def holes_in_mm(report: dict[str, Any] | None, scale: float | None) -> list[dict[str, Any]]:
    """The stored ``part_report["holes"]`` list converted to MM (issue
    #396).

    The holes are stored in FILE units (the same space as
    ``part_report["bbox_file_units"]`` — the import measures before the
    part's scale is known). Every reader (the offer selection, the
    instruction, the question-answer reply) must compare and report in
    MM — the user's unit — so this is the ONE conversion helper: each
    hole's centre and diameter are scaled by ``part_scale``.

    ``report`` is the stored part report (``None`` / no ``holes`` key /
    a malformed entry → the hole is simply omitted — omit-not-null,
    mirroring the import). ``scale`` is the part's ``part_scale``
    (``None`` / non-numeric / ``<= 0`` → ``[]`` — no measured holes,
    never file units: a missing or corrupt scale means the holes are in
    unknown units, and reporting them as mm would be a lie).

    Returns a NEW list of ``{"center": [x, y, z], "axis": [x, y, z],
    "diameter_mm": d}`` dicts in mm (the axis is unit — unchanged by
    the scale). ``[]`` when the scale is missing, non-numeric, or
    non-positive (never file units).
    """
    holes: list[dict[str, Any]] = []
    if not isinstance(report, dict):
        return holes
    stored = report.get("holes")
    if not isinstance(stored, list):
        return holes
    try:
        factor = float(scale) if scale is not None else 0.0
    except (TypeError, ValueError):
        factor = 0.0
    if factor <= 0:
        # A missing, non-numeric, or non-positive scale means the holes
        # are in unknown units — returning them unscaled would report
        # file-unit values as mm (a lie). No measured holes instead.
        return holes
    for entry in stored:
        if not isinstance(entry, dict):
            continue
        center = entry.get("center")
        diameter = entry.get("diameter_mm")
        if not isinstance(center, (list, tuple)) or len(center) < 2:
            continue
        if not (
            isinstance(diameter, (int, float))
            and not isinstance(diameter, bool)
            and diameter > 0
        ):
            continue
        axis = entry.get("axis")
        holes.append(
            {
                "center": [
                    float(v) * factor for v in center[:3]
                ],
                "axis": ([float(v) for v in axis] if isinstance(axis, (list, tuple)) and len(axis) == 3 else [0.0, 0.0, 1.0]),
                "diameter_mm": float(diameter) * factor,
            }
        )
    return holes
