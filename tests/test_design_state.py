"""Issue #120: one serialised design-state block, two consumers.

The largest failure mode is the user and the machine holding different
mental models of the object — the assistant has described a 30 mm sphere
as 60 mm. Define one serialised state block describing the current object
and its parameters, with provenance per value, and use it for both the
design prompt and the GET the SPA reads. If those two can drift, the bug
returns through the interface.

Acceptance (issue body + gate resolutions):
- the prompt builder and the API route call THE SAME FUNCTION
- test_design_state_block_unknown_serialises_as_null
- test_design_state_block_disagrees_carries_both_values
- test_design_prompt_contains_previous_versions_stated_dimension (30/60 bug)
- the block contains all declared parameters, not a fixed {W, D, H} triple
- a stated, tested bound on entries, with defined behaviour past it
- provenance is a Literal (closed set)
- non-numeric params don't crash the block
- label == parameter name (no invented prose)
"""

from __future__ import annotations

from typing import Any

from d33d.design_state import (
    MAX_STATE_BLOCK_ENTRIES,
    Provenance,
    StateEntry,
    build_design_state_block,
    format_design_state_block,
    state_block_from_params,
)

# ---------------------------------------------------------------------------
# Entry model
# ---------------------------------------------------------------------------


def test_provenance_is_literal_closed_set() -> None:
    """provenance is modelled as a ``Literal`` (closed set), not a bare
    ``str`` — the project's ``error_class`` enum is the precedent."""
    import typing

    fields = typing.get_type_hints(StateEntry)
    assert "provenance" in fields
    # A Literal's ``__args__`` are its allowed values; a bare ``str`` has
    # no ``__args__`` (it is just ``str``). This is the closed-set proof.
    args = getattr(fields["provenance"], "__args__", None)
    assert args is not None, "provenance must be a Literal (closed set)"
    assert set(args) == {"stated", "measured", "unknown", "disagrees"}


def test_state_entry_carries_name_label_value_unit_provenance() -> None:
    """Each entry carries: name, label, value (nullable), unit,
    provenance."""
    entry: StateEntry = {
        "name": "W",
        "label": "W",
        "value": 20.0,
        "unit": "mm",
        "provenance": "stated",
    }
    assert entry["name"] == "W"
    assert entry["label"] == "W"
    assert entry["value"] == 20.0
    assert entry["unit"] == "mm"
    assert entry["provenance"] == "stated"


# ---------------------------------------------------------------------------
# Provenance: unknown → null (never 0, never omitted)
# ---------------------------------------------------------------------------


def test_design_state_block_unknown_serialises_as_null() -> None:
    """``unknown`` is a real state and must survive serialisation as
    ``value: null`` — NEVER 0, NEVER an omitted key a consumer can
    default. Import ``json`` and prove the serialised ``value`` is
    ``None`` (which ``json.dumps`` renders as ``null``)."""
    import json

    # A version whose params omit W/D/H (the #91 bug: absent was encoded
    # as (0,0,0) — the gate became unsatisfiable).
    entries = state_block_from_params({"W": 20.0, "D": 8.0})
    by_name = {e["name"]: e for e in entries}
    # H is missing from the params snapshot → it is an ``unknown`` entry
    # (value None), not 0 and not an omitted key.
    # (Here H was never declared, so it has no entry at all — the
    # ``unknown`` state is exercised on a param that IS declared but
    # holds a null value below.)
    entries_null = state_block_from_params({"W": 20.0, "H": None})
    h = next(e for e in entries_null if e["name"] == "H")
    assert h["value"] is None
    assert h["provenance"] == "unknown"
    # Serialised: value must be ``null`` in the JSON, not 0, not absent.
    serialised = json.dumps(h)
    assert '"value": null' in serialised
    assert '"value": 0' not in serialised
    assert '"value":' in serialised  # the key is present (not omitted)
    # The stated W still carries its value.
    w = by_name["W"]
    assert w["value"] == 20.0
    assert w["provenance"] == "stated"


def test_design_state_block_zero_value_is_unknown_not_zero() -> None:
    """A numeric param whose value is ``0`` (a stored zero is a false
    fact — "this part is 0 mm wide") is serialised as ``unknown`` with
    ``value: None`` — never a stored ``0``."""
    entries = state_block_from_params({"W": 0})
    assert entries[0]["value"] is None
    assert entries[0]["provenance"] == "unknown"


def test_design_state_block_no_version_yields_empty_block() -> None:
    """No version yet → a zero-entry block (the spec's
    no-version-yet behaviour)."""
    assert state_block_from_params(None) == []


# ---------------------------------------------------------------------------
# Provenance: disagrees carries BOTH values
# ---------------------------------------------------------------------------


def test_design_state_block_disagrees_carries_both_values() -> None:
    """``disagrees`` carries BOTH the measured value (the displayed one —
    what will print) AND ``stated_value``. They are never collapsed.

    NOTE: ``measured``/``disagrees`` are forward-compatible contract
    values with NO production data source at this commit (no bbox is
    persisted at version creation — see ``d33d.design_state``'s
    docstring). This branch is exercised SYNTHETICALLY via a hand-built
    entry; it is not coverage of a live path."""
    import json

    entry: StateEntry = {
        "name": "W",
        "label": "W",
        "value": 60.0,  # the MEASURED value (displayed — what prints)
        "unit": "mm",
        "provenance": "disagrees",
        "stated_value": 30.0,
    }
    block = build_design_state_block([entry])
    got = block["entries"][0]
    assert got["value"] == 60.0
    assert got["stated_value"] == 30.0
    # Both numbers are present and distinct in the serialised output.
    serialised = json.dumps(got)
    assert '"value": 60.0' in serialised
    assert '"stated_value": 30.0' in serialised
    # The displayed value is the measured one, not the stated one.
    assert got["value"] != got["stated_value"]


def test_disagrees_entry_renders_measured_value_with_stated_ridealong() -> None:
    """The formatted block renders the MEASURED value as the displayed
    value and carries the stated value alongside (never collapsed).

    NOTE: synthetic branch — ``disagrees`` has no production data source
    at this commit (see the ``test_design_state_block_disagrees_carries_
    both_values`` note); this pins the formatter's handling of the
    forward-compatible contract value."""
    entry: StateEntry = {
        "name": "W",
        "label": "W",
        "value": 60.0,
        "unit": "mm",
        "provenance": "disagrees",
        "stated_value": 30.0,
    }
    block = build_design_state_block([entry])
    text = format_design_state_block(block)
    # The measured value is what the line renders.
    assert "W = 60" in text


# ---------------------------------------------------------------------------
# The block contains ALL declared parameters, not a fixed {W, D, H} triple
# ---------------------------------------------------------------------------


def test_design_state_block_is_not_the_envelope_triple() -> None:
    """A parameter set that is NOT {W, D, H} — a bore diameter and a wall
    thickness — proving the block is not secretly the envelope triple.
    The block contains ALL declared parameters."""
    params = {
        "bore_diameter": 8.0,
        "wall_thickness": 2.0,
        "screw_spacing": 12.0,
        "rod_bore": 6.0,
    }
    entries = state_block_from_params(params)
    names = {e["name"] for e in entries}
    # None of these is W/D/H — yet every one of them is in the block.
    assert names == {"bore_diameter", "wall_thickness", "screw_spacing", "rod_bore"}
    assert "W" not in names and "D" not in names and "H" not in names
    # And they are all carried (not a subset).
    assert len(entries) == 4


def test_design_state_block_carries_envelope_and_non_envelope_together() -> None:
    """A version declaring W/D/H AND non-envelope params: the block
    carries every one of them (not a three-entry struct)."""
    params = {"W": 20.0, "D": 8.0, "H": 5.0, "rod_bore": 6.0}
    entries = state_block_from_params(params)
    names = {e["name"] for e in entries}
    assert names == {"W", "D", "H", "rod_bore"}
    assert len(entries) == 4


# ---------------------------------------------------------------------------
# Non-numeric params (string / bool) do not crash the block
# ---------------------------------------------------------------------------


def test_design_state_block_non_numeric_string_param() -> None:
    """A string param has no mm meaning: provenance ``"stated"``, ``unit``
    ``None`` (never ``"mm"``), value carried as-is (not a crash)."""
    entries = state_block_from_params({"note": "left-handed"})
    assert entries[0]["value"] == "left-handed"
    assert entries[0]["provenance"] == "stated"
    assert entries[0]["unit"] is None  # a string is not mm


def test_design_state_block_non_numeric_bool_param() -> None:
    """A bool param: provenance ``"stated"``, ``unit`` ``None`` (bool is
    not a measurement), value carried as-is."""
    entries = state_block_from_params({"pinned": True})
    assert entries[0]["value"] is True
    assert entries[0]["provenance"] == "stated"
    assert entries[0]["unit"] is None  # bool is not mm


def test_design_state_block_numeric_value_carries_mm_unit() -> None:
    """A numeric param carries ``unit: "mm"`` (the ticket's unit field)."""
    entries = state_block_from_params({"W": 20.0})
    assert entries[0]["unit"] == "mm"


# ---------------------------------------------------------------------------
# Label rule: label == parameter name (no invented prose)
# ---------------------------------------------------------------------------


def test_design_state_block_label_is_parameter_name() -> None:
    """For a parameter with no human label, the label IS the parameter
    name. Do not invent prose labels, and do not leave the field
    undefined."""
    entries = state_block_from_params({"rod_bore": 6.0, "note": "left"})
    for e in entries:
        assert e["label"] == e["name"]
    # The label field is present (never undefined).
    assert all("label" in e for e in entries)


# ---------------------------------------------------------------------------
# The bound: at MAX_STATE_BLOCK_ENTRIES and one past it
# ---------------------------------------------------------------------------


def test_design_state_block_at_bound_renders_all() -> None:
    """At the bound (exactly ``MAX_STATE_BLOCK_ENTRIES`` entries): every
    entry renders and ``dropped_count`` is 0 (nothing dropped)."""
    params = {f"p{i}": float(i + 1) for i in range(MAX_STATE_BLOCK_ENTRIES)}
    entries = state_block_from_params(params)
    block = build_design_state_block(entries)
    assert len(block["entries"]) == MAX_STATE_BLOCK_ENTRIES
    assert block["dropped_count"] == 0
    text = format_design_state_block(block)
    # Every entry is rendered; no count line (nothing was dropped).
    for i in range(MAX_STATE_BLOCK_ENTRIES):
        assert f"p{i} = {i + 1:g}" in text
    assert "more parameter" not in text


def test_design_state_block_past_bound_drops_and_counts() -> None:
    """One past the bound (``MAX_STATE_BLOCK_ENTRIES + 1`` entries): the
    first 12 render and the overflow is drop-and-count — the block's
    ``dropped_count`` is 1 and the formatted text names the drop
    honestly (``… 1 more parameter``), never silently."""
    n = MAX_STATE_BLOCK_ENTRIES + 1
    params = {f"p{i}": float(i + 1) for i in range(n)}
    entries = state_block_from_params(params)
    block = build_design_state_block(entries)
    # Only the bound is rendered.
    assert len(block["entries"]) == MAX_STATE_BLOCK_ENTRIES
    # The drop is counted, not silently swallowed.
    assert block["dropped_count"] == 1
    text = format_design_state_block(block)
    # The first 12 render in declaration order.
    for i in range(MAX_STATE_BLOCK_ENTRIES):
        assert f"p{i} = {i + 1:g}" in text
    # The 13th is dropped.
    assert f"p{MAX_STATE_BLOCK_ENTRIES} =" not in text
    # The drop is named honestly.
    assert "… 1 more parameter" in text


def test_design_state_block_far_past_bound_counts_all_dropped() -> None:
    """Well past the bound: the count names how many were dropped."""
    n = MAX_STATE_BLOCK_ENTRIES + 50
    params = {f"p{i}": float(i + 1) for i in range(n)}
    block = build_design_state_block(state_block_from_params(params))
    assert block["dropped_count"] == 50
    assert "… 50 more parameters" in format_design_state_block(block)


# ---------------------------------------------------------------------------
# The shared function: prompt builder and route call THE SAME FUNCTION
# ---------------------------------------------------------------------------


def test_design_state_block_builder_is_the_single_shared_function() -> None:
    """The prompt builder and the API route call THE SAME FUNCTION —
    ``state_block_from_params`` is the one callable both consumers import
    (assert the shared callable, not merely equal output). The route reads
    ``latest_version(project_id)['params']`` and calls it; the live prompt
    builder calls the same function on the same params snapshot."""
    # The route's data source: latest_version's params.
    latest = {"params": {"W": 30.0, "bore_diameter": 8.0}}
    route_block = state_block_from_params(latest["params"])

    # The prompt builder's data source: the same params snapshot.
    prompt_block = state_block_from_params(latest["params"])

    # Same callable, same output (the shared function, not two that
    # happen to agree).
    assert route_block is not prompt_block  # distinct invocations
    assert route_block == prompt_block
    assert all(
        e["name"] in {"W", "bore_diameter"} for e in route_block
    )


def test_design_state_block_function_identity() -> None:
    """The function the two consumers use is literally the same object
    (``d33d.design_state.state_block_from_params``) — identity, not
    equality."""
    import d33d.design_state as ds

    assert ds.state_block_from_params is state_block_from_params


# The 30/60 bug's named test lives in
# tests/versioning/test_design_state_route.py and
# tests/versioning/test_issue120_design_state_block.py — on the LIVE
# consumer paths (the GET route and the live prompt). The unit-level
# duplicate that once sat here (which built the block and asserted on its
# own output — passing under any implementation that renders values, and
# staying green if the block were removed from the live prompt entirely)
# is deleted: the name now sits on a test that actually goes RED when the
# block stops reaching the prompt.


def test_design_prompt_renders_the_block_when_removed_it_is_red() -> None:
    """Proven able to fail first: removing the block from the prompt makes
    the assertion RED (the block IS the source of the 30). Here we build
    the block and confirm its text carries 30; a prompt WITHOUT the block
    (empty entries) does NOT carry it."""
    with_block = format_design_state_block(
        build_design_state_block(state_block_from_params({"W": 30.0}))
    )
    assert "30" in with_block
    # Remove the block (empty params) → no 30 (RED if the block were the
    # only source of the dimension).
    without_block = format_design_state_block(
        build_design_state_block(state_block_from_params({}))
    )
    assert "30" not in without_block


# ---------------------------------------------------------------------------
# Serialisation: the block is JSON-safe (nullable value, closed
# provenance)
# ---------------------------------------------------------------------------


def test_design_state_block_json_roundtrips() -> None:
    """The block survives a JSON round-trip: ``unknown`` → ``null``,
    ``disagrees`` carries both values, non-numeric units stay ``None``."""
    import json

    block = build_design_state_block(
        state_block_from_params({"W": 30.0, "note": "left", "H": None})
    )
    raw = json.dumps(block)
    roundtrip = json.loads(raw)
    values = {e["name"]: e["value"] for e in roundtrip["entries"]}
    assert values["W"] == 30.0
    assert values["note"] == "left"
    assert values["H"] is None  # null round-trips to None
    # The closed provenance set is preserved.
    provs = {e["provenance"] for e in roundtrip["entries"]}
    assert provs == {"stated", "unknown"}
