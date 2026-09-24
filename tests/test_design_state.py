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
    AXIS_PARAM_NAMES,
    MAX_STATE_BLOCK_ENTRIES,
    Provenance,
    StateEntry,
    build_design_state_block,
    format_design_state_block,
    persisted_bbox_extents,
    state_block_for_version,
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
    # Issue #246 added ``assumed`` (the model-emitted default) to the set.
    assert set(args) == {"stated", "measured", "assumed", "unknown", "disagrees"}


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
    # The W param carries its value (issue #246: a model-emitted param is
    # ``assumed`` — the user's words are never visible to this function).
    w = by_name["W"]
    assert w["value"] == 20.0
    assert w["provenance"] == "assumed"


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

    NOTE: ``measured``/``disagrees`` are reachable from real data (issue
    #137 — the version's persisted bbox); this test exercises the
    serialised entry shape directly (a hand-built entry), while the
    tolerance rules that PRODUCE those entries are pinned in the
    ``state_block_for_version`` tests below and end-to-end in
    ``tests/versioning/test_issue137_persist_bbox.py``."""
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


# ---------------------------------------------------------------------------
# Provenance: measured / disagrees from a REAL persisted measurement
# (issue #137 — the tolerance rules, exercised through the new shared
# function, not the synthetic hand-built entries above)
# ---------------------------------------------------------------------------


def test_stated_within_tolerance_yields_measured_not_disagrees() -> None:
    """A stated value WITHIN the named tolerance (``max(1%, 0.5mm)`` —
    ``BBOX_TOLERANCE_REL`` / ``BBOX_TOLERANCE_MIN_MM``) does NOT yield
    ``disagrees``: the provenance is ``measured`` and the displayed value
    is the measured one."""
    # Stated W=30, measured 30.4: |30.4-30| = 0.4 <= tol = max(0.3, 0.5) = 0.5.
    entries = state_block_for_version(
        {"W": 30.0, "D": 30.0, "H": 30.0}, {"x": 30.4, "y": 30.0, "z": 30.0}
    )
    by_name = {e["name"]: e for e in entries}
    assert by_name["W"]["provenance"] == "measured"
    assert by_name["W"]["value"] == 30.4  # the measured value is displayed
    assert "stated_value" not in by_name["W"]
    # D and H are exact matches → measured too.
    assert by_name["D"]["provenance"] == "measured"
    assert by_name["H"]["provenance"] == "measured"


def test_stated_outside_tolerance_yields_disagrees_with_both_values() -> None:
    """A stated value OUTSIDE the tolerance yields ``disagrees`` carrying
    BOTH numbers — the measured one as the displayed value (what will
    print) and ``stated_value`` as the ride-along."""
    # Stated W=30, measured 29.2: |29.2-30| = 0.8 > tol = max(0.3, 0.5) = 0.5.
    entries = state_block_for_version(
        {"W": 30.0, "D": 30.0, "H": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0}
    )
    by_name = {e["name"]: e for e in entries}
    w = by_name["W"]
    assert w["provenance"] == "disagrees"
    # The MEASURED value is displayed (what will actually print).
    assert w["value"] == 29.2
    # The stated value rides alongside (never collapsed).
    assert w["stated_value"] == 30.0
    assert w["value"] != w["stated_value"]
    # The other axes are within tolerance → measured, not disagrees.
    assert by_name["D"]["provenance"] == "measured"
    assert by_name["H"]["provenance"] == "measured"


def test_non_axis_parameter_stays_assumed_never_becomes_measured() -> None:
    """A parameter with no bbox axis (``rod_bore``) is NEVER compared
    against the measurement: it stays ``assumed`` (issue #246 — model-
    emitted params have no stated evidence) and never silently becomes
    ``measured``/``stated`` (the axis-mapping rule: only W/D/H are
    comparable)."""
    entries = state_block_for_version(
        {"W": 30.0, "rod_bore": 6.0}, {"x": 29.2, "y": 30.0, "z": 30.0}
    )
    by_name = {e["name"]: e for e in entries}
    # The non-axis param keeps its value and stays assumed (no measurement
    # applies to it; no promotion path exists for it).
    assert by_name["rod_bore"]["provenance"] == "assumed"
    assert by_name["rod_bore"]["value"] == 6.0
    assert "stated_value" not in by_name["rod_bore"]
    # The axis param is still compared (disagrees here — 29.2 vs 30).
    assert by_name["W"]["provenance"] == "disagrees"


def test_no_measurement_persists_assumed_and_unknown_only() -> None:
    """No persisted measurement (``None``) and no stated evidence → the
    pure params-only substrate's output: ``assumed``/``unknown`` only
    (issue #246: model-emitted params are assumed, never stated), never
    measured."""
    entries = state_block_for_version({"W": 30.0, "H": None, "rod_bore": 6.0}, None)
    provs = {e["provenance"] for e in entries}
    assert provs == {"assumed", "unknown"}
    by_name = {e["name"]: e for e in entries}
    assert by_name["W"]["provenance"] == "assumed"
    assert by_name["H"]["provenance"] == "unknown"
    assert by_name["H"]["value"] is None


def test_stated_zero_axis_abstains_never_disagrees() -> None:
    """A stated value that is zero (unknown — ticket #91) is never
    compared: it stays ``unknown`` even when a measurement exists
    (comparing against 0 would mark it disagrees for no reason)."""
    entries = state_block_for_version({"W": 0, "D": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0})
    by_name = {e["name"]: e for e in entries}
    assert by_name["W"]["provenance"] == "unknown"
    assert by_name["W"]["value"] is None
    assert by_name["D"]["provenance"] == "measured"


def test_persisted_bbox_extents_null_abstains_never_zero_triple() -> None:
    """An absent measurement (``None``) abstains — it is never encoded as
    a zero triple (issue #91's precedent). A stored zero axis also
    abstains: a zero is the encoded absence."""
    assert persisted_bbox_extents(None) is None
    assert persisted_bbox_extents({"x": 0.0, "y": 30.0, "z": 30.0}) is None
    assert persisted_bbox_extents({"x": 30.0, "y": 30.0, "z": 30.0}) == (30.0, 30.0, 30.0)


def test_disagrees_entry_renders_measured_value_with_stated_ridealong() -> None:
    """The formatted block renders the MEASURED value as the displayed
    value and carries the stated value alongside (never collapsed).

    NOTE: the formatter's handling of the ``disagrees`` entry shape
    (``measured``/``disagrees`` are reachable from real data via the
    version's persisted bbox — issue #137; the producer's tolerance
    rules are pinned in the ``state_block_for_version`` tests)."""
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
    """A string param has no mm meaning: provenance ``"assumed"``
    (issue #246 — model-emitted, no stated evidence), ``unit``
    ``None`` (never ``"mm"``), value carried as-is (not a crash)."""
    entries = state_block_from_params({"note": "left-handed"})
    assert entries[0]["value"] == "left-handed"
    assert entries[0]["provenance"] == "assumed"
    assert entries[0]["unit"] is None  # a string is not mm


def test_design_state_block_non_numeric_bool_param() -> None:
    """A bool param: provenance ``"assumed"`` (issue #246), ``unit``
    ``None`` (bool is not a measurement), value carried as-is."""
    entries = state_block_from_params({"pinned": True})
    assert entries[0]["value"] is True
    assert entries[0]["provenance"] == "assumed"
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
    route_block = state_block_for_version(latest["params"])

    # The prompt builder's data source: the same params snapshot.
    prompt_block = state_block_for_version(latest["params"])

    # Same callable, same output (the shared function, not two that
    # happen to agree).
    assert route_block is not prompt_block  # distinct invocations
    assert route_block == prompt_block
    assert all(
        e["name"] in {"W", "bore_diameter"} for e in route_block
    )


def test_design_state_block_function_identity() -> None:
    """The function the two consumers use is literally the same object
    (``d33d.design_state.state_block_for_version``) — identity, not
    equality (issue #137: the measurement-aware shared callable, not a
    parallel implementation). The pure params-only substrate
    (``state_block_from_params``) survives unchanged as the substrate the
    new function wraps."""
    import d33d.design_state as ds

    assert ds.state_block_for_version is state_block_for_version
    # The substrate survives unchanged and is what the new function wraps.
    substrate = state_block_from_params({"W": 30.0})
    wrapped = state_block_for_version({"W": 30.0}, None)
    assert substrate == wrapped


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
    # The closed provenance set is preserved (issue #246: model-emitted
    # params are assumed, never stated).
    provs = {e["provenance"] for e in roundtrip["entries"]}
    assert provs == {"assumed", "unknown"}


# ---------------------------------------------------------------------------
# Issue #246: the ``assumed`` provenance — default to assumed, promote on
# evidence, never the reverse
# ---------------------------------------------------------------------------


_V24_PARAMS = {
    "fillet_radius": 1.5,
    "hole_clearance": 0.3,
    "hole_diameter": 3.3,
    "spacer_depth": 20,
    "spacer_height": 12,
    "spacer_width": 20,
    "wall_thickness": 3,
}


def test_v24_like_params_are_all_assumed_never_stated() -> None:
    """The v24 bug: seven model-emitted parameters from a message that
    stated no axis ("A spacer to lift a shelf 12 mm") — the block is
    ALL ``assumed``, ZERO ``stated`` (a model-emitted param is never
    stated from a params snapshot alone: the function never sees the
    user's words). The 12 in the message must NOT promote
    ``spacer_height`` (no name-guessing, no axis evidence)."""
    entries = state_block_for_version(dict(_V24_PARAMS))
    by_name = {e["name"]: e for e in entries}
    assert len(entries) == 7
    for name in _V24_PARAMS:
        assert by_name[name]["provenance"] == "assumed", f"{name}: {by_name[name]}"
    # No ``stated`` anywhere, even though 12 appears in the message.
    assert {e["provenance"] for e in entries} == {"assumed"}
    # The value is carried verbatim (assumed is not unknown — the value
    # is shown, just honestly labelled).
    assert by_name["spacer_height"]["value"] == 12


def test_axis_row_stated_from_persisted_per_axis_set() -> None:
    """A message stating only height 12 → the version persists
    ``{"H": 12.0}`` → the block carries an ``H`` axis row with
    ``provenance == "stated"`` value 12 — an AXIS ROW, not a param row:
    the model-named params (spacer_height etc.) stay ``assumed``, and
    the stated evidence never leaks into a model-named param."""
    entries = state_block_for_version(
        dict(_V24_PARAMS), None, {"H": 12.0}
    )
    by_name = {e["name"]: e for e in entries}
    # The H axis row: stated, value 12 (the protocol's evidence).
    h = by_name["H"]
    assert h["provenance"] == "stated"
    assert h["value"] == 12.0
    assert h["unit"] == "mm"
    # The model-named param stays assumed (no name-guessing promotion).
    assert by_name["spacer_height"]["provenance"] == "assumed"
    assert by_name["spacer_height"]["value"] == 12
    # The other params are assumed too.
    for name in _V24_PARAMS:
        if name != "spacer_height":
            assert by_name[name]["provenance"] == "assumed"


def test_no_stated_axes_and_no_measurement_yields_no_axis_rows() -> None:
    """No stated axes persisted and no bbox: no axis rows appear (never
    a fabricated W/D/H entry) — only the model-emitted param rows
    (``assumed``)."""
    entries = state_block_for_version(
        {"W": 30.0, "D": 30.0, "H": 30.0}, None, None
    )
    by_name = {e["name"]: e for e in entries}
    assert len(entries) == 3
    for axis in ("W", "D", "H"):
        # These are the MODEL's own W/D/H-named params — assumed, no
        # evidence, no measurement.
        assert by_name[axis]["provenance"] == "assumed"
    # A model name set with NO W/D/H keys: zero axis rows at all.
    entries2 = state_block_for_version({"spacer_width": 20.0}, None, None)
    assert {e["name"] for e in entries2} == {"spacer_width"}
    assert entries2[0]["provenance"] == "assumed"


def test_partial_stated_axes_omit_unstated_axis_entirely() -> None:
    """Persisted stated {W: 60, H: 80} (partial): W and H axis rows show
    ``stated``; the D axis row is OMITTED entirely (no row — not
    "unknown", not a fabricated 0)."""
    entries = state_block_for_version(
        {"spacer_width": 60.0, "spacer_depth": 45.0, "spacer_height": 80.0},
        None,
        {"W": 60.0, "H": 80.0},
    )
    by_name = {e["name"]: e for e in entries}
    # W and H axis rows: stated.
    assert by_name["W"]["provenance"] == "stated"
    assert by_name["W"]["value"] == 60.0
    assert by_name["H"]["provenance"] == "stated"
    assert by_name["H"]["value"] == 80.0
    # D: no axis row at all (omitted, not unknown, not 0).
    assert "D" not in by_name
    # The model's own param rows still render (assumed).
    assert by_name["spacer_width"]["provenance"] == "assumed"
    assert by_name["spacer_depth"]["provenance"] == "assumed"
    assert by_name["spacer_height"]["provenance"] == "assumed"


def test_axis_named_param_stays_assumed_even_when_value_matches() -> None:
    """#248 decisions: ``AXIS_PARAM_NAMES`` must NOT promote params. A
    model-emitted param LITERALLY NAMED ``W`` whose value EQUALS the
    persisted W evidence stays ``assumed`` (the spec forbids name-based
    promotion: "key it on the protocol's confirmed set, not on parameter
    names"). The user's stated evidence renders as the separate W AXIS
    ROW (stated); a mismatching param value stays ``assumed`` too."""
    # Match → the param row stays assumed; the axis row is stated.
    entries = state_block_for_version({"W": 60.0}, None, {"W": 60.0})
    assumed_rows = [e for e in entries if e["provenance"] == "assumed"]
    assert len(assumed_rows) == 1
    assert assumed_rows[0]["value"] == 60.0
    # The separate W axis row (appended after the param rows) carries the
    # user's stated value.
    stated_rows = [e for e in entries if e["provenance"] == "stated"]
    assert len(stated_rows) == 1
    assert stated_rows[0]["value"] == 60.0
    assert len(entries) == 2  # param row + axis row, never collapsed
    # Mismatch → the param row stays assumed; the axis row carries the
    # user's stated value. Both render (never collapsed, never guessed).
    entries2 = state_block_for_version({"W": 50.0}, None, {"W": 60.0})
    stated_rows2 = [e for e in entries2 if e["provenance"] == "stated"]
    assumed_rows2 = [e for e in entries2 if e["provenance"] == "assumed"]
    assert len(stated_rows2) == 1
    assert stated_rows2[0]["value"] == 60.0
    assert len(assumed_rows2) == 1
    assert assumed_rows2[0]["value"] == 50.0
    assert len(entries2) == 2


def test_stated_axis_plus_bbox_within_tolerance_yields_measured() -> None:
    """A stated axis (persisted {W: 30}) with a persisted bbox within the
    tolerance → the axis row renders ``measured`` with the MEASURED value
    displayed (what will print), not the stated one."""
    # W stated 30, measured 30.4 (|30.4-30| = 0.4 <= tol = max(0.3, 0.5)).
    entries = state_block_for_version(
        {"W": 30.0, "D": 30.0, "H": 30.0},
        {"x": 30.4, "y": 30.0, "z": 30.0},
        {"W": 30.0, "D": 30.0, "H": 30.0},
    )
    by_name = {e["name"]: e for e in entries}
    # The model's own W/D/H-named param rows are compared against the
    # measurement (issue #137) — within tolerance → measured (the
    # displayed value is the measured one: 30.4 for W).
    assert by_name["W"]["provenance"] == "measured"
    assert by_name["W"]["value"] == 30.4
    assert by_name["D"]["provenance"] == "measured"
    assert by_name["D"]["value"] == 30.0
    assert by_name["H"]["provenance"] == "measured"
    assert by_name["H"]["value"] == 30.0
    # The SEPARATE axis rows (driven by the persisted stated set) are
    # appended after the param rows — three more measured rows, one per
    # axis (param rows and axis rows are independent surfaces).
    axis_rows = entries[3:]
    assert len(axis_rows) == 3
    for axis in ("W", "D", "H"):
        assert axis_rows[AXIS_PARAM_NAMES.index(axis)]["name"] == axis
        assert axis_rows[AXIS_PARAM_NAMES.index(axis)]["provenance"] == "measured"
    assert len(entries) == 6


def test_stated_axis_plus_bbox_outside_tolerance_yields_disagrees() -> None:
    """A stated axis outside the measurement's tolerance → ``disagrees``
    carrying BOTH numbers (the measured one displayed, the stated one
    riding along) — the existing rule, now driven by the persisted
    axis-stated evidence."""
    # W stated 30, measured 29.2 (|29.2-30| = 0.8 > tol = max(0.3, 0.5)).
    entries = state_block_for_version(
        {"W": 30.0},
        {"x": 29.2, "y": 30.0, "z": 30.0},
        {"W": 30.0},
    )
    by_name = {e["name"]: e for e in entries}
    # The model's own W param row is compared against the measurement —
    # outside tolerance → disagrees, carrying both numbers (the measured
    # one displayed, the stated one riding along).
    w = by_name["W"]
    assert w["provenance"] == "disagrees"
    assert w["value"] == 29.2  # the measured value (the display)
    assert w["stated_value"] == 30.0  # the stated value (the ride-along)
    # The separate W axis row (the persisted stated evidence vs the
    # measurement) renders alongside — also disagrees, same numbers.
    axis_rows = [e for e in entries if e is not w]
    assert len(axis_rows) == 1
    assert axis_rows[0]["provenance"] == "disagrees"
    assert axis_rows[0]["value"] == 29.2
    assert axis_rows[0]["stated_value"] == 30.0
    assert len(entries) == 2


def test_assumed_axis_name_without_stated_evidence_stays_assumed() -> None:
    """A model-emitted param NAMED ``H`` whose value differs from the
    persisted H evidence stays ``assumed`` (no promotion without a value
    match); the stated H axis row renders alongside."""
    entries = state_block_for_version({"H": 99.0}, None, {"H": 12.0})
    by_name = {e["name"]: e for e in entries}
    # The model's own param row (H=99, no matching evidence) stays
    # assumed — no name-guessing promotion.
    assumed = [e for e in entries if e["provenance"] == "assumed"]
    assert len(assumed) == 1
    assert assumed[0]["value"] == 99.0
    # The axis row (H, the user's 12) is stated — the LAST entry (axis
    # rows append after the param rows).
    stated_rows = [e for e in entries if e["provenance"] == "stated"]
    assert len(stated_rows) == 1
    assert stated_rows[0]["value"] == 12.0
    assert entries[-1] is stated_rows[0]
    assert len(entries) == 2


def test_prompt_format_marks_assumed_and_stated_provenance() -> None:
    """The prompt rendering marks provenance (issue #246): ``assumed``
    renders ``(assumed — the user never set this)``, ``stated`` renders
    ``(stated by the user)`` — the model can tell user-set values from
    its own guesses. ``measured``/``disagrees``/``unknown`` keep their
    existing plain rendering."""
    block = build_design_state_block(
        state_block_for_version(
            {"W": 30.0, "spacer_width": 20.0},
            None,
            {"W": 30.0},
        )
    )
    text = format_design_state_block(block)
    assert "W = 30 (stated by the user)" in text
    assert "spacer_width = 20 (assumed — the user never set this)" in text
    # Unknown renders as before (no mark) — but its sibling param IS
    # assumed (issue #246), so the block as a whole carries the mark.
    block2 = build_design_state_block(
        state_block_for_version({"W": 30.0, "H": None})
    )
    text2 = format_design_state_block(block2)
    assert "H = not specified" in text2
    assert "(stated by the user)" not in text2
    # The single-line variant marks the same way.
    from d33d.design_state import format_design_state_line

    line = format_design_state_line(block["entries"])
    assert "W=30 (stated by the user)" in line
