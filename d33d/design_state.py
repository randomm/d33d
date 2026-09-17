"""One serialised design-state block, shared by BOTH consumers (issue #120).

The largest failure mode is the user and the machine holding different
mental models of the object — the model has described a 30 mm sphere as 60
mm because the prompt carried no description of the existing design (the
bug this module pins). The fix: ONE serialised state block describing the
current object and its parameters, with provenance per value, built by ONE
function that both the live design prompt builder (``d33d.design_loop``)
and the API route the SPA reads (``d33d.versions_routes``) call — the same
callable, asserted by the tests, not two functions that happen to agree.

Entry shape (the contract the SPA's Brief renders and the prompt renders):
``name`` (the parameter key), ``label`` (the human label — for a parameter
with no label the label IS the parameter name, never invented prose),
``value`` (nullable), ``unit`` (``"mm"`` for numeric params, else ``None``),
and ``provenance`` — a ``Literal`` (closed set, never a bare ``str``;
the project's ``error_class`` enum is the precedent) of
``"stated" | "measured" | "unknown" | "disagrees"``, plus ``stated_value``
when ``provenance == "disagrees"``.

``unknown`` is a REAL state and must survive serialisation as
``value: null`` — never ``0``, never an omitted key a consumer can
default. ``latest_version_stated_dims`` (``d33d.design_loop_events``)
returns ``None`` rather than a zero triple for exactly this reason; this
module follows it.

The block's bound: at most :data:`MAX_STATE_BLOCK_ENTRIES` entries
(12) reach the prompt — the prompt already carries the request, chat
history, reference dimensions and emission instruction, and the Brief's
own six-row display surface fits with headroom. Past the bound the
overflow is DROP-and-COUNT: the first 12 entries (declaration order —
JSON objects preserve insertion order) render, and a final
``… N more parameter(s)`` count line names the drop honestly (never
silently).

``measured`` / ``disagrees`` provenance is reachable from real data:
the versions table persists the measured bounding box of the render that
produced each version (issue #137 — the best candidate's bbox, the
matched component for multi-part models, NULL when no measurement was
obtainable). The shared callable :func:`state_block_for_version`
compares the latest version's params snapshot against that persisted
measurement: a stated value that matches the measurement within the
tolerance renders as ``measured`` (the displayed value is the MEASURED
one — what will actually print), a stated value outside the tolerance
renders as ``disagrees`` carrying BOTH numbers (``stated_value`` rides
alongside), and a stated value with no persisted measurement keeps
``stated``. Only parameters mapped to the bbox's axes (``W``/``D``/``H``)
are compared — a non-axis parameter such as ``rod_bore`` has no axis and
MUST stay ``stated``; it never silently becomes ``measured``. The live
consumers read the persisted measurement only; they do NOT re-render to
obtain one.
"""

from __future__ import annotations

from typing import Any, Literal, NotRequired, TypedDict

from d33d.design_loop import (
    BBOX_TOLERANCE_MIN_MM,
    BBOX_TOLERANCE_REL,
    best_match_component,
)

__all__ = [
    "MAX_STATE_BLOCK_ENTRIES",
    "Provenance",
    "StateEntry",
    "build_design_state_block",
    "format_design_state_block",
    "persisted_bbox_extents",
    "state_block_for_version",
    "state_block_from_params",
]

#: The bound on how many entries reach the prompt (and the SPA's Brief).
#: The prompt already carries the request, chat history, reference
#: dimensions, and emission instruction; 12 entries keeps the block a
#: single, readable, size-bounded section (the Brief's six-row display
#: surface fits with headroom). Past the bound the overflow is
#: drop-and-count (see :func:`build_design_state_block`).
MAX_STATE_BLOCK_ENTRIES = 12

#: The closed provenance set (the project's ``error_class`` enum is the
#: precedent — a ``Literal``, never a bare ``str``). ``stated`` (the user
#: said this value), ``measured`` (a render produced this value),
#: ``unknown`` (no value — value serialises to ``null``), ``disagrees``
#: (a measured value exists that differs from the stated one — both
#: numbers are carried).
Provenance = Literal["stated", "measured", "unknown", "disagrees"]


class StateEntry(TypedDict):
    """One design-state entry (the serialised contract).

    ``value`` is nullable: ``"unknown"`` provenance carries ``None``.
    ``stated_value`` is present ONLY when ``provenance == "disagrees"``
    (the displayed value is the MEASURED one — what will print — and the
    stated value rides alongside; they are never collapsed), so it is
    ``NotRequired``: the common (``stated``/``unknown``) case carries no
    ``stated_value`` key at all.
    """

    name: str
    label: str
    value: float | str | bool | None
    unit: str | None
    provenance: Provenance
    stated_value: NotRequired[float | str | bool | None]


def _is_number(value: Any) -> bool:
    """True for int/float (bool is excluded — it is not a measurement)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _entry(
    name: str,
    value: Any,
    provenance: Provenance,
    stated_value: Any = None,
) -> dict[str, Any]:
    """One entry from a (name, value) pair (provenance supplied)."""
    out: dict[str, Any] = {
        "name": name,
        "label": name,  # label IS the parameter name (no invented prose)
        "value": value,
        "unit": "mm" if _is_number(value) else None,
        "provenance": provenance,
    }
    if provenance == "disagrees":
        out["stated_value"] = stated_value
    return out


def state_block_from_params(
    params: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The design-state block from a version's params snapshot
    (``versions_service.latest_version(project_id)["params"]``).

    This is the LIVE route's data source. Every declared parameter
    (never a fixed {W, D, H} triple) becomes an entry:

    - a known numeric/bool value → provenance ``"stated"``, ``unit``
      ``"mm"`` for numeric, ``None`` for non-numeric (a string/bool
      parameter has no mm meaning);
    - a missing/null/zero/absent value → provenance ``"unknown"`` with
      ``value: None`` (``null`` — never ``0``, never an omitted key).

    ``None`` (no version yet) → an empty block (zero entries).
    """
    if params is None:
        return []
    out: list[dict[str, Any]] = []
    for name, value in params.items():
        if _is_number(value):
            if value == 0:
                out.append(_entry(name, None, "unknown"))
            else:
                out.append(_entry(name, value, "stated"))
        elif value is None:
            out.append(_entry(name, None, "unknown"))
        else:
            # string/bool — a real parameter, stated, no mm unit.
            out.append(_entry(name, value, "stated"))
    return out


#: The parameter names that map onto the bbox's axes (``W`` → x,
#: ``D`` → y, ``H`` → z — the ``_dim_params`` convention ``d33d.design_loop``
#: threads to the render as ``defines``). A bbox gives W/D/H and ONLY
#: those parameters may be compared against it: a parameter with no axis
#: (``rod_bore``, ``wall_thickness``) is never a measurement target —
#: it stays ``stated``, never silently becomes ``measured``.
AXIS_PARAM_NAMES: tuple[str, str, str] = ("W", "D", "H")


def persisted_bbox_extents(measurement: Any) -> tuple[float, float, float] | None:
    """The per-axis extents to compare stated values against, or ``None``
    (issue #137).

    ``measurement`` is the version row's persisted ``bbox`` (``None`` —
    no measurement was obtainable at version creation — or the JSON
    ``{"x": ..., "y": ..., "z": ...}`` the write path stored). ``None``
    → ``None`` (ABSTAIN: a persisted NULL means "no measurement", never
    a fabricated ``(0.0, 0.0, 0.0)`` — issue #91's precedent). A stored
    zero axis also abstains: a zero extent is not a measurement, it is
    the encoded absence, and comparing a stated value against ``0`` would
    mark every axis ``disagrees`` for a version that never measured.
    """
    if measurement is None:
        return None
    if not isinstance(measurement, dict):
        return None
    axes = [measurement.get(name) for name in ("x", "y", "z")]
    if not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in axes):
        return None
    extents = tuple(float(v) for v in axes)  # type: ignore[arg-type]
    if any(e <= 0 for e in extents):
        return None
    return extents


def state_block_for_version(
    params: dict[str, Any] | None,
    measurement: Any = None,
) -> list[dict[str, Any]]:
    """The design-state block for ONE version: the version's params
    snapshot (``state_block_from_params``) upgraded with the version's
    PERSISTED measurement (issue #137).

    This is THE shared callable — the live prompt builder
    (``d33d.design_loop``) and the GET route the SPA reads
    (``d33d.versions_routes``) both call it (identity pinned by the
    tests, not output equality). ``measurement`` is the version row's
    ``bbox`` field as the write path stored it (issue #137: the best
    candidate's render bbox — the MATCHED COMPONENT for a multi-part
    model, ``None`` when the component was not identifiable or no
    measurement was obtained). ``None`` measurement → the pure
    params-only substrate's output, verbatim (``stated``/``unknown``).

    Comparison rule (stated explicitly): a parameter is compared against
    the measurement ONLY when its name maps onto a bbox axis —
    :data:`AXIS_PARAM_NAMES` (``W`` → x, ``D`` → y, ``H`` → z). A
    parameter with no axis (``rod_bore``) is never compared: it keeps
    ``stated`` (or ``unknown``) and never silently becomes ``measured``.

    For a comparable parameter whose stated value is known (a positive
    number):

    - the measured axis is within the named tolerance (``max(
      BBOX_TOLERANCE_REL * stated, BBOX_TOLERANCE_MIN_MM)`` — the same
      rule the bbox gate applies, ``d33d.design_loop``'s constants) →
      provenance ``"measured"``, and the DISPLAYED value is the measured
      one (what will actually print), not the stated one;
    - the measured axis is outside the tolerance → provenance
      ``"disagrees"`` carrying BOTH numbers: ``value`` is the measured
      one (the display), ``stated_value`` the stated one (the ride-
      along).

    A stated value that is unknown/absent/zero, and every non-axis
    parameter, is untouched by the measurement. ``None`` params (no
    version yet) → an empty block.
    """
    entries = state_block_from_params(params)
    if measurement is None:
        return entries
    extents = persisted_bbox_extents(measurement)
    if extents is None:
        return entries
    out: list[dict[str, Any]] = []
    for entry in entries:
        axis = entry["name"] if entry["name"] in AXIS_PARAM_NAMES else None
        if axis is None:
            out.append(entry)
            continue
        stated = entry["value"]
        if not _is_number(stated) or stated <= 0:
            # unknown/zero stated value: no comparison possible (the
            # gate's ticket #91 abstain semantics, mirrored here).
            out.append(entry)
            continue
        extent = extents[AXIS_PARAM_NAMES.index(axis)]
        tol = max(BBOX_TOLERANCE_REL * stated, BBOX_TOLERANCE_MIN_MM)
        if abs(extent - stated) <= tol:
            e = dict(entry)
            e["value"] = extent
            e["provenance"] = "measured"
            out.append(e)
        else:
            e = dict(entry)
            e["value"] = extent
            e["provenance"] = "disagrees"
            e["stated_value"] = stated
            out.append(e)
    return out


def build_design_state_block(
    entries: list[dict[str, Any]],
    max_entries: int = MAX_STATE_BLOCK_ENTRIES,
) -> dict[str, Any]:
    """The serialised block: entries bounded to ``max_entries`` with a
    drop-and-count overflow marker.

    The bound is enforced HERE (the single function both consumers call):
    at most ``max_entries`` entries reach the prompt (or the SPA). Past
    the bound the overflow is dropped and counted — the first
    ``max_entries`` entries (declaration order) render and the block's
    ``dropped_count`` carries how many were omitted. ``dropped_count == 0``
    means nothing was dropped (the count line is omitted by the formatter).
    """
    dropped = max(0, len(entries) - max_entries)
    return {
        "entries": list(entries[:max_entries]),
        "dropped_count": dropped,
        "unit": "mm",
    }


def format_design_state_block(block: dict[str, Any]) -> str:
    """The block as prompt text (the live prompt's rendering of the block).

    One line per entry (``label = value`` — ``not specified`` for a
    ``null`` value), then a final ``… N more parameter(s)`` count line
    when ``dropped_count > 0``. An empty block (no version yet) renders a
    single ``not specified`` line so the section is never silently absent
    (the 30/60 bug is a prompt that carries no description of the
    existing design — an absent block is the bug itself).
    """
    lines: list[str] = []
    for entry in block.get("entries", []):  # type: ignore[union-attr]
        value = entry.get("value")
        if _is_number(value):
            value_str = f"{value:g}"
        elif value is None:
            value_str = "not specified"
        else:
            value_str = str(value)
        label = entry.get("label") or entry.get("name")
        lines.append(f"{label} = {value_str}")
    if block.get("dropped_count", 0) > 0:
        n = block["dropped_count"]
        lines.append(f"… {n} more parameter{'s' if n != 1 else ''}")
    if not lines:
        lines.append("(no parameters yet)")
    return "Current design state (mm):\n" + "\n".join(lines)


def format_design_state_line(entries: list[dict[str, Any]]) -> str:
    """A single-line variant of the block (no header, no count line) —
    the prompt's ground-truth line. Empty block → ``not specified``.
    (The live prompt's system line uses the compact W/D/H triple; this
    helper is provided for any consumer that wants the full entry set on
    one line.)"""
    if not entries:
        return "not specified"
    parts: list[str] = []
    for entry in entries:
        value = entry.get("value")
        if _is_number(value):
            value_str = f"{value:g}"
        elif value is None:
            value_str = "not specified"
        else:
            value_str = str(value)
        label = entry.get("label") or entry.get("name")
        parts.append(f"{label}={value_str}")
    return ", ".join(parts)
