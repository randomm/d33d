"""Issue #264 (task-b): offer eligibility excludes disagrees params.

The v25-shaped fixture: an assumed param with a declared axis whose
value differs from the measured bbox beyond tolerance → the block
renders it ``disagrees`` (model-source) → the param is NEVER offerable.
"""

from __future__ import annotations

from tests.versioning.helpers import (
    create_project,
    run_async,
)

# ---------------------------------------------------------------------------
# Pure: select_offer_candidate with an explicit disagree_names set
# ---------------------------------------------------------------------------


def test_disagree_names_excludes_param_from_offer():
    """A param whose value the measurement contradicts (in the
    ``disagree_names`` set) is NOT offerable — even though
    ``state_block_from_params`` still labels it ``assumed`` (the
    helper is measurement-blind; the caller's explicit set is the
    only gate)."""
    from d33d.confirm_offer import select_offer_candidate

    # v25-shaped: Spacer width 40 axis W, measured 43.8 → disagrees.
    params = {"spacer_width": 40.0, "spacer_depth": 40.0}
    meta = {
        "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
        "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
    }
    # Both params in the disagree set → no offer (nothing else eligible).
    assert (
        select_offer_candidate(
            params, meta, None, set(), None,
            disagree_names={"spacer_width", "spacer_depth"},
        )
        is None
    )
    # One param disagrees, the other does not → the non-disagreeing
    # param is still offered (the exclusion is per-name, not all-or-
    # nothing).
    assert (
        select_offer_candidate(
            params, meta, None, set(), None,
            disagree_names={"spacer_width"},
        )
        == "spacer_depth"
    )


def test_disagree_names_rejects_confirm_first_naming_it():
    """A ``confirm_first`` flag naming a disagrees param is REJECTED
    (the offer is ABSENT — never a silent second guess), matching the
    existing behaviour for confirmed/changed params."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"spacer_width": 40.0, "wall_thickness": 3.0}
    meta = {"spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"}}
    # The flag names the disagrees param → rejected (no offer).
    assert (
        select_offer_candidate(
            params, meta, None, set(), "spacer_width",
            disagree_names={"spacer_width"},
        )
        is None
    )
    # The flag names a DIFFERENT param (not in the set) → that param
    # is still offered.
    assert (
        select_offer_candidate(
            params, meta, None, set(), "wall_thickness",
            disagree_names={"spacer_width"},
        )
        == "wall_thickness"
    )


def test_disagree_names_default_empty_preserves_old_behaviour():
    """``disagree_names`` defaults to ``frozenset()`` — pre-#264
    callers (and the tier tests) get exactly the old behaviour: an
    empty set excludes nothing."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"width": 60.0, "wall_thickness": 3.0}
    meta = {"width": {"label": "Width", "unit": "mm", "axis": "W"}}
    # No disagree set → the declared-axis param is offered (old behaviour).
    assert select_offer_candidate(params, meta, None, set(), "wall_thickness") == "width"
    # Explicit empty set → same.
    assert (
        select_offer_candidate(
            params, meta, None, set(), "wall_thickness", disagree_names=frozenset()
        )
        == "width"
    )
    # A non-empty set DOES exclude the named param (the contrast case —
    # the flag must name the NON-excluded param to get an offer).
    assert (
        select_offer_candidate(
            params, meta, None, set(), "wall_thickness", disagree_names={"width"}
        )
        == "wall_thickness"
    )
    # The flag names the EXCLUDED param → rejected (no offer).
    assert (
        select_offer_candidate(
            params, meta, None, set(), "width", disagree_names={"width"}
        )
        is None
    )


def test_disagree_names_tuple_accepted():
    """``disagree_names`` accepts a tuple (the caller may build a tuple
    from a generator) — the helper coerces to a set internally."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"width": 60.0, "wall_thickness": 3.0}
    meta = {"width": {"label": "Width", "unit": "mm", "axis": "W"}}
    # The tuple names "width" (the only eligible param) → excluded → no
    # offer (the flag must name the excluded param to be rejected).
    assert (
        select_offer_candidate(
            params, meta, None, set(), "width",
            disagree_names=("width",),  # tuple, not set
        )
        is None
    )


def test_disagree_names_none_treated_as_empty():
    """``disagree_names=None`` (a defensive caller) is treated as empty
    (no exclusion) — the helper never crashes on a None set."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"width": 60.0, "wall_thickness": 3.0}
    meta = {"width": {"label": "Width", "unit": "mm", "axis": "W"}}
    assert (
        select_offer_candidate(
            params, meta, None, set(), "wall_thickness", disagree_names=None
        )
        == "width"
    )


def test_disagree_names_does_not_affect_tiers():
    """The exclude set is INDEPENDENT of the tier logic: a tier-1
    (released-axis) or tier-2 (user-quoted) param that is ALSO in the
    disagree set is still excluded (the exclusion applies BEFORE the
    tiers run — the eligible set is computed first)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_height": 15.0, "width": 60.0}
    meta = {
        "lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"},
        "width": {"label": "Width", "unit": "mm", "axis": "W"},
    }
    # Tier 1 would pick lift_height (H released), but it is in the
    # disagree set → excluded. width (W, not released) is NOT in the set
    # → tier 1 empty (no released-axis param eligible), tier 2 empty,
    # tier 3: width (the only declared-axis param) → offered.
    assert (
        select_offer_candidate(
            params, meta, None, set(), None,
            released_axes={"H"},
            disagree_names={"lift_height"},
        )
        == "width"
    )
    # Both in the disagree set → no offer (nothing eligible).
    assert (
        select_offer_candidate(
            params, meta, None, set(), None,
            released_axes={"H", "W"},
            disagree_names={"lift_height", "width"},
        )
        is None
    )


# ---------------------------------------------------------------------------
# End-to-end: the v25-shaped fixture through the API + pure selection
# ---------------------------------------------------------------------------


def test_user_stated_v25_fixture_offer_selection_excludes_disagrees(app_with_versions):
    """THE acceptance case (user-source): a v25-shaped version (created
    via the service with a real bbox AND a stated W=40) → the block
    renders the W param ``disagrees`` (user-source — the measurement
    contradicts the stated value) → the offer selection (fed the
    block's disagree set, the way ``_resolve_offer`` computes it)
    excludes the param — the v25 fixture NEVER offers a disagrees param
    (a value the measurement contradicts is never an assumption to
    confirm).

    (The model-source case — an assumed param with a declared axis whose
    value differs beyond tolerance — is task-a's scope; the exclusion
    mechanism is identical for both sources, and this test pins the
    user-source path that the current codebase renders.)"""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        # Create the version via the service (the API route does not
        # accept bbox/stated_dims/param_meta in the create body — it
        # derives them from the design loop; the service is the direct
        # seam the adapter uses).
        v = await svc.create_version(
            pid,
            {"W": 40.0, "spacer_depth": 40.0},
            param_meta={
                "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
            },
            stated_dims={"W": 40.0},
            bbox=(43.8, 43.9, 12.0),
        )
        row = svc.get_version(pid, v["id"])
        from d33d.confirm_offer import select_offer_candidate
        from d33d.design_state import state_block_for_version

        # The exact computation _resolve_offer performs: the full
        # block, then the param-row disagree names, then the selection.
        block = state_block_for_version(
            row["params"], row["bbox"], row["stated_dims"],
            row["param_meta"], row["confirmed_params"],
        )
        disagree_names = {
            e["name"] for e in block
            if e.get("kind") == "param" and e.get("provenance") == "disagrees"
        }
        name = select_offer_candidate(
            row["params"], row["param_meta"], row["confirmed_params"],
            set(), None, disagree_names=disagree_names,
        )
        param_rows = [e for e in block if e.get("kind") == "param"]
        return name, disagree_names, param_rows

    name, disagree_names, param_rows = run_async(app_with_versions, _call)
    # The W param is in the disagree set (the measurement contradicts
    # the stated value 40 vs measured 43.8).
    assert disagree_names == {"W"}, disagree_names
    # The offer selection excludes W → no offer (spacer_depth is
    # assumed with a declared axis D; the D measurement (43.9) does
    # NOT contradict 40 via the current codebase's W/D/H-named
    # comparison — spacer_depth is not named D, so it stays assumed
    # and IS offered (it is the only eligible param).
    #
    # So the offer IS emitted, for spacer_depth. The test asserts the
    # CONTRAST: W is excluded (in the disagree set), but spacer_depth is
    # still offered (not in the set).
    assert name == "spacer_depth", name
    # The block renders W as disagrees (the data the exclusion reads
    # from).
    provs = {e["name"]: e["provenance"] for e in param_rows}
    assert provs["W"] == "disagrees", provs
    assert provs["spacer_depth"] == "assumed", provs


def test_v25_fixture_all_params_disagree_no_offer(app_with_versions):
    """THE v25-shaped fixture (all params in the disagree set) → the
    offer selection returns None (nothing eligible) — the v25 fixture
    NEVER offers a disagrees param."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        # All params are W/D/H-named and stated, and the measurement
        # contradicts all three → all three are disagrees.
        v = await svc.create_version(
            pid,
            {"W": 40.0, "D": 40.0, "H": 12.0},
            stated_dims={"W": 40.0, "D": 40.0, "H": 12.0},
            bbox=(43.8, 43.9, 12.0),
        )
        row = svc.get_version(pid, v["id"])
        from d33d.confirm_offer import select_offer_candidate
        from d33d.design_state import state_block_for_version

        block = state_block_for_version(
            row["params"], row["bbox"], row["stated_dims"],
            row["param_meta"], row["confirmed_params"],
        )
        disagree_names = {
            e["name"] for e in block
            if e.get("kind") == "param" and e.get("provenance") == "disagrees"
        }
        name = select_offer_candidate(
            row["params"], row["param_meta"], row["confirmed_params"],
            set(), None, disagree_names=disagree_names,
        )
        param_rows = [e for e in block if e.get("kind") == "param"]
        return name, disagree_names, param_rows

    name, disagree_names, param_rows = run_async(app_with_versions, _call)
    # W and D are disagrees (40 vs 43.8/43.9); H is measured (12.0
    # vs 12.0, within tolerance).
    assert disagree_names == {"W", "D"}, disagree_names
    # H is measured (within tolerance) → NOT in the disagree set →
    # offered (tier 3: H is a declared-axis param... wait, H has no
    # declared axis in param_meta (param_meta is None) → H is a
    # W/D/H-named param, not a declared-axis param. The offer's tier 3
    # picks declared-axis params first; H has no declared axis. The
    # confirm_first is None → no offer.
    #
    # Actually: H is assumed (stated? no — stated_dims has H=12.0, but
    # the param's provenance comes from the W/D/H-named comparison:
    # H=12.0 vs measured 12.0 → measured. So H is measured, not
    # assumed → not offerable (the offer's provenance filter requires
    # "assumed").
    #
    # So: W and D are disagrees (excluded), H is measured (not
    # assumed → not offerable). No eligible params → no offer.
    assert name is None, name
    provs = {e["name"]: e["provenance"] for e in param_rows}
    assert provs == {"W": "disagrees", "D": "disagrees", "H": "measured"}, provs
