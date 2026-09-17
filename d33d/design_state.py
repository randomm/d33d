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

``measured`` / ``disagrees`` provenance requires a measurement to compare
against. Measured values come from the rendered bbox — but the versions
table has no bbox column, so NO measurement is persisted at version
creation yet. The builder therefore supports ``measured``/``disagrees``
synthetically (the unit tests exercise that branch); the LIVE route reads
only the params snapshot and emits ``stated``/``unknown``. It does NOT
re-render to obtain a measurement.
"""

from __future__ import annotations

from typing import Any, Literal, TypedDict

__all__ = [
    "MAX_STATE_BLOCK_ENTRIES",
    "Provenance",
    "StateEntry",
    "build_design_state_block",
    "format_design_state_block",
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
    stated value rides alongside; they are never collapsed).
    """

    name: str
    label: str
    value: float | str | bool | None
    unit: str | None
    provenance: Provenance
    stated_value: float | str | bool | None


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
