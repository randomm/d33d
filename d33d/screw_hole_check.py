"""Deterministic screw-hole clearance post-check (issue #317).

The post-check runs on the ok-render branch of the design loop, BEFORE the
``score.perfect`` pass return — an undersize metric-screw hole is invisible
to all five gate bits, so without this check a 4.0 mm M4 hole would pass
silently.

The module is deliberately separate from ``d33d.design_loop`` (the loop
orchestrates; this module owns the check's logic) and from
``d33d.design_prompts`` (the single source of the clearance table, regex,
and abstention guard). Both the live loop prompt and ``design_prompt``
render the table's rows from ``d33d.design_prompts.METRIC_SCREW_CLEARANCE_MM``
— no second copy of the numbers lives here.

The repair dict the check returns carries the EXISTING ``geometrically_wrong``
failure class (no new class, no new dict shape) and is routed through the
same ``route_repair`` / ``_design_messages`` path as every other repair.
"""

from __future__ import annotations

from typing import Any

from d33d.design_prompts import (
    _THREAD_HOLE_WORD_RE,
    METRIC_SCREW_CLEARANCE_MM,
    SCREW_SIZE_RE,
)

__all__ = [
    "is_screw_hole_candidate",
    "mm",
    "undersize_screw_hole",
]


def mm(value: float) -> str:
    """Render a millimetre measurement at its natural precision.

    ``4.0`` for 4.0, ``4.44`` for 4.44, ``4.5`` for 4.5 — the exact-form
    repair message pins the value the model actually emitted.  At least one
    decimal is always shown (``4.0``, never ``4``).
    """
    s = f"{value:.2f}"
    if "." in s:
        s = s.rstrip("0")
        if s.endswith("."):
            s += "0"
    return s


def is_screw_hole_candidate(
    name: str, value: float, meta: dict[str, Any] | None
) -> bool:
    """Does this named parameter read as a screw through-hole diameter?

    A candidate is a positive numeric parameter whose name or its
    ``param_meta`` label/reason says "hole".  Threaded/tapped/insert
    wording in the name or metadata disqualifies it — those are not
    clearance holes, and the check abstains on them (no repair, no false
    repair).  A param with no hole wording at all is not a candidate
    either: the check must never guess which of several parameters a
    named screw sizes.
    """
    name_lower = name.lower()
    if "thread" in name_lower or "tap" in name_lower or "insert" in name_lower:
        return False
    if "hole" not in name_lower:
        return False
    if meta:
        meta_text = " ".join(
            str(meta.get(key, "")) for key in ("label", "reason")
        ).lower()
        if any(word in meta_text for word in ("thread", "tap", "insert")):
            return False
        if "hole" not in meta_text:
            return False
    return True


def undersize_screw_hole(
    request: str,
    params: dict[str, float],
    param_meta: dict[str, Any],
) -> dict[str, Any] | None:
    """The deterministic screw-hole clearance post-check (issue #317).

    Runs on the ok-render branch of the loop, BEFORE the ``score.perfect``
    pass return (an undersize hole is invisible to all five gate bits).

    Returns ``None`` (the check abstains — nothing is fed to the next
    iteration) unless ALL of the following hold:

    * the user's ``request`` names a metric screw size with a word
      boundary (``M4`` in "a 60 × 45 mm plate with an M4 hole"; ``M40``
      and ``BM4`` do not; ``M4x20`` does);
    * the request does NOT use threaded/tapped/insert wording (through
      holes only — those abstain, never a false repair);
    * EXACTLY ONE candidate parameter (name/label/reason reads as a hole
      diameter, threaded/tapped/insert wording excluded) — zero or more
      than one candidate abstains (never guess which param a named screw
      sizes).

    Otherwise returns the structured repair dict (the same shape
    ``route_repair`` yields — ``failure_class`` pinned to the EXISTING
    ``geometrically_wrong`` class, no new class) whose ``instruction``
    carries the sentence ``M4 clearance hole is 4.0 mm; printed M4
    clearance is 4.5 mm`` (values from the single-source
    ``METRIC_SCREW_CLEARANCE_MM`` table) and whose ``evidence`` names the
    parameter with both numbers.  The hole must be BELOW the table's
    clearance diameter by MORE than 0.05 mm to trigger (a hole at
    4.45 mm for M4 abstains; at 4.44 mm it triggers).
    """
    if not request or not params:
        return None
    if _THREAD_HOLE_WORD_RE.search(request):
        return None
    sizes: list[str] = []
    for value in SCREW_SIZE_RE.findall(request):
        size = f"M{value}"
        if size in METRIC_SCREW_CLEARANCE_MM and size not in sizes:
            sizes.append(size)
    if not sizes:
        return None
    undersize: list[tuple[str, str, float, float]] = []
    for size in sizes:
        clearance = METRIC_SCREW_CLEARANCE_MM[size]
        for name, value in params.items():
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                continue
            value = float(value)
            if value <= 0:
                continue
            meta = param_meta.get(name) if isinstance(param_meta, dict) else None
            if not is_screw_hole_candidate(name, value, meta):
                continue
            if clearance - value > 0.05:
                undersize.append((name, size, value, clearance))
    if len(undersize) != 1:
        # Zero: nothing to repair.  More than one (two hole params, or
        # two named screw sizes): the check cannot identify the param
        # confidently — it does nothing, never a false repair.
        return None
    name, size, value, clearance = undersize[0]
    meta = param_meta.get(name) if isinstance(param_meta, dict) else None
    label = (
        meta.get("label")
        if isinstance(meta, dict) and isinstance(meta.get("label"), str)
        else name
    )
    return {
        "failure_class": "geometrically_wrong",
        "instruction": (
            f"{size} clearance hole is {mm(value)} mm; printed {size} "
            f"clearance is {mm(clearance)} mm. Model the hole at the "
            f"clearance diameter ({mm(clearance)} mm), not the nominal "
            f"size, and state the clearance in the parameter's reason."
        ),
        "scad_source": "",
        "evidence": (
            f"{label} = {mm(value)} mm "
            f"(below the {size} clearance of {mm(clearance)} mm)"
        ),
    }
