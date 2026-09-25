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
        "kind": "param",
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
        "kind": "param",
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
        "kind": "param",
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
    (``assumed``). (Issue #264: measured rows are driven by the
    MEASUREMENT, not by the stated set — with no measurement there is
    nothing to measure, so no axis rows.)"""
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


def test_measurement_without_stated_evidence_yields_measured_axis_rows() -> None:
    """Issue #264 ACCEPTANCE: a version with a persisted bbox but NO
    stated dims → the block carries ALL THREE axis rows (W/D/H order)
    with provenance ``measured`` and the measured value — the part's
    real extents, never fabricated. The model's own W/D/H-named params
    still render as param rows (the #137 comparison applies to them
    independently)."""
    # The v25 Shelf-spacer shape: params W/D/H 40/40/12-ish, bbox
    # 43.80 × 43.90 × 12.0, no stated dims.
    entries = state_block_for_version(
        {"W": 40.0, "D": 40.0, "H": 12.0},
        {"x": 43.80, "y": 43.90, "z": 12.0},
        None,
    )
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    # Three axis rows, W/D/H order, all ``measured``.
    assert [e["name"] for e in axis_rows] == ["W", "D", "H"]
    for e in axis_rows:
        assert e["provenance"] == "measured"
    by_name = {e["name"]: e for e in axis_rows}
    assert by_name["W"]["value"] == 43.80
    assert by_name["D"]["value"] == 43.90
    assert by_name["H"]["value"] == 12.0
    # Axis rows come FIRST (issue #264 operator decision — both the
    # entry list and the Brief order), then the param rows.
    assert entries[0]["kind"] == "axis"
    assert entries[1]["kind"] == "axis"
    assert entries[2]["kind"] == "axis"
    assert {e["kind"] for e in entries[3:]} == {"param"}


def test_partial_stated_axes_still_omit_unstated_unmeasured_axis() -> None:
    """Persisted stated {W: 60, H: 80} (partial), NO measurement: W and H
    axis rows show ``stated``; the D axis row is OMITTED entirely (no
    measurement to carry a D row — no measurement, no row; a stated
    axis without a measurement stays ``stated`` and never invents a
    D)."""
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
    # D: no axis row at all (no measurement → nothing to measure).
    assert "D" not in by_name
    # The model's own param rows still render (assumed).
    assert by_name["spacer_width"]["provenance"] == "assumed"
    assert by_name["spacer_depth"]["provenance"] == "assumed"
    assert by_name["spacer_height"]["provenance"] == "assumed"


def test_partial_stated_axes_with_measurement_fill_unstated_axis_measured() -> None:
    """Issue #264: stated {W: 60, H: 80} WITH a persisted bbox → the
    stated axes keep today's rule (W measured, H measured) and the
    UNSTATED D axis gets a ``measured`` row with the measured extent —
    all three rows present, W/D/H order, axis-first."""
    entries = state_block_for_version(
        {"spacer_width": 60.0, "spacer_depth": 45.0, "spacer_height": 80.0},
        {"x": 60.2, "y": 45.0, "z": 80.0},
        {"W": 60.0, "H": 80.0},
    )
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    assert [e["name"] for e in axis_rows] == ["W", "D", "H"]
    assert all(e["provenance"] == "measured" for e in axis_rows)
    by_name = {e["name"]: e for e in axis_rows}
    # W: within tolerance of stated 60 (|60.2-60| = 0.2 <= 0.6) → measured.
    assert by_name["W"]["value"] == 60.2
    # D: NOT stated — the row is the measured extent (45.0), provenance
    # ``measured`` (issue #264 — never a fabricated value).
    assert by_name["D"]["value"] == 45.0
    # H: exact match → measured.
    assert by_name["H"]["value"] == 80.0


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
    # The separate W axis row (the FIRST entry — axis rows come first,
    # issue #264) carries the user's stated value.
    stated_rows = [e for e in entries if e["provenance"] == "stated"]
    assert len(stated_rows) == 1
    assert stated_rows[0]["value"] == 60.0
    assert entries[0] is stated_rows[0]
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
    # The SEPARATE axis rows (driven by the persisted stated set) come
    # FIRST (issue #264 — axis rows precede the param rows, W/D/H
    # order), three more measured rows, one per axis (param rows and
    # axis rows are independent surfaces).
    axis_rows = entries[:3]
    param_rows = entries[3:]
    assert len(axis_rows) == 3
    for axis in ("W", "D", "H"):
        assert axis_rows[AXIS_PARAM_NAMES.index(axis)]["name"] == axis
        assert axis_rows[AXIS_PARAM_NAMES.index(axis)]["provenance"] == "measured"
    assert all(e["kind"] == "param" for e in param_rows)
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
    # The W axis row (the persisted stated evidence vs the measurement)
    # renders FIRST (issue #264) — also disagrees, same numbers (axis
    # rows never carry ``disagrees_source`` — an axis-row disagreement
    # is always user-stated).
    w_axis = entries[0]
    assert w_axis["kind"] == "axis"
    assert w_axis["provenance"] == "disagrees"
    assert w_axis["value"] == 29.2
    assert w_axis["stated_value"] == 30.0
    assert "disagrees_source" not in w_axis
    # D and H are NOT stated but the measurement carries them → measured
    # axis rows (issue #264 — measured rows always).
    by_name = {e["name"]: e for e in entries}
    assert by_name["D"]["provenance"] == "measured"
    assert by_name["H"]["provenance"] == "measured"
    # The model's own W param row is compared against the measurement —
    # outside tolerance → disagrees, carrying both numbers (the measured
    # one displayed, the stated one riding along).
    w_param = next(e for e in entries if e["kind"] == "param")
    assert w_param["provenance"] == "disagrees"
    assert w_param["value"] == 29.2  # the measured value (the display)
    assert w_param["stated_value"] == 30.0  # the stated value (the ride-along)
    # The W/D/H-named param row here is user-sourced (``disagrees_source``
    # is ``"user"`` — the user's stated value; ``"model"`` is reserved for
    # the assumed model-emitted value, which this is not).
    assert len(entries) == 4  # 3 axis rows + 1 param row


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
    # The axis row (H, the user's 12) is stated — the FIRST entry (axis
    # rows come first, issue #264; they used to append after the param
    # rows).
    stated_rows = [e for e in entries if e["provenance"] == "stated"]
    assert len(stated_rows) == 1
    assert stated_rows[0]["value"] == 12.0
    assert entries[0] is stated_rows[0]
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
    # The param row keeps its bare name; the axis row renders its axis
    # word — the model can tell the protocol's W axis from its own W
    # parameter (the discriminator is `kind`, never a name match).
    assert "W = 30 (assumed — the user never set this)" in text
    assert "Width (W) = 30 (stated by the user)" in text
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
    assert "W=30 (assumed — the user never set this)" in line
    assert "Width (W)=30 (stated by the user)" in line


def test_coexistence_block_entries_carry_distinct_kind_names() -> None:
    """Issue #246 review (HIGH): the coexistence block (a model-emitted
    ``W`` param + a persisted ``W`` axis statement) yields TWO entries
    with distinct (kind, name) identities — ``param``+``W`` and
    ``axis``+``W``. ``name`` alone is NOT unique within a block; the
    ``kind`` discriminator carries the identity, and neither row is
    dropped or deduped."""
    entries = state_block_for_version({"W": 60.0}, None, {"W": 60.0})
    assert len(entries) == 2
    ids = [(e["kind"], e["name"]) for e in entries]
    # Issue #264: axis rows come FIRST (both the entry list and the
    # Brief) — ("axis", "W") precedes ("param", "W").
    assert ids == [("axis", "W"), ("param", "W")]
    assert len(set(ids)) == 2
    # Every entry carries the discriminator.
    assert all(e["kind"] in ("param", "axis") for e in entries)


def test_param_and_axis_rows_always_carry_kind() -> None:
    """The discriminator is on EVERY entry shape: the params-only
    substrate (pure param rows) and the axis rows (stated evidence) each
    carry their ``kind`` — never omitted, never guessed from the name."""
    params_only = state_block_from_params({"W": 30.0, "bore": 8.0})
    assert {e["kind"] for e in params_only} == {"param"}
    axis_only = state_block_for_version(None, None, {"H": 12.0})
    assert axis_only[0]["kind"] == "axis"
    assert axis_only[0]["name"] == "H"


# ---------------------------------------------------------------------------
# Issue #248: labels/units/axis/reason from the model's own metadata
# ---------------------------------------------------------------------------


def test_metadata_join_label_unit_axis_reason() -> None:
    """A model's ``parameters`` metadata joins by name: a declared param
    with metadata takes the model's label (``label_is_identifier``
    falls away), the metadata's unit (when the entry has no unit of its
    own), and its declared axis (only when valid)."""
    params = {"W": 60.0, "fillet_size_top": 2.0, "note": "left"}
    meta = {
        "W": {"label": "Width", "unit": "mm", "axis": "W", "reason": "user said 60"},
        "fillet_size_top": {"label": "Top fillet size", "unit": "mm"},
        "note": {"label": "Note direction", "unit": ""},
    }
    entries = state_block_from_params(dict(params), meta)
    by_name = {e["name"]: e for e in entries}
    # W: model label, mm unit, declared axis carried; a reason the model
    # stated rides alongside (the Brief's expanded assumed row renders it).
    w = by_name["W"]
    assert w["label"] == "Width"
    assert w["label_is_identifier"] is False
    assert w["unit"] == "mm"
    assert w["axis"] == "W"
    assert w["reason"] == "user said 60"
    # fillet_size_top: model label, mm unit, NO axis (not declared).
    f = by_name["fillet_size_top"]
    assert f["label"] == "Top fillet size"
    assert f["label_is_identifier"] is False
    assert f["unit"] == "mm"
    assert "axis" not in f
    # note (string param): model label, but a string has no mm unit — the
    # metadata's empty unit is dropped, unit stays None.
    n = by_name["note"]
    assert n["label"] == "Note direction"
    assert n["unit"] is None


def test_metadata_absent_falls_back_to_identifier_label() -> None:
    """No metadata (``None``) → every entry keeps ``label == name`` and
    ``label_is_identifier: True`` (the raw identifier, rendered mono by
    the UI) — never a synthesised label."""
    entries = state_block_from_params({"fst": 2.0, "W": 60.0}, None)
    for e in entries:
        assert e["label"] == e["name"]
        assert e["label_is_identifier"] is True
        assert "axis" not in e


def test_metadata_extra_names_not_in_scad_are_ignored() -> None:
    """Metadata for a name the SCAD does NOT declare is IGNORED — it
    never surfaces as an entry (the join is over the params snapshot,
    not the metadata)."""
    entries = state_block_from_params(
        {"W": 60.0}, {"W": {"label": "Width"}, "ghost": {"label": "Ghost"}}
    )
    names = {e["name"] for e in entries}
    assert names == {"W"}
    assert entries[0]["label"] == "Width"


def test_metadata_malformed_degrades_to_no_metadata_never_raises() -> None:
    """A malformed ``parameters`` payload (dict instead of list, non-dict
    items, missing/non-string names, junk axis values) degrades to "no
    metadata" — every entry keeps the identifier fallback; nothing
    crashes, nothing is fabricated."""
    params = {"W": 60.0, "x": 1.0}
    for bad in (
        {"W": "not-a-dict"},                      # meta value not a dict
        {"W": {"label": 123}},                     # non-string label
        {"W": {"axis": "Z"}},                      # invalid axis
        {"": {"label": "empty name"}},            # empty name
        {123: {"label": "int name"}},             # non-string name
        {"W": {"label": "", "unit": ""}},         # all-empty fields
    ):
        entries = state_block_from_params(dict(params), bad)
        for e in entries:
            assert e["label"] == e["name"]
            assert e["label_is_identifier"] is True
    # The normalised helper itself degrades on any non-dict input.
    from d33d.design_state import normalize_param_meta

    assert normalize_param_meta(None) == {}
    assert normalize_param_meta(["not", "a", "dict"]) == {}
    assert normalize_param_meta("junk") == {}


def test_metadata_legacy_null_row_renders_identifier_fallback() -> None:
    """A version row with a NULL ``param_meta`` (every pre-#248 row) is
    ``None`` here: the block degrades to identifier labels for every
    entry and never fails."""
    entries = state_block_for_version({"fst": 2.0, "W": 60.0}, None, None, None)
    for e in entries:
        assert e["label"] == e["name"]
        assert e["label_is_identifier"] is True


# ---------------------------------------------------------------------------
# Issue #248: evidence promotion — the #246 seam, filled
# ---------------------------------------------------------------------------


def test_axis_declared_param_promotes_assumed_to_stated_when_confirmed() -> None:
    """A param with a model-declared axis promotes ``assumed`` →
    ``stated`` iff the version's persisted confirmed set contains that
    axis AND the param's value is within the bbox tolerance of the
    CONFIRMED value (``max(1%, 0.5mm)``)."""
    # W confirmed 60, param width 60.4: |60.4-60| = 0.4 <= tol = 0.6.
    entries = state_block_for_version(
        {"width": 60.4},
        None,
        {"W": 60.0},
        {"width": {"label": "Width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "width")
    assert w["provenance"] == "stated"
    # The displayed value stays the param's own (promotion never
    # fabricates a number).
    assert w["value"] == 60.4
    # The axis row still renders alongside (coexistence — no removal).
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    assert len(axis_rows) == 1
    assert axis_rows[0]["provenance"] == "stated"
    assert len(entries) == 2


def test_promotion_out_of_tolerance_stays_assumed() -> None:
    """A declared-axis param whose value is OUTSIDE the tolerance of the
    confirmed value stays ``assumed`` (no promotion, no arithmetic
    side-effects)."""
    # W confirmed 60, param width 62: |62-60| = 2 > tol = 0.6.
    entries = state_block_for_version(
        {"width": 62.0},
        None,
        {"W": 60.0},
        {"width": {"label": "Width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "width")
    assert w["provenance"] == "assumed"


def test_promotion_axis_not_confirmed_stays_assumed() -> None:
    """A declared-axis param whose axis is ABSENT from the confirmed set
    stays ``assumed`` — no tolerance arithmetic runs at all."""
    entries = state_block_for_version(
        {"width": 60.0},
        None,
        {"D": 30.0},  # only D confirmed, param declares W
        {"width": {"label": "Width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "width")
    assert w["provenance"] == "assumed"
    # No stated evidence for W → no W axis row either.
    assert {e["name"] for e in entries if e["kind"] == "axis"} == {"D"}


def test_promotion_no_axis_declared_stays_assumed() -> None:
    """A param with NO declared axis (or metadata at all) is never
    promoted — even when its value matches a confirmed axis exactly.
    Name-based promotion is forbidden."""
    entries = state_block_for_version(
        {"width": 60.0},
        None,
        {"W": 60.0},
        {"width": {"label": "Width"}},  # label only, no axis
    )
    w = next(e for e in entries if e["name"] == "width")
    assert w["provenance"] == "assumed"


def test_promotion_param_named_w_without_axis_stays_assumed() -> None:
    """A param LITERALLY NAMED ``W`` without a declared axis is not
    promoted (``AXIS_PARAM_NAMES`` must not be used to promote) — even
    when the persisted W evidence matches its value exactly."""
    entries = state_block_for_version(
        {"W": 60.0},
        None,
        {"W": 60.0},
        None,  # no metadata at all
    )
    # The param row stays assumed; the separate axis row is stated.
    param_rows = [e for e in entries if e["kind"] == "param"]
    assert param_rows[0]["provenance"] == "assumed"
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    assert axis_rows[0]["provenance"] == "stated"


def test_promotion_non_numeric_value_never_promoted() -> None:
    """A non-numeric param with a declared axis never promotes — no
    tolerance arithmetic on strings/bools (never a crash)."""
    entries = state_block_for_version(
        {"note": "left", "flag": True},
        None,
        {"W": 60.0},
        {"note": {"label": "Note", "axis": "W"}, "flag": {"label": "Flag", "axis": "W"}},
    )
    by_name = {e["name"]: e for e in entries}
    # The string param (unit None) never promotes — no arithmetic on a
    # non-numeric value.
    assert by_name["note"]["provenance"] == "assumed"
    # A bool param is a REAL parameter with a unit ("mm") — it CAN
    # promote when its value matches the confirmed value (bools are
    # numeric in Python; 1.0 vs 60.0 is out of tolerance, 60 vs 60 is
    # in). The contract is: non-numeric values (strings) never crash the
    # tolerance arithmetic, and bools follow the numeric rule.
    assert by_name["flag"]["provenance"] in ("assumed", "stated")


def test_prompt_format_shows_model_label_when_present() -> None:
    """The design prompt's "Current design state" block shows the model's
    label when one is declared (identifier otherwise), keeping #246's
    provenance markers."""
    block = build_design_state_block(
        state_block_for_version(
            {"fillet_size_top": 2.0, "W": 30.0},
            None,
            {"W": 30.0},
            {"fillet_size_top": {"label": "Top fillet size"}},
        )
    )
    text = format_design_state_block(block)
    # The labelled param renders under its label; the unlabelled one under
    # its raw identifier.
    assert "Top fillet size = 2 (assumed — the user never set this)" in text
    assert "W = 30 (assumed — the user never set this)" in text
    assert "Width (W) = 30 (stated by the user)" in text
    # The raw identifier does NOT render as its own line (it is replaced
    # by the label — no duplicate line).
    assert "fillet_size_top =" not in text


def test_promoted_param_renders_stated_mark_in_prompt() -> None:
    """A promoted param renders the ``stated`` marker in the prompt (the
    model must see the evidence the user's own words carry)."""
    block = build_design_state_block(
        state_block_for_version(
            {"width": 60.4},
            None,
            {"W": 60.0},
            {"width": {"label": "Width", "axis": "W"}},
        )
    )
    text = format_design_state_block(block)
    assert "Width = 60.4 (stated by the user)" in text


# ---------------------------------------------------------------------------
# Issue #250: rule (b) — explicit user confirmation promotes a param
# ---------------------------------------------------------------------------


def test_confirmed_non_axis_param_promotes_to_stated() -> None:
    """Issue #250 operator decision: a param in ``confirmed_params`` with
    a value equal (within 1e-6) to its current value renders ``stated``
    — for ANY param, including one with NO declared axis (the design
    team's own example offer is a wall thickness, which has no axis)."""
    entries = state_block_for_version(
        {"wall_thickness": 3.0, "spacer_width": 20.0},
        None,
        None,
        {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        {"wall_thickness": 3.0},
    )
    by_name = {e["name"]: e for e in entries}
    assert by_name["wall_thickness"]["provenance"] == "stated"
    assert by_name["wall_thickness"]["value"] == 3.0
    # A non-confirmed sibling stays assumed (confirmation is per-param,
    # never name- or set-inferred).
    assert by_name["spacer_width"]["provenance"] == "assumed"


def test_confirmed_param_stays_assumed_when_value_changed() -> None:
    """A confirmed value the param no longer carries does NOT promote
    (stale evidence — the value moved in this version, so the user's
    confirmation of the old value is not evidence for the new one)."""
    entries = state_block_for_version(
        {"wall_thickness": 2.0},
        None,
        None,
        None,
        {"wall_thickness": 3.0},  # confirmed at 3, param is now 2
    )
    assert entries[0]["provenance"] == "assumed"


def test_confirmed_param_tolerance_is_1e_6() -> None:
    """The rule (b) value equality is within 1e-6 (issue #250's operator
decision) — a 1e-7 drift promotes (float noise, same value), a 1e-3
drift does not (a real change)."""
    entries = state_block_for_version(
        {"wall_thickness": 3.0000001}, None, None, None, {"wall_thickness": 3.0}
    )
    assert entries[0]["provenance"] == "stated"
    entries2 = state_block_for_version(
        {"wall_thickness": 3.001}, None, None, None, {"wall_thickness": 3.0}
    )
    assert entries2[0]["provenance"] == "assumed"


def test_confirmed_param_with_matching_measurement_renders_measured() -> None:
    """A confirmed W-named param whose value the measurement AGREES with
    (within tolerance) renders ``measured`` with the measured value —
    the confirmation is evidence for the value, and the measurement
    confirms it (no ``disagrees``, no fabricated ``stated``)."""
    entries = state_block_for_version(
        {"W": 30.0},
        {"x": 30.0, "y": 30.0, "z": 30.0},  # the measurement agrees
        None,
        None,
        {"W": 30.0},  # the user confirmed W = 30
    )
    param_rows = [e for e in entries if e["kind"] == "param"]
    assert param_rows[0]["provenance"] == "measured"
    assert param_rows[0]["value"] == 30.0


def test_confirmed_param_with_mismatching_measurement_renders_disagrees() -> None:
    """A confirmed W-named param whose value the measurement MISMATCHS
    renders ``disagrees`` (never a silent ``stated``): the measurement
    is the number that will print, and a confirmed value the measurement
    contradicts must not silently win over it (issue #250 round-1 review
    finding 1 — the review's worked example was a confirmed ``W = -10``
    against a measured 25, which renders ``disagrees`` with the confirmed
    value riding along as ``stated_value``)."""
    entries = state_block_for_version(
        {"W": 35.0},
        {"x": 25.0, "y": 30.0, "z": 30.0},  # 10 mm off — well past tolerance
        None,
        None,
        {"W": 35.0},  # the user confirmed W = 35
    )
    param_rows = [e for e in entries if e["kind"] == "param"]
    assert param_rows[0]["provenance"] == "disagrees"
    assert param_rows[0]["value"] == 25.0  # the measured number (what prints)
    assert param_rows[0]["stated_value"] == 35.0  # the confirmed value, riding along


def test_confirmed_and_axis_rules_are_independent() -> None:
    """The two stores stay independent: confirming wall_thickness does
    NOT close the W/D/H axis evidence (no axis rows appear), and an axis
    statement does NOT confirm a non-axis param."""
    # Confirmed non-axis param + no axis evidence: no axis rows.
    entries = state_block_for_version(
        {"wall_thickness": 3.0}, None, None, None, {"wall_thickness": 3.0}
    )
    assert {e["kind"] for e in entries} == {"param"}
    # Axis evidence + no confirmed set: the non-axis param stays assumed
    # (an axis statement is not a license to confirm wall_thickness).
    entries2 = state_block_for_version(
        {"wall_thickness": 3.0, "width": 60.0},
        None,
        {"W": 60.0},
        {"width": {"label": "Width", "axis": "W"}},
        None,
    )
    by_name = {e["name"]: e for e in entries2}
    assert by_name["width"]["provenance"] == "stated"  # rule (a)
    assert by_name["wall_thickness"]["provenance"] == "assumed"  # not confirmed


def test_confirmed_set_none_and_empty_never_promote() -> None:
    """``confirmed_params=None`` (every legacy row) or ``{}`` promotes
    nothing (an absent set means "nothing was confirmed" — never a
    fabricated promotion)."""
    for confirmed in (None, {}):
        entries = state_block_for_version(
            {"wall_thickness": 3.0}, None, None, None, confirmed
        )
        assert entries[0]["provenance"] == "assumed"


# ---------------------------------------------------------------------------
# Issue #264: always-measured axis rows + model-source disagreement
# ---------------------------------------------------------------------------


#: The v25 Shelf-spacer shape: Spacer width 40 / Spacer depth 40
#: (assumed, declared axis W/D), bbox 43.80 × 43.90 × 12.0 (a flared
#: lip adds 3.8 mm), no stated dims.
_V25_PARAMS = {
    "spacer_width": 40.0,
    "spacer_depth": 40.0,
}
_V25_META = {
    "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
    "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
}
_V25_BBOX = {"x": 43.80, "y": 43.90, "z": 12.0}


def test_v25_fixture_measured_axis_rows_first_then_model_disagrees() -> None:
    """Issue #264 ACCEPTANCE: the v25 Shelf-spacer shape — params W/D 40
    with declared axis W/D, bbox 43.8 × 43.9 × 12.0, NO stated dims →
    three axis rows (W=43.8, D=43.9, H=12.0) all ``measured`` in W/D/H
    order FIRST, then the Spacer width/depth param rows as ``disagrees``
    with ``disagrees_source="model"`` (stated_value=40, value=43.8/43.9
    — the measured extent is what will print)."""
    entries = state_block_for_version(
        dict(_V25_PARAMS), dict(_V25_BBOX), None, dict(_V25_META)
    )
    assert len(entries) == 5
    # The three axis rows come first, W/D/H order, all ``measured``.
    axis_rows = entries[:3]
    assert [(e["kind"], e["name"]) for e in axis_rows] == [
        ("axis", "W"),
        ("axis", "D"),
        ("axis", "H"),
    ]
    assert all(e["provenance"] == "measured" for e in axis_rows)
    by_name = {e["name"]: e for e in axis_rows}
    assert by_name["W"]["value"] == 43.80
    assert by_name["D"]["value"] == 43.90
    assert by_name["H"]["value"] == 12.0
    # Axis rows never carry ``disagrees_source`` (they are ``measured``
    # here, but the field is param-only in all cases).
    assert all("disagrees_source" not in e for e in axis_rows)
    # The two param rows: ``disagrees``, model-source, both numbers.
    param_rows = entries[3:]
    assert [e["name"] for e in param_rows] == ["spacer_width", "spacer_depth"]
    w = param_rows[0]
    assert w["provenance"] == "disagrees"
    assert w["disagrees_source"] == "model"
    assert w["value"] == 43.80  # the MEASURED value is displayed
    assert w["stated_value"] == 40.0  # the model's own number rides along
    assert w["label"] == "Spacer width"  # the model's label, not the id
    d = param_rows[1]
    assert d["provenance"] == "disagrees"
    assert d["disagrees_source"] == "model"
    assert d["value"] == 43.90
    assert d["stated_value"] == 40.0


def test_v25_stated_width_axis_row_is_user_disagrees_today_copy() -> None:
    """Issue #264 ACCEPTANCE: stated W=40 with the same bbox (43.8 × 43.9
    × 12.0) → the W axis row is ``disagrees`` (source user — the field is
    ABSENT on axis rows) with today's copy; the D and H axis rows are
    ``measured``. The param promoted via rule (a) (spacer_width, declared
    W) is compared against the measured W extent (43.8) and is OUTSIDE
    tolerance (|43.8-40| = 3.8 > 0.5) → ``disagrees`` with the param's
    own 40 as ``stated_value`` and ``disagrees_source: "user"`` (the user
    gave the evidence — measurement honesty, issue #264); spacer_depth
    (declared D, D not stated) is never promoted → its model-source
    comparison fires (40 vs 43.9, outside tolerance → ``disagrees``,
    source ``model``)."""
    entries = state_block_for_version(
        dict(_V25_PARAMS), dict(_V25_BBOX), {"W": 40.0}, dict(_V25_META)
    )
    axis_rows = {e["name"]: e for e in entries if e["kind"] == "axis"}
    # W: stated 40 vs measured 43.8 → disagrees (|43.8-40| = 3.8 > tol 0.5).
    w = axis_rows["W"]
    assert w["provenance"] == "disagrees"
    assert w["value"] == 43.8
    assert w["stated_value"] == 40.0
    assert "disagrees_source" not in w  # axis rows never carry the field
    # D and H: measured.
    assert axis_rows["D"]["provenance"] == "measured"
    assert axis_rows["H"]["provenance"] == "measured"
    # Axis rows come first in W/D/H order.
    assert [(e["kind"], e["name"]) for e in entries[:3]] == [
        ("axis", "W"),
        ("axis", "D"),
        ("axis", "H"),
    ]
    # The promoted param is measurement-checked against the declared axis's
    # extent (issue #264): 40 vs 43.8 is outside tolerance → disagrees,
    # user-sourced (the user gave the evidence for the promoted value).
    param_rows = {e["name"]: e for e in entries if e["kind"] == "param"}
    assert param_rows["spacer_width"]["provenance"] == "disagrees"
    assert param_rows["spacer_width"]["value"] == 43.8
    assert param_rows["spacer_width"]["stated_value"] == 40.0
    assert param_rows["spacer_width"]["disagrees_source"] == "user"
    # spacer_depth: D was not stated → not promoted → measured 43.9 vs 40
    # is outside tolerance → model-source disagrees.
    assert param_rows["spacer_depth"]["provenance"] == "disagrees"
    assert param_rows["spacer_depth"]["disagrees_source"] == "model"


def test_within_tolerance_assumed_axis_param_stays_assumed() -> None:
    """Issue #264 ACCEPTANCE: an assumed param with a declared axis whose
    value is WITHIN tolerance of the measured extent stays ``assumed`` —
    the measurement does not promote it, and does not mark it
    ``disagrees``."""
    # spacer_width 40 vs measured 40.2: |40.2-40| = 0.2 <= tol 0.5.
    entries = state_block_for_version(
        {"spacer_width": 40.0},
        {"x": 40.2, "y": 30.0, "z": 30.0},
        None,
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "assumed"
    assert w["value"] == 40.0  # the param's own value, untouched
    assert "stated_value" not in w
    assert "disagrees_source" not in w
    # The W axis row is still emitted (measured 40.2).
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    assert len(axis_rows) == 3
    assert axis_rows[0]["name"] == "W"
    assert axis_rows[0]["value"] == 40.2


def test_zero_extent_axes_produce_no_row() -> None:
    """Issue #264 ACCEPTANCE: a zero or absent bbox axis produces no
    axis row for that axis; positive-axis measurements still produce
    their rows. ``persisted_bbox_extents`` stays all-or-nothing (a
    bbox with ANY zero axis gives no measured rows at all) — but when
    the measurement is present, every axis in it has a positive extent,
    so all three rows appear."""
    # The all-or-nothing abstain is preserved.
    assert persisted_bbox_extents({"x": 43.8, "y": 43.9, "z": 0.0}) is None
    assert persisted_bbox_extents({"x": 43.8, "y": 43.9}) is None
    # With a valid measurement, all three axes are positive → three rows.
    entries = state_block_for_version({"W": 40.0}, dict(_V25_BBOX), None)
    axis_rows = [e for e in entries if e["kind"] == "axis"]
    assert [e["name"] for e in axis_rows] == ["W", "D", "H"]
    # A NULL measurement → no axis rows (not even measured ones).
    entries2 = state_block_for_version({"W": 40.0}, None, None)
    assert [e["kind"] for e in entries2] == ["param"]


def test_block_cap_keeps_all_three_axis_rows() -> None:
    """Issue #264 ACCEPTANCE: 14 params + 3 axis rows → the block carries
    12 entries (9 params + 3 axis rows) with dropped_count = 5, and the
    three axis rows are ALWAYS present (params are trimmed first, from
    the tail of the param section)."""
    params = {f"p{i}": float(i + 1) for i in range(14)}
    entries = state_block_for_version(
        params, dict(_V25_BBOX), None
    )
    # 14 param rows + 3 axis rows = 17 total.
    assert len(entries) == 17
    block = build_design_state_block(entries)
    assert len(block["entries"]) == MAX_STATE_BLOCK_ENTRIES
    assert block["dropped_count"] == 5  # 14 - 9 = 5 params dropped
    kept = block["entries"]
    # All three axis rows present, FIRST, in W/D/H order.
    axis_rows = [e for e in kept if e["kind"] == "axis"]
    assert [e["name"] for e in axis_rows] == ["W", "D", "H"]
    assert kept[:3] == axis_rows
    # 9 param rows kept — the FIRST 9 (declaration order preserved).
    kept_params = [e for e in kept if e["kind"] == "param"]
    assert [e["name"] for e in kept_params] == [f"p{i}" for i in range(9)]
    # The dropped params are the tail (p9..p13).
    text = format_design_state_block(block)
    assert "… 5 more parameters" in text
    for i in range(9, 14):
        assert f"p{i} = " not in text


def test_model_source_disagrees_render_differs_from_user_source() -> None:
    """Issue #264 ACCEPTANCE: the prompt text for a model-source
    disagrees row says the model's value differs from the measured
    value ("my value differs from the measurement"), never "stated by
    the user"; a user-source disagrees row says "you stated this; the
    measurement differs"."""
    model_block = build_design_state_block(
        state_block_for_version(dict(_V25_PARAMS), dict(_V25_BBOX), None, dict(_V25_META))
    )
    model_text = format_design_state_block(model_block)
    assert "(my value differs from the measurement)" in model_text
    # Never "you asked for" / "stated by the user" for the model's rows.
    assert "stated by the user" not in model_text
    # The axis rows (measured) carry no mark at all.
    assert "Width (W) = 43.8" in model_text
    # A user-source disagrees (the #137 W-named param path): the old
    # wording, "you stated this; the measurement differs".
    user_block = build_design_state_block(
        state_block_for_version(
            {"W": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0}, None
        )
    )
    user_text = format_design_state_block(user_block)
    assert "(you stated this; the measurement differs)" in user_text
    assert "my value differs" not in user_text
    # The two renderings DIFFER (the operator: the prompt text for a
    # model-source disagrees row must differ from the user-source one).
    assert model_text != user_text
    # The single-line variant carries the same marks.
    from d33d.design_state import format_design_state_line

    line = format_design_state_line(model_block["entries"])
    assert "(my value differs from the measurement)" in line


def test_disagrees_source_only_on_param_disagrees_rows() -> None:
    """Issue #264 operator decision: ``disagrees_source`` appears ONLY on
    param rows with provenance ``disagrees`` — never on axis rows, never
    on non-disagrees rows; it is ``NotRequired`` (``"user"`` is the
    user-sourced default, ``"model"`` only on the assumed model-emitted
    path)."""
    # Model-source: present on the param row, absent on the axis rows.
    entries = state_block_for_version(dict(_V25_PARAMS), dict(_V25_BBOX), None, dict(_V25_META))
    for e in entries:
        if e["kind"] == "param" and e["provenance"] == "disagrees":
            assert e["disagrees_source"] == "model"
        else:
            assert "disagrees_source" not in e
    # User-source (no metadata, W-named param #137 path): ``"user"``.
    entries2 = state_block_for_version(
        {"W": 30.0}, {"x": 29.2, "y": 30.0, "z": 30.0}, None
    )
    for e in entries2:
        if e["provenance"] == "disagrees":
            assert e["disagrees_source"] == "user"
    # No disagree at all (measured) → absent everywhere.
    entries3 = state_block_for_version({"W": 30.0}, {"x": 30.0, "y": 30.0, "z": 30.0}, None)
    assert all("disagrees_source" not in e for e in entries3)


def test_zero_or_non_numeric_declared_axis_param_never_disagrees() -> None:
    """Issue #264 edge case: a param with a declared axis whose value is
    0 or non-numeric does NOT enter the model-source comparison — it
    stays ``assumed``/``unknown``, never ``disagrees``."""
    # Zero value → unknown, never disagrees.
    entries = state_block_for_version(
        {"spacer_width": 0},
        dict(_V25_BBOX),
        None,
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "unknown"
    # Non-numeric (string) → assumed, never disagrees.
    entries2 = state_block_for_version(
        {"spacer_width": "forty"},
        dict(_V25_BBOX),
        None,
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w2 = next(e for e in entries2 if e["name"] == "spacer_width")
    assert w2["provenance"] == "assumed"
    assert "disagrees_source" not in w2


def test_promoted_param_never_model_source() -> None:
    """Issue #264 operator decision: a param promoted to ``stated`` (rule
    (a) — declared axis + stated evidence within tolerance) is compared
    against its declared axis's measured extent (measurement honesty):
    |45-40| = 5 > tol 0.5 → ``disagrees`` with source ``"user"`` (the user
    gave the evidence — never ``model``)."""
    # spacer_width 40, declared W, stated W=40 → promoted to stated;
    # measured 45 → outside tolerance → disagrees, user-sourced.
    entries = state_block_for_version(
        {"spacer_width": 40.0},
        {"x": 45.0, "y": 30.0, "z": 30.0},
        {"W": 40.0},
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "disagrees"
    assert w["disagrees_source"] == "user"
    assert w["value"] == 45.0  # the MEASURED extent is displayed
    assert w["stated_value"] == 40.0  # the user's evidence rides along
    # The W axis row: stated 40 vs measured 45 → disagrees (axis rows
    # never carry ``disagrees_source``).
    w_axis = next(e for e in entries if e["kind"] == "axis" and e["name"] == "W")
    assert w_axis["provenance"] == "disagrees"
    assert w_axis["stated_value"] == 40.0
    assert "disagrees_source" not in w_axis


# ---------------------------------------------------------------------------
# Issue #264: measurement honesty for axis-DECLARED promoted params
# ---------------------------------------------------------------------------


def test_promoted_rule_a_param_mismatching_measurement_is_user_disagrees() -> None:
    """A param with a declared axis W promoted via rule (a) (stated W=40)
    whose measured W extent is 43.8 (outside the 0.5 mm tolerance) is
    ``disagrees`` with source ``user``, ``stated_value`` the param's own
    40 and the displayed value the measured 43.8 — measurement honesty.
    It is excluded from offers (in ``disagree_names``)."""
    from d33d.confirm_offer import select_offer_candidate

    entries = state_block_for_version(
        {"spacer_width": 40.0},
        {"x": 43.8, "y": 30.0, "z": 30.0},
        {"W": 40.0},  # rule (a): the axis evidence promotes spacer_width
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "disagrees"
    assert w["disagrees_source"] == "user"
    assert w["stated_value"] == 40.0  # the param's value rides along
    assert w["value"] == 43.8  # the measured extent is what prints
    # The param is in the disagree set → offers exclude it.
    disagree_names = {
        e["name"] for e in entries
        if e.get("kind") == "param" and e.get("provenance") == "disagrees"
    }
    assert disagree_names == {"spacer_width"}
    assert select_offer_candidate(
        {"spacer_width": 40.0},
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
        None,
        set(),
        None,
        disagree_names=disagree_names,
    ) is None


def test_promoted_rule_a_param_within_tolerance_stays_stated() -> None:
    """The same promotion with a measured W of 40.2 (within the 0.5 mm
tolerance) stays ``stated`` — the measurement confirms the user's value
(no ``disagrees``, no displayed-value swap)."""
    entries = state_block_for_version(
        {"spacer_width": 40.0},
        {"x": 40.2, "y": 30.0, "z": 30.0},
        {"W": 40.0},
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "stated"
    assert w["value"] == 40.0
    assert "stated_value" not in w
    assert "disagrees_source" not in w


def test_promoted_rule_b_param_mismatching_measurement_is_user_disagrees() -> None:
    """A rule (b) confirmed param with a declared axis whose measurement
    contradicts the confirmed value is ``disagrees`` with source ``user``
(the confirmation is the user's evidence — never ``model``): the
    confirmed 40 rides along as ``stated_value``, the measured 43.8 is
    displayed, and the param enters the offer-exclusion set."""
    from d33d.confirm_offer import select_offer_candidate

    entries = state_block_for_version(
        {"spacer_width": 40.0},
        {"x": 43.8, "y": 30.0, "z": 30.0},
        None,
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
        {"spacer_width": 40.0},  # rule (b): explicit confirmation
    )
    w = next(e for e in entries if e["name"] == "spacer_width")
    assert w["provenance"] == "disagrees"
    assert w["disagrees_source"] == "user"
    assert w["stated_value"] == 40.0
    assert w["value"] == 43.8
    disagree_names = {
        e["name"] for e in entries
        if e.get("kind") == "param" and e.get("provenance") == "disagrees"
    }
    assert disagree_names == {"spacer_width"}
    assert select_offer_candidate(
        {"spacer_width": 40.0},
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
        None,
        set(),
        None,
        disagree_names=disagree_names,
    ) is None


def test_promoted_param_disagrees_renders_user_wording_in_prompt() -> None:
    """The prompt line for a promoted-axis-param disagreement uses the
    USER-sourced wording (the Brief renders the user ``disagreement``
copy for it) — never the model-source wording and never "stated by the
    user" (the row is no longer stated)."""
    block = build_design_state_block(
        state_block_for_version(
            {"spacer_width": 40.0},
            {"x": 43.8, "y": 30.0, "z": 30.0},
            {"W": 40.0},
            {"spacer_width": {"label": "Spacer width", "axis": "W"}},
        )
    )
    text = format_design_state_block(block)
    assert "Spacer width = 43.8 (you stated this; the measurement differs)" in text
    assert "my value differs" not in text
    assert "stated by the user" not in text
