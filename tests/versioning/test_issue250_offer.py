"""Issue #250 (task-a): the assumed-value offer — selection precedence,
the confirm_first/confirm_sentence reply contract, the number-guarded
sentence, the acceptance flow, and the persistence of the offer state.

All fast (stub loops, no LLM, no Docker).
"""

from __future__ import annotations

from typing import Any

from d33d.confirm_offer import offer_entry
from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)


def _reopen_conn(app):
    """Reopen the app's DB connection if it was closed by a previous
    test's lifespan teardown (the same pattern as the carried-forward
    test above)."""
    closed = False
    try:
        app.state.conn.raw.execute("SELECT 1")
    except Exception:
        closed = True
    if app.state.conn is None or closed:
        import d33d.db as _db
        from d33d import versions as _versions_mod

        fresh = _db.connect(app.state.db_path)
        _versions_mod.migrate(fresh)
        app.state.conn = fresh
        app.state.versions = _versions_mod.VersionService(fresh)



# ---------------------------------------------------------------------------
# Offer selection precedence (pure — d33d.confirm_offer)
# ---------------------------------------------------------------------------


def _meta_with_axis(name: str) -> dict[str, Any]:
    return {name: {"label": "Width", "unit": "mm", "axis": "W"}}


def test_selection_declared_axis_param_wins_over_confirm_first():
    """Rule 1: a pass with an assumed param carrying a model-declared
    axis (issue #248) → the offer names that axis param FIRST, even when
    the model's ``confirm_first`` flags a different param."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"width": 60.0, "wall_thickness": 3.0}
    meta = _meta_with_axis("width")
    name = select_offer_candidate(params, meta, None, set(), "wall_thickness")
    assert name == "width"  # the declared-axis param, not confirm_first


def test_selection_confirm_first_when_no_axis_params():
    """Rule 2: no axis params, but ``confirm_first`` names a valid assumed
    numeric param of this version → that param is the offer."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "fillet_radius": 1.5}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    name = select_offer_candidate(params, meta, None, set(), "wall_thickness")
    assert name == "wall_thickness"


def test_selection_invalid_confirm_first_yields_no_offer():
    """Rule 2 validation: a ``confirm_first`` that names a nonexistent,
    non-numeric, zero, or non-assumed param is rejected — the offer is
    ABSENT (never a silent second guess)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "note": "left", "zero": 0.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    for bad in ("ghost", "note", "zero", None, ""):
        assert select_offer_candidate(params, meta, None, set(), bad) is None


def test_selection_never_re_offers_confirmed_param():
    """A param already in ``confirmed_params`` (the user confirmed it on
    this version) is never offered again: a flag naming it is rejected
    (no silent fallback), while a flag naming another eligible param
    still works."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "fillet_radius": 1.5}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    confirmed = {"wall_thickness": 3.0}
    assert (
        select_offer_candidate(params, meta, confirmed, set(), "wall_thickness")
        is None
    )
    assert (
        select_offer_candidate(params, meta, confirmed, set(), "fillet_radius")
        == "fillet_radius"
    )


def test_selection_never_re_offers_user_changed_param():
    """A param the user changed via the design loop (it is in the
    changed set — a value move or a new param) is never offered: those
    values are the user's own moves, not assumptions to confirm."""
    from d33d.confirm_offer import select_offer_candidate

    assert (
        select_offer_candidate(
            {"wall_thickness": 2.0, "fillet": 1.0},
            None,
            None,
            {"wall_thickness"},
            "wall_thickness",
        )
        is None
    )
    # A param the user did NOT change is still offered when flagged:
    assert (
        select_offer_candidate(
            {"wall_thickness": 2.0, "fillet": 1.0},
            None,
            None,
            {"wall_thickness"},
            "fillet",
        )
        == "fillet"
    )


def test_selection_no_eligible_param_yields_no_offer():
    """String-only / zero / bool params: no numeric assumed candidate →
    no offer (never a fake one), regardless of the flag."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"note": "left", "flag": True, "zero": 0.0}
    assert select_offer_candidate(params, None, None, set(), "note") is None
    assert select_offer_candidate(params, None, None, set(), None) is None


# ---------------------------------------------------------------------------
# The sentence: model text is number-guarded, else the template
# ---------------------------------------------------------------------------


def test_sentence_model_text_accepted_when_guard_passes():
    """The model's ``confirm_sentence`` is used verbatim when it names
    the chosen value AND every number in it appears in the block."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    sentence = offer_sentence(
        entry,
        "I assumed 3 mm Wall thickness. That is sturdy for a shelf spacer. Want it thinner?",
        [entry],
    )
    assert sentence == (
        "I assumed 3 mm Wall thickness. That is sturdy for a shelf spacer. Want it thinner?"
    )


def test_sentence_invented_number_falls_to_template():
    """A model sentence with a number that is NOT in the block (an
    invented value — "I assumed 4 mm" when the param is 3) fails the
    guard → the deterministic template is used."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    sentence = offer_sentence(entry, "I assumed 4 mm walls. Want it thinner?", [entry])
    assert sentence == "I assumed 3.0\u202fmm for Wall thickness. Want it different?"


def test_sentence_wrong_value_falls_to_template():
    """A sentence that names the RIGHT param but the WRONG value fails
    the value-name check (even if the number exists in the block under
    another param) → the template."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0, "spacer_width": 20.0}
    meta = {
        "wall_thickness": {"label": "Wall thickness", "unit": "mm"},
        "spacer_width": {"label": "Spacer width", "unit": "mm"},
    }
    entry = offer_entry(params, meta, "wall_thickness")
    block = [
        offer_entry(params, meta, "wall_thickness"),
        offer_entry(params, meta, "spacer_width"),
    ]
    sentence = offer_sentence(entry, "I assumed 20 mm walls. Want it thinner?", block)
    assert sentence == "I assumed 3.0\u202fmm for Wall thickness. Want it different?"


def test_sentence_none_falls_to_template_with_identifier_fallback():
    """No model sentence → the deterministic template (label per #248,
    raw-identifier fallback when no label — the mono case)."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    entry = offer_entry(params, None, "wall_thickness")  # no metadata
    sentence = offer_sentence(entry, None, [entry])
    assert sentence == "I assumed 3 for wall_thickness. Want it different?"


def test_ack_sentence_shape():
    """The acknowledgement is the deterministic template:
    "Got it — {label} stays {value}." (label per #248, identifier
    fallback)."""
    from d33d.confirm_offer import ack_sentence, offer_entry

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    assert ack_sentence(entry) == "Got it — Wall thickness stays 3.0\u202fmm."
    entry2 = offer_entry({"bore": 8.0}, None, "bore")
    assert ack_sentence(entry2) == "Got it — bore stays 8."


# ---------------------------------------------------------------------------
# Acceptance: clean affirmation + live offer (heuristic reuse)
# ---------------------------------------------------------------------------


def test_acceptance_matrix_through_heuristic():
    """The composite acceptance predicate: a clean affirmation (the
    issue #249 ``_is_clean_affirmation`` heuristic, reused) AND a live
    pending offer on the current latest version. "yes but make it 2 mm"
    is NOT an acceptance (a change request routes to the design loop); a
    stale offer (a newer version exists) lapses."""
    from d33d.confirm_offer import is_pending_offer_acceptance

    offer = {"version_id": 7, "param": "wall_thickness"}
    latest = {"id": 7}
    for message in ("yes", "Yes.", "ok", "confirmed", "yep"):
        assert is_pending_offer_acceptance(message, offer, latest), message
    for message in (
        "yes but make it 2 mm",
        "is it 3 mm?",
        "no",
        "nope",
        "why 3 mm?",
    ):
        assert not is_pending_offer_acceptance(message, offer, latest), message
    # Stale offer (a newer version was created meanwhile) → lapses:
    assert not is_pending_offer_acceptance("yes", offer, {"id": 8})
    # No pending offer → normal routing:
    assert not is_pending_offer_acceptance("yes", None, latest)


# ---------------------------------------------------------------------------
# Through the /chat route: the done frame's offer field, the server-side
# pending-offer state, the acceptance flow, and the carry-forward
# ---------------------------------------------------------------------------


class _OfferStubResult:
    """A pass result whose ``best`` is a real ``IterationRecord``
    carrying the declared ``confirm_first`` / ``confirm_sentence``
    fields (the adapter reads them as declared fields, never
    duck-typed)."""

    def __init__(
        self,
        params: dict[str, Any],
        meta: dict[str, Any] | None = None,
        confirm_first: str | None = None,
        confirm_sentence: str | None = None,
    ) -> None:
        from d33d.design_loop import IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "pass"
        self.failure_reason = None
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 10; cube([W, W, W]);",
            render=_default_render(),
            score=Score(bits=(True, True, True, True, True), rank=5, tiebreak=(True,)*5),
            params=dict(params),
            param_meta=dict(meta) if meta else {},
            confirm_first=confirm_first,
            confirm_sentence=confirm_sentence,
        )


class _V25PassStub:
    """A v25-shaped pass result (issue #264): params 40/40 with declared
    axis W/D on a render that MEASURES 43.80 x 43.90 x 12.0 (the flared
    lip adds 3.8 mm) — the best candidate's declared ``bbox`` is what the
    version write path persists, and it is the precondition the offer
    gate keys on (both params render model-source disagrees in the new
    version's own design-state block)."""

    def __init__(self) -> None:
        from d33d.design_loop import BboxInfo, IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "pass"
        self.failure_reason = None
        self.best = IterationRecord(
            iteration=0,
            scad_source="x = 40; cube([x, x, 12]);",
            render=_default_render(),
            score=Score(bits=(True, True, True, True, True), rank=5, tiebreak=(True,) * 5),
            bbox=BboxInfo(x=43.80, y=43.90, z=12.0, volume=2297.0),
            params={"spacer_width": 40.0, "spacer_depth": 40.0},
            param_meta={
                "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
                "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
            },
        )


class _ExhaustedStub:
    """An exhausted result (no version, no offer — the pending offer is
    cleared on the exhausted path)."""

    def __init__(self, params: dict[str, Any]) -> None:
        from d33d.design_loop import IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "exhausted"
        self.failure_reason = "bbox_out_of_tolerance"
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 10; cube([W, W, W]);",
            render=_default_render(),
            score=Score(bits=(False,)*5, rank=0, tiebreak=(False,)*5),
            params=dict(params),
        )


# ---------------------------------------------------------------------------
# Issue #261 (task-d): offer priority tiers 1/2 — the released-axis and
# user-quoted-unmapped-number offers, with mm()-formatted tier sentences
# ---------------------------------------------------------------------------

def test_tier1_released_axis_param_offered_first():
    """Tier 1: an assumed numeric param with a declared axis ON AN AXIS
    RELEASED by this turn's relative cue is offered BEFORE a different
    declared-axis param (which would win under tier 3) and before
    ``confirm_first``."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_height": 15.0, "width": 60.0}
    meta = {
        "lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"},
        "width": {"label": "Width", "unit": "mm", "axis": "W"},
    }
    # "make it taller" releases H (task-b's lexicon feed supplies this at
    # the adapter seam — here the released set is passed directly).
    name = select_offer_candidate(
        params, meta, None, set(), "width", released_axes={"H"}
    )
    assert name == "lift_height"  # tier 1 beats tier 3's declaration order


def test_tier1_global_release_covers_all_axes():
    """Tier 1: a GLOBAL cue releases all three axes — an assumed
    param declared on ANY axis is eligible (declaration order)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_height": 15.0, "width": 60.0}
    meta = {
        "lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"},
        "width": {"label": "Width", "unit": "mm", "axis": "W"},
    }
    # "make it bigger" → all three axes released.
    name = select_offer_candidate(
        params, meta, None, set(), None, released_axes={"W", "D", "H"}
    )
    assert name == "lift_height"  # first declared-axis param in order


def test_tier1_empty_falls_through_to_tier3():
    """Tier 1 EMPTY (the released axis has no assumed axis-declared
    param) falls through to tier 3 (declared-axis declaration order)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_gap": 4.0, "width": 60.0}
    meta = {"width": {"label": "Width", "unit": "mm", "axis": "W"}}
    # H released, but only lift_gap (no axis) is assumed → tier 1 empty.
    name = select_offer_candidate(
        params, meta, None, set(), None, released_axes={"H"}
    )
    assert name == "width"  # tier 3: the declared-axis param


def test_tier2_user_quoted_unmapped_number_offered():
    """Tier 2: an assumed numeric param whose value equals (±1e-6) a
    user-quoted UNMAPPED mm number is offered ("a spacer to lift a shelf
    12 mm" → the 12-valued param). Beats tier 3's declaration order and
    ``confirm_first`` when the quoted param is not a declared-axis param."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_gap": 12.0, "wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    # "a spacer to lift a shelf 12 mm" → 12 is an unmapped mm number
    # (lift is excluded from the lexicon; nothing else in the message).
    name = select_offer_candidate(
        params,
        meta,
        None,
        set(),
        "wall_thickness",
        user_quoted_mm={12.0},
    )
    assert name == "lift_gap"


def test_tier2_empty_falls_through_to_tier3():
    """Tier 2 EMPTY (no user-quoted unmapped number matches an eligible
    param's value) falls through to tier 3 (the #250 order — declared-
    axis declaration order, else validated ``confirm_first``)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_gap": 12.0, "wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    # 99.0 is quoted but no eligible param carries it → tier 2 empty.
    assert (
        select_offer_candidate(
            params, meta, None, set(), "wall_thickness", user_quoted_mm={99.0}
        )
        == "wall_thickness"
    )
    # No quoted numbers at all → tier 3 unchanged.
    assert (
        select_offer_candidate(params, meta, None, set(), "wall_thickness")
        == "wall_thickness"
    )


def test_tier1_beats_tier2():
    """Tier precedence: tier 1 (a released-axis param) wins when BOTH
    tiers have a candidate — the user's own relative words are the
    stronger signal than a quoted number."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"lift_height": 15.0, "lift_gap": 12.0}
    meta = {
        "lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"},
    }
    # H released by a relative cue AND the user quoted an unmapped 12
    # (tier 1 has lift_height, tier 2 has lift_gap).
    name = select_offer_candidate(
        params,
        meta,
        None,
        set(),
        None,
        released_axes={"H"},
        user_quoted_mm={12.0},
    )
    assert name == "lift_height"  # tier 1 first


def test_tier_sentences_mm_formatted_exactly_as_mm():
    """The tier-1/tier-2 sentences' ``{value}`` is ALWAYS the mm()-
    formatted string when the param's METADATA unit is mm (the
    ``meta_unit`` key — the model-declared unit the ``offer_entry``
    graft adds; the entry's default ``unit: "mm"`` never counts) — one
    decimal, the U+202F narrow no-break space, ``mm`` — never the raw
    number, never ``:g`` (``12`` would diverge from the deck's ``12.0``).
    The template shapes match copy.ts's ``tier1Offer``/``tier2Offer``
    slot-for-slot (the design-contract pin, the #250 way)."""
    from d33d.confirm_offer import (
        mm_formatted,
        tier_1_sentence,
        tier_1_sentence_template,
        tier_2_sentence,
        tier_2_sentence_template,
    )

    # mm()-formatted: 12 → "12.0\u202fmm" (the exact deck string).
    assert mm_formatted(12) == "12.0\u202fmm"
    entry = {
        "name": "lift_height",
        "label": "Lift height",
        "value": 12.0,
        "unit": "mm",
        "meta_unit": "mm",
    }
    assert tier_2_sentence(entry) == "You said 12.0\u202fmm — I used it for Lift height. Right?"
    assert tier_1_sentence(entry, "taller") == "You asked for taller — I made Lift height 12.0\u202fmm. Right?"
    # The templates (deck mirror) render the same sentences when the slots
    # are filled with the same strings the backend uses.
    assert (
        tier_1_sentence_template()
        .replace("{cue}", "taller")
        .replace("{label}", "Lift height")
        .replace("{value}", mm_formatted(12))
        == tier_1_sentence(entry, "taller")
    )
    assert (
        tier_2_sentence_template()
        .replace("{value}", mm_formatted(12))
        .replace("{label}", "Lift height")
        == tier_2_sentence(entry)
    )


def test_tier_sentences_non_numeric_value_falls_back_to_format_value():
    """A non-numeric value in an mm-labelled entry never reaches
    ``mm_formatted`` (it needs a number): the tier sentences fall back
    to the plain value formatter (verbatim, no fabricated ``mm``)."""
    from d33d.confirm_offer import tier_1_sentence, tier_2_sentence

    entry = {
        "name": "finish",
        "label": "Finish",
        "value": "matte",
        "unit": "mm",
        "meta_unit": "mm",
    }
    assert tier_2_sentence(entry) == "You said matte — I used it for Finish. Right?"
    assert tier_1_sentence(entry, "taller") == "You asked for taller — I made Finish matte. Right?"


def test_tier1_cue_from_lexicon():
    """Tier 1's ``{cue}`` is the FIRST entry of classify(message).cue_words
    that belongs to the lexicon's relative or global word sets —
    verbatim, lowercased — no second scan of the raw message."""
    from d33d.confirm_offer import tier_1_cue

    assert tier_1_cue("make it taller") == "taller"
    assert tier_1_cue("make it wider, please") == "wider"
    assert tier_1_cue("make it half the size") == "half the size"
    # No relative/global cue → no tier-1 offer.
    assert tier_1_cue("add a hole") is None
    # An absolute-only cue is not a tier-1 cue ("how tall is it?" stays a
    # question; "12 mm tall" sets H, it does not release it).
    assert tier_1_cue("make it 12 mm tall") is None


def test_tier2_user_quoted_unmapped_mm_helper():
    """The tier-2 helper (task-b's ``user_quoted_unmapped_mm``) scans ALL
    user messages: only explicit-mm numbers count; a number the lexicon
    or an explicit protocol cue mapped to an axis is NOT eligible."""
    from d33d.dimension_protocol import user_quoted_unmapped_mm

    # "a 20 mm wide thing, lift it 12 mm" → 20 is mapped (W), 12 is not.
    assert user_quoted_unmapped_mm(["a 20 mm wide thing, lift it 12 mm"]) == {12.0}
    # Both mapped → nothing eligible.
    assert user_quoted_unmapped_mm(["12 mm tall, 20 mm wide"]) == set()
    # A bare number never counts (no explicit mm unit).
    assert user_quoted_unmapped_mm(["spacer_height 12"]) == set()
    # Cross-message union: a quoted number in ANY message is eligible.
    assert (
        user_quoted_unmapped_mm(["make it 12 mm tall", "and lift it 12 mm"])
        == {12.0}
    )
    # An explicit protocol cue in the message consumes the number.
    assert user_quoted_unmapped_mm(["H: 12 mm"]) == set()


# ---------------------------------------------------------------------------
# Issue #265: the tier-3 offer and ack carry the mm unit (reusing
# ``mm_formatted``), unitless / non-mm params keep the bare form, and the
# model-sentence value-name check accepts BOTH spellings.
# ---------------------------------------------------------------------------


def test_tier3_offer_and_ack_mm_formatted_for_mm_param():
    """A tier-3 offer for a param whose ``param_meta`` carries unit "mm"
    spells the value EXACTLY as ``mm()`` renders it ("40.0\u202fmm"),
    and the ack matches — the acceptance criterion's two sentences."""
    from d33d.confirm_offer import ack_sentence, mm_value_str, offer_sentence

    params = {"spacer_depth": 40.0}
    meta = {"spacer_depth": {"label": "Spacer depth", "unit": "mm"}}
    entry = offer_entry(params, meta, "spacer_depth")
    assert mm_value_str(entry) == "40.0\u202fmm"
    assert offer_sentence(entry, None, [entry]) == (
        "I assumed 40.0\u202fmm for Spacer depth. Want it different?"
    )
    assert ack_sentence(entry) == "Got it — Spacer depth stays 40.0\u202fmm."


def test_tier3_offer_and_ack_bare_when_unitless_or_non_mm():
    """A unitless param (no param_meta) or a non-mm unit ("count") keeps
    the bare ``:g`` spelling in BOTH sentences — no unit is fabricated
    (the binding operator decision: the entry's default ``unit: "mm"``
    does NOT count)."""
    from d33d.confirm_offer import ack_sentence, mm_value_str, offer_sentence

    # No metadata at all (the identifier fallback, unitless) → bare.
    entry = offer_entry({"hole_count": 3.0}, None, "hole_count")
    assert mm_value_str(entry) == "3"
    assert offer_sentence(entry, None, [entry]) == (
        "I assumed 3 for hole_count. Want it different?"
    )
    assert ack_sentence(entry) == "Got it — hole_count stays 3."
    # A param_meta unit that is not "mm" → bare.
    entry2 = offer_entry(
        {"hole_count": 3.0},
        {"hole_count": {"label": "Hole count", "unit": "count"}},
        "hole_count",
    )
    assert mm_value_str(entry2) == "3"
    assert offer_sentence(entry2, None, [entry2]) == (
        "I assumed 3 for Hole count. Want it different?"
    )
    assert ack_sentence(entry2) == "Got it — Hole count stays 3."


def test_tier3_offer_declared_axis_param_is_mm_even_without_unit_meta():
    """The binding operator decision: a declared axis ALSO counts as mm
    (a param_meta with axis "W" and no unit is still mm-formatted)."""
    from d33d.confirm_offer import mm_value_str, offer_sentence

    entry = offer_entry(
        {"spacer_width": 40.0},
        {"spacer_width": {"label": "Spacer width", "axis": "W"}},
        "spacer_width",
    )
    assert mm_value_str(entry) == "40.0\u202fmm"
    assert offer_sentence(entry, None, [entry]) == (
        "I assumed 40.0\u202fmm for Spacer width. Want it different?"
    )


def test_tier3_offer_and_ack_use_the_same_mm_spelling(app_with_versions):
    """The documented #265 invariant, end-to-end: the tier-3 OFFER (the
    adapter's ``_resolve_offer`` — the offer path) and the ack (the
    ``/chat`` acceptance route — the ack path) render the SAME mm spelling
    for the same param. The binding seam (issue #265): the entry the
    offer and ack formatters see carries the version's ``param_meta``
    — resolved ONCE onto the entry by ``offer_entry`` (the adapter's
    ``_resolve_offer`` and the ``/chat`` acceptance route both build it
    via ``offer_entry`` from the same version row) — so ``mm_value_str``
    sees the declared unit and both sentences render the SAME mm
    spelling. A path that dropped the metadata would render "I assumed
    40 for Spacer depth" while the ack rendered "… stays 40.0 mm."."""

    async def _body(client):
        svc = app_with_versions.state.versions
        proj = await create_project(client)
        pid = proj["id"]
        v = await svc.create_version(
            pid,
            {"spacer_depth": 40.0},
            param_meta={"spacer_depth": {"label": "Spacer depth", "unit": "mm"}},
        )
        # The offer path (the adapter's ``_resolve_offer``) and the ack
        # path (the /chat acceptance route) both build their entry via
        # ``offer_entry`` from the SAME version row. Assert the documented
        # #265 invariant directly on both paths: both sentences must use
        # the SAME mm spelling (before the fix the offer path's entry
        # lacked the param_meta graft → the offer said "I assumed 40 for
        # …" while the ack said "… stays 40.0 mm.").
        from d33d.confirm_offer import ack_sentence, offer_entry, offer_sentence
        from d33d.design_state import state_block_for_version

        latest = svc.latest_version(pid)
        # The offer path (the adapter's ``_resolve_offer``): entry via
        # ``offer_entry`` — the version's ``param_meta`` resolved onto it.
        offer_entry_ = offer_entry(dict(latest["params"]), latest["param_meta"], "spacer_depth")
        block = state_block_for_version(
            latest["params"], latest["bbox"], latest["stated_dims"],
            latest["param_meta"], latest["confirmed_params"],
        )
        offer_str = offer_sentence(offer_entry_, None, block)
        # The ack path: the same ``offer_entry`` call, as projects.py does.
        ack_entry = offer_entry(dict(latest["params"]), latest["param_meta"], "spacer_depth")
        ack_str = ack_sentence(ack_entry)
        return offer_str, ack_str

    offer_str, ack_str = run_async(app_with_versions, _body)
    # The offer path's sentence is the mm spelling — NOT the bare :g
    # (the pre-fix divergence: offer "40", ack "40.0 mm").
    assert offer_str == "I assumed 40.0\u202fmm for Spacer depth. Want it different?", offer_str
    # The ack agrees with the offer: the same value spelling, byte-for-byte.
    assert ack_str == "Got it — Spacer depth stays 40.0\u202fmm.", ack_str
    assert "40.0\u202fmm" in offer_str
    assert "40.0\u202fmm" in ack_str


def test_tier3_model_sentence_bare_mm_and_decimal_spellings_accepted():
    """The model ``confirm_sentence`` is accepted when it names the value
    in EITHER the bare ("40") or the mm-formatted ("40.0 mm" — U+202F or
    regular space) spelling. The test matrix: bare, mm (U+202F), mm
    (regular space), decimal bare ("40.0")."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"spacer_depth": 40.0}
    meta = {"spacer_depth": {"label": "Spacer depth", "unit": "mm"}}
    entry = offer_entry(params, meta, "spacer_depth")
    for sentence in (
        "I assumed 40 for Spacer depth. Want it different?",
        "I assumed 40.0\u202fmm for Spacer depth. Want it different?",
        "I assumed 40.0 mm for Spacer depth. Want it different?",
        "I assumed 40.0 for Spacer depth. Want it different?",
    ):
        assert offer_sentence(entry, sentence, [entry]) == sentence, sentence


def test_tier3_model_sentence_wrong_value_still_falls_to_template():
    """A model sentence that names the value in the mm spelling but a
    DIFFERENT number than the param ("41.0 mm" when the param is 40)
    fails the value-name check → the template with the mm spelling."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"spacer_depth": 40.0}
    meta = {"spacer_depth": {"label": "Spacer depth", "unit": "mm"}}
    entry = offer_entry(params, meta, "spacer_depth")
    sentence = offer_sentence(
        entry, "I assumed 41.0 mm for Spacer depth. Want it different?", [entry]
    )
    assert sentence == "I assumed 40.0\u202fmm for Spacer depth. Want it different?"


def test_tier3_model_sentence_cross_param_collision_falls_to_template():
    """A model sentence that names a value that equals ANOTHER param in the
    same block (the cross-param collision the adversarial review flagged on
    issue #265) fails the value-name check and falls to the template.

    Before the fix, the value-name check used a bare substring ``in``:
    ``"4" in "40"`` is True, so a model sentence "I assumed 4 for Spacer
    depth" (a wrong value that happens to equal ``hole_count 4``) would
    be accepted verbatim.  The token-exact check (``"4" in ["40"]`` →
    False) closes the gap: the sentence falls to the mm-formatted
    template."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"spacer_depth": 40.0, "hole_count": 4.0}
    meta = {
        "spacer_depth": {"label": "Spacer depth", "unit": "mm"},
        "hole_count": {"label": "Hole count", "unit": "count"},
    }
    entry = offer_entry(params, meta, "spacer_depth")
    block = [entry, offer_entry(params, meta, "hole_count")]
    # The wrong value 4 IS a valid number in the block (hole_count=4),
    # so guard_answer_numbers passes; the value-name check must still
    # reject it because 4 is not the spelling of 40 in any form.
    sentence = offer_sentence(entry, "I assumed 4 for Spacer depth. Want it different?", block)
    assert sentence == "I assumed 40.0\u202fmm for Spacer depth. Want it different?"
    # The correct value, spelled bare, is still accepted verbatim.
    assert (
        offer_sentence(
            entry, "I assumed 40 for Spacer depth. Want it different?", block
        )
        == "I assumed 40 for Spacer depth. Want it different?"
    )


def test_tier3_model_sentence_decimal_truncation_accepted():
    """A model sentence that truncates the value's ``:g`` spelling to fewer
    decimal digits (issue #265's adversarial finding on the mm-prefix gap)
    is accepted by the VALUE-NAME CHECK — a value whose ``:g`` rendering
    needs more than one decimal digit (e.g. ``1.23456``) would otherwise
    demote every plausible model rounding ("1.23", "1.2") to the template.

    The token-exact check accepts a token that is a proper prefix of the
    bare ``:g`` spelling (the integer part identical, the decimal fraction
    truncated) — the natural model rounding. A token that rounds UP ("1.3"
    for ``1.23456``) or changes the integer part ("12" for ``1.23456``)
    still falls to the template.

    Note: the full ``offer_sentence`` also runs the number guard
    (``guard_answer_numbers``), which requires every number in the sentence
    to appear in the block within 1e-6. A truncated rounding ("1.23" for
    ``1.23456``) does NOT pass the guard (it's not in the block), so the
    full ``offer_sentence`` still falls to the template for that sentence.
    This test verifies the VALUE-NAME CHECK directly (via
    ``_sentence_names_value``) to isolate the fix from the guard's stricter
    presence-only rule."""
    from d33d.confirm_offer import _sentence_names_value

    v = 1.23456
    # The natural 2-decimal rounding ("1.23" truncates "1.23456") is
    # accepted by the value-name check — the pre-fix behaviour rejected it
    # (neither the bare "1.23456" nor the mm prefix "1.2" matched "1.23").
    assert _sentence_names_value(v, "I assumed 1.23 for W.") is True
    # The 1-decimal rounding ("1.2" — the mm prefix) is still accepted.
    assert _sentence_names_value(v, "I assumed 1.2 for W.") is True
    # The exact ``:g`` spelling is still accepted.
    assert _sentence_names_value(v, "I assumed 1.23456 for W.") is True
    # A token that rounds UP ("1.3" is not a truncation of "1.23456")
    # is rejected by the value-name check.
    assert _sentence_names_value(v, "I assumed 1.3 for W.") is False
    # A token that changes the integer part ("12" for "1.23456") is
    # rejected by the value-name check.
    assert _sentence_names_value(v, "I assumed 12 for W.") is False
    # The cross-param collision case still holds: "4" does not name 40.
    assert _sentence_names_value(40.0, "I assumed 4 for W.") is False
    assert _sentence_names_value(40.0, "I assumed 40 for W.") is True


def test_tier3_ack_mm_value_rides_done_frame(app_with_versions):
    """Through the /chat acceptance route: the done frame's
    ``confirm_ack_value`` and ``message`` are both mm-formatted for a
    unit-"mm" param (issue #265 — the value the SPA renders in the mono
    face is the same string as the ack sentence's)."""

    async def _call(client):
        svc = app_with_versions.state.versions
        proj = await create_project(client)
        pid = proj["id"]
        v = await svc.create_version(
            pid,
            {"spacer_depth": 40.0},
            param_meta={"spacer_depth": {"label": "Spacer depth", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "spacer_depth"})
        app_with_versions.state.run_design_loop = None
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done[0].get("confirm_ack_value") == "40.0\u202fmm", done
    assert done[0]["message"] == "Got it — Spacer depth stays 40.0\u202fmm.", done


def test_chat_e2e_offer_then_yes_mm_spelling(app_with_versions):
    """END-TO-END through the chat route (issue #265): a passing pass
    whose param's ``param_meta`` carries an explicit unit "mm" AND a
    declared axis produces a done-frame ``confirm_sentence`` of EXACTLY
    "I assumed 40.0\u202fmm for Spacer depth. Want it different?" (the
    template — no model sentence supplied — the value spelled with the
    U+202F narrow no-break space, byte-for-byte the deck's ``mm(40)``),
    and a clean "yes" on the next /chat request then produces an ack
    frame whose ``confirm_ack_value`` is "40.0\u202fmm" (and whose
    ``message`` carries the same spelling). Both frames ride the SAME
    seam: the entry ``offer_entry`` resolves the version's
    ``param_meta`` onto (the offer path — the adapter's
    ``_resolve_offer`` — and the ack path — the /chat acceptance
    route).

    The two /chat requests run on the fixture app back to back. The
    inflight design-loop flag is released in production by the SSE
    stream's ``finally`` (``d33d.streaming._stream_events`` — the single
    release point for every event source, on every exit path); a raw
    generator driven directly by a test bypasses that endpoint, so the
    test releases the flag between requests (a ``discard`` that mirrors
    the endpoint's ``finally``) — otherwise the second request would
    409 (the flag was claimed by the first)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"spacer_depth": 40.0},
            meta={"spacer_depth": {"axis": "D", "unit": "mm", "label": "Spacer depth"}},
        )

    async def _call(client):
        app = app_with_versions
        app.state.run_design_loop = _loop
        proj = await create_project(client)
        pid = proj["id"]
        r1, frames1 = await _drive_chat(app, client, pid, {"message": "make a spacer"})
        # Release the inflight flag — the SSE endpoint's ``finally`` does
        # this in production; a test driving the raw generator must
        # mirror it or the next request 409s.
        inflight = getattr(app.state, "design_loop_inflight", None)
        if inflight is not None:
            inflight.discard(pid)
        r2, frames2 = await _drive_chat(app, client, pid, {"message": "yes"})
        return r1.status_code, r2.status_code, frames1, frames2

    s1, s2, frames1, frames2 = run_async(app_with_versions, _call)
    assert s1 == 202 and s2 == 202
    done1 = [d for e, d in frames1 if e == "done"]
    done2 = [d for e, d in frames2 if e == "done"]
    assert done1 and done2
    # The offer: the template with the mm spelling (the deck's mm(40),
    # U+202F — the binding operator decision's exact string).
    assert done1[-1].get("confirm_sentence") == (
        "I assumed 40.0\u202fmm for Spacer depth. Want it different?"
    ), done1
    assert done1[-1].get("confirm_offer") == "spacer_depth"
    # The ack: value and message both carry the same mm spelling.
    assert done2[-1].get("confirm_ack_value") == "40.0\u202fmm", done2
    assert done2[-1]["message"] == "Got it — Spacer depth stays 40.0\u202fmm.", done2


class _TierStubResult:
    """A pass result whose ``best`` is a real ``IterationRecord`` for the
    tier tests (the adapter's offer resolution runs against the NEW
    version's row)."""

    def __init__(self, params: dict[str, Any], meta: dict[str, Any] | None = None) -> None:
        from d33d.design_loop import IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "pass"
        self.failure_reason = None
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 10; cube([W, W, W]);",
            render=_default_render(),
            score=Score(bits=(True, True, True, True, True), rank=5, tiebreak=(True,) * 5),
            params=dict(params),
            param_meta=dict(meta) if meta else {},
        )


def test_chat_tier2_unmapped_mm_offer(app_with_versions):
    """The acceptance case: "A spacer to lift a shelf 12 mm" states no
    axis (lift is excluded from the lexicon). With an assumed param of
    value 12, the offer is EXACTLY the tier-2 sentence with the value
    spelled the way ``mm(12)`` renders it ("12.0\u202fmm") — never the
    raw number, never the #250 template."""

    async def _loop(app, **kwargs):
        return _TierStubResult(
            {"lift_gap": 12.0},
            meta={"lift_gap": {"label": "Lift height", "unit": "mm"}},
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        app_with_versions.state.answer_question = None
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions,
            client,
            pid,
            {"message": "A spacer to lift a shelf 12 mm", "chat_history": []},
        )
        return r.status_code, frames, svc.latest_version(pid)

    status, frames, latest = run_async(app_with_versions, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[-1].get("confirm_offer") == "lift_gap", done
    # The tier-2 sentence, mm()-formatted value, exact string.
    assert done[-1].get("confirm_sentence") == (
        "You said 12.0\u202fmm — I used it for Lift height. Right?"
    ), done
    # The new version persisted the carried (empty) stated_dims: the
    # message stated no axis, so the effective set is {} → NULL.
    assert latest["stated_dims"] is None, latest


def test_chat_tier1_released_axis_offer(app_with_versions):
    """Tier 1 through the chat route: v1 states H (the latest version's
    persisted ``stated_dims`` {"H": 12}), then "make it taller" (a
    relative H cue) RELEASES H — the offered param is the H-declared
    assumed param of the NEW version, and the sentence is the tier-1
    template with the mm()-formatted value."""

    async def _loop(app, **kwargs):
        return _TierStubResult(
            {"lift_height": 15.0},
            meta={"lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"}},
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        app_with_versions.state.answer_question = None
        await svc.create_version(
            pid,
            {"lift_height": 12.0},
            param_meta={"lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"}},
            stated_dims={"H": 12.0},
        )
        # v2 must exist before the chat POST so _resolve_offer can read it
        # from the DB (the adapter's _resolve_version_create creates the
        # version row from the stub's result, but _resolve_offer reads the
        # row by id — the row must exist first). The stub loop's result
        # carries the same params as v2; the adapter's create_version will
        # create a third version (name collision → suffix), which is a test
        # harness artifact. The offer resolution reads v2's row and builds
        # the tier-1 sentence from it.
        v2 = await svc.create_version(
            pid,
            {"lift_height": 15.0},
            param_meta={"lift_height": {"label": "Lift height", "unit": "mm", "axis": "H"}},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "make it taller", "chat_history": []}
        )
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[-1].get("confirm_offer") == "lift_height", done
    assert done[-1].get("confirm_sentence") == (
        "You asked for taller — I made Lift height 15.0\u202fmm. Right?"
    ), done


def _drive_chat(app, client, pid, body):
    """POST ``body`` to /chat and drive the event source to its terminal
    frame (the same pattern the issue #54 chat tests use). Returns a
    coroutine — ``await`` it from inside an async ``_call`` closure.
    Returns ``(response, frames)``."""

    async def _drive():
        r = await client.post(f"/api/projects/{pid}/chat", json=body)
        source = app.state.event_sources.get(pid)
        frames = []
        assert source is not None, "event source not registered before 202"
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    return _drive()


def test_chat_pass_with_assumed_axis_param_offers_it(app_with_versions):
    """A passing pass whose new version carries an assumed param with a
    model-declared axis → the done frame's ADDITIVE ``confirm_offer`` /
    ``confirm_sentence`` fields name that axis param first (the template
    sentence — no model sentence supplied), and the pending offer is
    stored server-side on the project row (never client-derived)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"width": 60.0, "wall_thickness": 3.0},
            meta={"width": {"label": "Width", "unit": "mm", "axis": "W"}},
            confirm_first="wall_thickness",  # the axis param must win
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        latest = app_with_versions.state.versions.latest_version(pid)
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, latest, row_offer

    status, frames, latest, row_offer = run_async(app_with_versions, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # The offer rides the done frame as ADDITIVE ``confirm_*`` fields
    # (the SPA's App.tsx routes them to a separate plain assistant
    # message after the pass card — issue #250's task-b contract).
    assert done[-1].get("confirm_offer") == "width", done
    # The tier-3 value is mm-formatted (issue #265 — the param's metadata
    # carries unit "mm" explicitly): the offer and the ack it leads to use
    # the same spelling.
    assert done[-1].get("confirm_sentence") == (
        "I assumed 60.0\u202fmm for Width. Want it different?"
    ), done
    # Server-side pending-offer state: the offer's version is the NEW
    # version, the param is the axis param.
    assert row_offer == {"version_id": latest["id"], "param": "width"}


def test_chat_pass_disagreeing_param_never_offered(app_with_versions):
    """Issue #264 ACCEPTANCE (the v25 offer gate): a v1 whose persisted
    bbox CONTESTS the param (spacer_width 40, declared axis W; bbox
    43.80 x 43.90 x 12.0 — the flared lip adds 3.8 mm, outside the
    tolerance) renders the param ``disagrees`` (``disagrees_source =
    "model"``) in the version's own design-state block. A passing pass
    that keeps the same value (40) must NOT offer the param: the offer
    sentence "I assumed 40 for Spacer width" would be false — the
    measurement already contradicts it. The done frame carries no offer
    and the pending-offer state is absent."""

    async def _loop(app, **kwargs):
        return _V25PassStub()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        app_with_versions.state.answer_question = None
        # v1: the v25 shape — the persisted bbox 43.80 x 43.90 x 12.0
        # makes both declared-axis params disagree (model-source).
        await svc.create_version(
            pid,
            {"spacer_width": 40.0, "spacer_depth": 40.0},
            param_meta={
                "spacer_width": {"label": "Spacer width", "unit": "mm", "axis": "W"},
                "spacer_depth": {"label": "Spacer depth", "unit": "mm", "axis": "D"},
            },
            bbox=(43.80, 43.90, 12.0),
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a flared lip"}
        )
        latest = svc.latest_version(pid)
        row_offer = svc.get_pending_offer(pid)
        return r.status_code, frames, latest, row_offer

    status, frames, latest, row_offer = run_async(app_with_versions, _call)
    assert status == 202, status
    # The new version's own block renders both params disagrees
    # (model-source) — the precondition the gate keys on.
    from d33d.design_state import state_block_for_version

    block = state_block_for_version(
        latest["params"], latest["bbox"], latest["stated_dims"],
        latest["param_meta"], latest["confirmed_params"],
    )
    by_name = {e["name"]: e for e in block if e.get("kind") == "param"}
    for name in ("spacer_width", "spacer_depth"):
        assert by_name[name]["provenance"] == "disagrees", by_name[name]
        assert by_name[name]["disagrees_source"] == "model", by_name[name]
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # NEITHER param is offered (both disagree with the measurement);
    # no offer at all, and no pending-offer state on the row.
    assert "confirm_offer" not in done[-1], done
    assert row_offer is None


def test_chat_pass_confirm_first_valid_offers_that_param(app_with_versions):
    """No axis params, but the model's ``confirm_first`` names a valid
    assumed numeric param of the new version → that param is the offer
    (the model's sentence is used when it passes the number guard)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
            confirm_first="wall_thickness",
            confirm_sentence="I assumed 3 mm Wall thickness. Want it thinner?",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done
    # The model's sentence names the value in the BARE spelling ("3") —
    # issue #265 accepts either the bare or the mm-formatted value name,
    # so the model's sentence passes the guard verbatim.
    assert done[-1].get("confirm_sentence") == (
        "I assumed 3 mm Wall thickness. Want it thinner?"
    ), done


def test_chat_pass_invalid_confirm_first_offers_nothing(app_with_versions):
    """A ``confirm_first`` naming a nonexistent param is rejected and —
    with no declared-axis param either — the pass carries NO offer
    (never a silent second guess) and the pending offer is cleared."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="ghost_param",  # not a param of this version
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, row_offer

    status, frames, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert "confirm_offer" not in done[-1], done
    assert "confirm_sentence" not in done[-1], done
    assert row_offer is None


def test_chat_pass_model_sentence_with_invented_number_uses_template(app_with_versions):
    """The model's ``confirm_sentence`` invents a number ("4 mm" when the
    param is 3) → the issue #249 number guard fails it → the
    deterministic template is used instead."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
            confirm_sentence="I assumed 4 mm walls. Want it thinner?",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done
    # No metadata on this version → the entry carries no meta_unit /
    # param_axis → the value keeps the bare ``:g`` spelling (issue #265's
    # unitless rule), and the invented-number model sentence falls to the
    # template.
    assert done[-1].get("confirm_sentence") == (
        "I assumed 3 for wall_thickness. Want it different?"
    ), done


def test_chat_exhausted_emits_no_offer_and_clears_state(app_with_versions):
    """An exhausted pass emits NO offer (the error frame carries no
    ``offer`` field) and CLEARS any pending offer (a failed run cannot
    keep a live offer)."""

    async def _loop(app, **kwargs):
        return _ExhaustedStub({"wall_thickness": 3.0})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, row_offer

    status, frames, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    event_names = [e for e, _d in frames]
    assert event_names[-1] == "error", frames
    for _e, d in frames:
        assert "confirm_offer" not in d, frames
        assert "confirm_sentence" not in d, frames
    assert row_offer is None


def test_chat_second_pass_with_new_param_offers_it(app_with_versions):
    """A follow-up pass that keeps the wall thickness (unchanged → not in
    the changed set) and adds a new assumed param: the new param is a
    fresh assumption and IS offered (the changed-set rule excludes only
    value-changed params, never new ones)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="fillet_radius",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "fillet_radius", done
    assert done[-1].get("confirm_sentence") == (
        "I assumed 1.5 for fillet_radius. Want it different?"
    ), done


def test_chat_yes_after_offer_confirms_param_no_new_version(app_with_versions):
    """DECISIVE: a "yes" after an offer (server-side pending-offer state
    from a prior passing pass) → the param is recorded as user-confirmed
    on the version (``confirmed_params``), the design-state block renders
    it ``stated`` (rule (b)), the acknowledgement is a ``kind:
    "answer"`` plain-message done frame, NO design run happens, and NO
    new version is created."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # A prior passing pass: a version with an assumed wall thickness
        # + the offer the adapter would have set (persisted server-side,
        # the way the passing pass's adapter writes it).
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        loop_called = {"n": 0}

        async def _loop(app, **kwargs):
            loop_called["n"] += 1
            return _OfferStubResult({"wall_thickness": 3.0})

        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        row = svc.latest_version(pid)
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        ds = await client.get(f"/api/projects/{pid}/design-state")
        return (
            r.status_code,
            frames,
            row,
            len(timeline),
            timeline[0]["id"],
            ds.json(),
            loop_called["n"],
            svc.get_pending_offer(pid),
        )

    status, frames, row, version_count, latest_id, ds_body, loop_n, row_offer = (
        run_async(app_with_versions, _call)
    )
    assert status == 202, status
    # NO design run: the acceptance pre-route short-circuits before it.
    assert loop_n == 0, "the design loop must NOT run on an accepted offer"
    # The reply is a kind "answer" plain-message done frame (the
    # acknowledgement), not a design-loop done frame.
    done = [d for e, d in frames if e == "done"]
    assert len(done) == 1, frames
    assert done[0].get("kind") == "answer", done
    assert "confirm_offer" not in done[0]
    # The acknowledgement rides the done frame with the ADDITIVE
    # ``confirm_ack`` fields (the SPA renders the value in the mono
    # face — a measurement must never hide inside a sentence).
    assert done[0].get("confirm_ack") is True, done
    assert done[0].get("confirm_ack_label") == "Wall thickness", done
    assert done[0].get("confirm_ack_value") == "3.0\u202fmm", done
    # The ack uses the deterministic template with the #248 label, value
    # mm-formatted (issue #265 — the param_meta carries unit "mm").
    assert done[0]["message"] == "Got it — Wall thickness stays 3.0\u202fmm.", done
    # The version is user-confirmed (the ONLY writer is this flow).
    assert row["confirmed_params"] == {"wall_thickness": 3.0}, row
    # The design-state block renders it stated (rule (b)).
    by_name = {e["name"]: e for e in ds_body}
    assert by_name["wall_thickness"]["provenance"] == "stated", by_name
    # NO new version: the version count is unchanged and the latest id is
    # the pre-existing version.
    assert version_count == 1, "acceptance must not create a version"
    # The offer is consumed (cleared — it never re-fires on a later yes).
    assert row_offer is None


def test_chat_yes_but_make_it_2mm_is_not_an_acceptance(app_with_versions):
    """"yes but make it 2 mm" is NOT an acceptance (the change request
    routes to the design loop — the pending offer stays as-is for a
    future clean yes, and the design run happens)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 2.0},
            confirm_first="wall_thickness",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes but make it 2 mm"}
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        row = svc.latest_version(pid)
        old = svc.get_version(pid, v["id"])
        return r.status_code, frames, len(timeline), row, old, svc.get_pending_offer(pid)

    status, frames, version_count, row, old, row_offer = run_async(
        app_with_versions, _call
    )
    assert status == 202
    # The design loop ran (a new version exists — the change went through
    # the normal route).
    assert version_count == 2, f"expected a new version, got {version_count}"
    # The old version is NOT confirmed (no confirmation happened — the
    # message was a change request, not an acceptance).
    assert old["confirmed_params"] is None
    # The new version carries a FRESH offer for its own param value
    # (the new version's wall_thickness=2.0 is assumed — the old
    # version's offer was superseded by the new version; the carry
    # forward carried nothing because v1 had no confirmed set).
    # The new version's wall_thickness (2.0, the user's change) was the
    # model's confirm_first flag, but the changed set excludes a
    # user-requested value from being the OFFER's target — a changed
    # value is the user's own move, not an assumption to confirm. With
    # no other eligible param, no offer is emitted.
    # (The acceptance test above pins the confirmation path; the
    # "never re-offer a CONFIRMED param" case is pinned at the pure level
    # in test_selection_never_re_offers_confirmed_param.)
    assert row_offer is None, f"changed param was re-offered: {row_offer}"


def test_chat_yes_after_offer_on_unchanged_param_confirms(app_with_versions):
    """A follow-up pass that keeps the wall thickness (UNCHANGED — not in
    the changed set) and the offer still live from the previous version
    is superseded by the new version's offer (the offer's version must be
    the LATEST — a "yes" to the old version's offer lapses)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v1 = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v1["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        v2 = svc.latest_version(pid)
        return r.status_code, frames, v2, svc.get_pending_offer(pid)

    status, frames, v2, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    # The old offer (on v1) is superseded by the new version's offer:
    assert row_offer is not None and row_offer["version_id"] == v2["id"]
    # The new pass's done frame carries a FRESH offer (additive
    # ``confirm_*`` fields) for its own version.
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done


def test_chat_yes_without_pending_offer_routes_normally(app_with_versions):
    """A bare "yes" with NO pending offer state falls through to the
    normal routing (the design loop) — the acceptance requires the
    server-side offer, never a client-side guess."""

    async def _loop(app, **kwargs):
        return _OfferStubResult({"wall_thickness": 3.0})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        loop_called = {"n": 0}

        async def _loop2(app, **kwargs):
            loop_called["n"] += 1
            return _OfferStubResult({"wall_thickness": 3.0})

        app_with_versions.state.run_design_loop = _loop2
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        return r.status_code, frames, loop_called["n"]

    status, frames, loop_n = run_async(app_with_versions, _call)
    assert status == 202
    assert loop_n == 1, "a bare yes with no pending offer must route to the loop"
    # No kind "answer" frame from an offer (the loop's done frame has no
    # kind field — the pass created a version normally).
    done = [d for e, d in frames if e == "done"]
    assert "kind" not in done[-1], done


def test_offer_state_survives_reopen_round_trip(app_with_versions):
    """Persistence: the pending offer (param P on version V) and
    ``confirmed_params`` survive a client-session boundary (a fresh
    client over the same app/DB — the "close the browser, come back"
    simulation): the acceptance check reads them from SERVER state,
    never from the client's chat."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        # A "yes" in a FRESH client session (the server state is the
        # only source of the offer).
        from httpx import ASGITransport, AsyncClient

        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            r = await fresh.post(f"/api/projects/{pid}/chat", json={"message": "yes"})
            source = app_with_versions.state.event_sources.get(pid)
            frames = []
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
            back = svc.latest_version(pid)
            offer = svc.get_pending_offer(pid)
            return r.status_code, frames, back, offer

    status, frames, back, offer = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[0].get("kind") == "answer", done
    assert done[0]["message"] == "Got it — Wall thickness stays 3.0\u202fmm."
    assert done[0].get("confirm_ack") is True, done
    assert done[0].get("confirm_ack_label") == "Wall thickness", done
    assert done[0].get("confirm_ack_value") == "3.0\u202fmm", done
    # The confirmation persisted on the version row.
    assert back["confirmed_params"] == {"wall_thickness": 3.0}
    # The offer is consumed.
    assert offer is None


def test_carried_forward_confirmed_param_not_re_offered(app_with_versions):
    """A param confirmed on the previous version (identical value on the
    new one) is NEVER re-offered on the new version: a flag naming the
    carried param is rejected (a confirmation the user made on the
    previous version is confirmed evidence, not an assumption to
    re-confirm — it excludes the param from the new version's offer
    selection), and the new version's ``confirmed_params`` row stays
    NULL (its own set, empty until the user accepts THIS version's
    offer — no carry-forward write)."""

    async def _loop(app, **kwargs):
        # A second pass that keeps wall_thickness at 3 (carried forward)
        # and introduces a new assumed param the model flags:
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="wall_thickness",  # the CARRIED param — rejected
        )

    async def _call(client):
        # The shared connection is closed after the previous test's
        # lifespan teardown; reopen it for this case (the lifespan's
        # ``if state.conn is None`` guard then skips the reconnect, so
        # the reopened connection stays in use — the same pattern as
        # ``test_chat_stage1_matrix_routes_imperatives_to_loop``).
        closed = False
        try:
            app_with_versions.state.conn.raw.execute("SELECT 1")
        except Exception:
            closed = True
        if app_with_versions.state.conn is None or closed:
            import d33d.db as _db
            from d33d import versions as _versions_mod

            fresh = _db.connect(app_with_versions.state.db_path)
            _versions_mod.migrate(fresh)
            app_with_versions.state.conn = fresh
            app_with_versions.state.versions = _versions_mod.VersionService(fresh)
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v1 = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v1["id"], "param": "wall_thickness"})
        # The user accepts (wall_thickness confirmed on v1).
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        # The answer path's one-frame source releases its in-flight claim
        # when drained (its ``finally`` — the generator's post-yield code
        # runs on exhaustion); the SSE endpoint does the same in
        # production. A follow-up pass needs the claim free.
        inflight = app_with_versions.state.design_loop_inflight
        if inflight is not None:
            inflight.discard(pid)
        # A second pass creates v2 (the adapter's offer resolution runs
        # against v2 with the carried confirmed set).
        r2, frames2 = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        v2 = svc.latest_version(pid)
        assert v2["id"] != v1["id"]
        v1_row = svc.get_version(pid, v1["id"])
        return frames, frames2, v2, v1_row, svc.get_pending_offer(pid)

    frames, frames2, v2, v1_row, row_offer = run_async(app_with_versions, _call)
    # v1 confirmed (read inside the lifespan — the connection is live).
    assert v1_row["confirmed_params"] == {"wall_thickness": 3.0}, v1_row
    # v2's confirmed set is its OWN (NULL — the accepted-offer flow is
    # the ONLY writer, and the user has not accepted v2's offer yet);
    # the carried param is excluded from the new version's offer by the
    # previous version's stashed confirmed set (no carry-forward write
    # onto the new row).
    assert v2["confirmed_params"] is None, v2
    # The flag named the CARRIED param (wall_thickness=3, unchanged): a
    # confirmation the user made on the previous version is confirmed
    # evidence, not an assumption to re-confirm — a confirmed param is
    # excluded from the offer (no other eligible param is flagged, so
    # the offer is absent).
    done = [d for e, d in frames2 if e == "done"]
    assert "confirm_offer" not in done[-1], done
    assert row_offer is None


# ---------------------------------------------------------------------------
# Item 1: cross-project race — the pre-pass confirmed set is LOCAL, not
# stashed in ``app.state``
# ---------------------------------------------------------------------------


def test_interleaved_passes_use_own_projects_confirmed_set(app_with_versions):
    """TWO projects' passes interleaved on the shared app: each pass's
    offer selection uses ITS OWN project's pre-pass confirmed set, never
    the other project's. Before the fix, ``app.state._offer_prev_confirmed``
    was a single global attribute — a pass on project B that started after
    project A's pass would read project A's confirmed set (a param B never
    confirmed would be excluded from B's offer, or a confirmed param would
    be re-offered)."""

    async def _loop_a(app, **kwargs):
        # Project A: pass 1 creates a version with wall_thickness assumed.
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _loop_b(app, **kwargs):
        # Project B: pass 1 creates a version with wall_thickness assumed.
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _loop_a2(app, **kwargs):
        # Project A: pass 2 keeps wall_thickness (carried forward) and adds
        # a new param the model flags.
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="wall_thickness",  # the CARRIED param — must be rejected
        )

    async def _loop_b2(app, **kwargs):
        # Project B: pass 2 keeps wall_thickness (carried forward) and adds
        # a new param the model flags.
        return _OfferStubResult(
            {"wall_thickness": 3.0, "groove_depth": 2.0},
            confirm_first="wall_thickness",  # the CARRIED param — must be rejected
        )

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions

        # Project A: pass 1 → v1_a (wall_thickness assumed, offer set).
        proj_a = await create_project(client, "project A")
        pid_a = proj_a["id"]
        app_with_versions.state.run_design_loop = _loop_a
        r_a1, _ = await _drive_chat(app_with_versions, client, pid_a, {"message": "make a part"})
        assert r_a1.status_code == 202

        # Project A: user accepts the offer (wall_thickness confirmed on v1_a).
        inflight = app_with_versions.state.design_loop_inflight
        if inflight is not None:
            inflight.discard(pid_a)
        r_a1yes, _ = await _drive_chat(app_with_versions, client, pid_a, {"message": "yes"})
        assert r_a1yes.status_code == 202

        # Project B: pass 1 → v1_b (wall_thickness assumed, offer set).
        # This runs AFTER project A's acceptance — if the pre-pass set were
        # stashed in app.state, project B's adapter would read project A's
        # confirmed set (the cross-project race).
        proj_b = await create_project(client, "project B")
        pid_b = proj_b["id"]
        app_with_versions.state.run_design_loop = _loop_b
        r_b1, _ = await _drive_chat(app_with_versions, client, pid_b, {"message": "make a part"})
        assert r_b1.status_code == 202

        # Project B: user accepts the offer (wall_thickness confirmed on v1_b).
        if inflight is not None:
            inflight.discard(pid_b)
        r_b1yes, _ = await _drive_chat(app_with_versions, client, pid_b, {"message": "yes"})
        assert r_b1yes.status_code == 202

        # Project A: pass 2 → v2_a. The adapter must read v1_a's confirmed
        # set (wall_thickness confirmed) — NOT project B's set (which also
        # confirms wall_thickness, but via project B's own acceptance). The
        # flag names the CARRIED param → rejected → no offer.
        if inflight is not None:
            inflight.discard(pid_a)
        app_with_versions.state.run_design_loop = _loop_a2
        r_a2, frames_a2 = await _drive_chat(app_with_versions, client, pid_a, {"message": "add a fillet"})
        assert r_a2.status_code == 202
        v2_a = svc.get_version(pid_a, (await client.get(f"/api/projects/{pid_a}/versions")).json()[-1]["id"])

        # Project B: pass 2 → v2_b. Same expectation — the adapter must
        # read v1_b's confirmed set, not project A's.
        if inflight is not None:
            inflight.discard(pid_b)
        app_with_versions.state.run_design_loop = _loop_b2
        r_b2, frames_b2 = await _drive_chat(app_with_versions, client, pid_b, {"message": "add a groove"})
        assert r_b2.status_code == 202
        v2_b = svc.get_version(pid_b, (await client.get(f"/api/projects/{pid_b}/versions")).json()[-1]["id"])

        done_a2 = [d for e, d in frames_a2 if e == "done"]
        done_b2 = [d for e, d in frames_b2 if e == "done"]
        return (
            v2_a, v2_b,
            done_a2[-1] if done_a2 else {},
            done_b2[-1] if done_b2 else {},
            svc.get_pending_offer(pid_a),
            svc.get_pending_offer(pid_b),
        )

    v2_a, v2_b, done_a2, done_b2, offer_a, offer_b = run_async(
        app_with_versions, _call
    )
    # Both projects: v2's confirmed set is its own (NULL — no carry-forward
    # write). The carried param (wall_thickness, confirmed on v1) is
    # EXCLUDED from the offer by the pre-pass confirmed set → the flag
    # naming it is rejected → no offer (a new param is introduced but not
    # flagged, so no fallback).
    assert v2_a["confirmed_params"] is None, v2_a
    assert v2_b["confirmed_params"] is None, v2_b
    assert "confirm_offer" not in done_a2, done_a2
    assert "confirm_offer" not in done_b2, done_b2
    assert offer_a is None
    assert offer_b is None


# ---------------------------------------------------------------------------
# Item 3: malformed confirmed_params row + set_pending_offer shape guard
# ---------------------------------------------------------------------------


def test_record_confirmation_malformed_confirmed_params_starts_from_empty(app_with_versions):
    """A version row whose ``confirmed_params`` is NOT a dict (corrupted
    JSON or hand-edited row): ``record_confirmation`` logs a WARNING and
    starts from ``{}`` — never a 500, never a crash."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "malformed test")
        pid = proj["id"]
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        vid = v["id"]
        # Corrupt the confirmed_params column: store a JSON list (valid
        # JSON, not a dict — simulates a corrupted row that was written
        # by a buggy writer). get_version's json.loads parses it to a
        # list; record_confirmation must handle the non-dict gracefully.
        svc.conn.raw.execute(
            "UPDATE versions SET confirmed_params = ? WHERE id = ?",
            ("[1, 2, 3]", vid),
        )
        svc.conn.commit()
        # record_confirmation must NOT raise — it starts from {}.
        svc.record_confirmation(pid, vid, "wall_thickness", 3.0)
        row = svc.get_version(pid, vid)
        return row

    row = run_async(app_with_versions, _call)
    # The write overwrote the malformed value with a well-formed set.
    assert row["confirmed_params"] == {"wall_thickness": 3.0}, row


def test_set_pending_offer_shape_guard_rejects_bad_input(app_with_versions):
    """``set_pending_offer`` with a malformed offer dict raises ValueError
    (never silently persists a bad row that would degrade to no offer on
    read anyway — the raise is the early, loud failure)."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "shape guard test")
        pid = proj["id"]
        results = {}
        # version_id is a bool (not a real int).
        try:
            svc.set_pending_offer(pid, {"version_id": True, "param": "x"})
            results["bool_version"] = "no raise"
        except ValueError as e:
            results["bool_version"] = str(e)
        # version_id is a string.
        try:
            svc.set_pending_offer(pid, {"version_id": "1", "param": "x"})
            results["str_version"] = "no raise"
        except ValueError as e:
            results["str_version"] = str(e)
        # param is empty.
        try:
            svc.set_pending_offer(pid, {"version_id": 1, "param": ""})
            results["empty_param"] = "no raise"
        except ValueError as e:
            results["empty_param"] = str(e)
        # param is not a string.
        try:
            svc.set_pending_offer(pid, {"version_id": 1, "param": 42})
            results["non_str_param"] = "no raise"
        except ValueError as e:
            results["non_str_param"] = str(e)
        # version_id missing.
        try:
            svc.set_pending_offer(pid, {"param": "x"})
            results["missing_version"] = "no raise"
        except ValueError as e:
            results["missing_version"] = str(e)
        # A well-formed offer still works (the guard doesn't reject valid
        # input).
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        results["valid"] = svc.get_pending_offer(pid)
        # None (clear) still works.
        svc.set_pending_offer(pid, None)
        results["clear"] = svc.get_pending_offer(pid)
        return results

    results = run_async(app_with_versions, _call)
    # All bad inputs raised ValueError.
    assert "no raise" not in results["bool_version"]
    assert "no raise" not in results["str_version"]
    assert "no raise" not in results["empty_param"]
    assert "no raise" not in results["non_str_param"]
    assert "no raise" not in results["missing_version"]
    # Valid offer was persisted.
    assert results["valid"] is not None
    assert results["valid"]["param"] == "wall_thickness"
    # Clear worked.
    assert results["clear"] is None


# ---------------------------------------------------------------------------
# Item 5: ack label is always non-empty
# ---------------------------------------------------------------------------


def test_ack_label_falls_back_to_param_name(app_with_versions):
    """The ack's ``confirm_ack_label`` is ALWAYS non-empty: when the
    param has no user-facing label (``param_meta`` is absent or the label
    key is missing), the label falls back to the param's identifier —
    never an empty string."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "ack label test")
        pid = proj["id"]
        # A version with NO param_meta (no label — the identifier fallback
        # must fire).
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        # No param_meta → the design-state entry has no "label" key →
        # the ack label falls back to the param name.
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = None
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "yes"})
        done = [d for e, d in frames if e == "done"]
        return r.status_code, done

    status, done = run_async(app_with_versions, _call)
    assert status == 202, status
    assert len(done) == 1, done
    # The ack label is the param's identifier (non-empty, never "").
    assert done[0].get("confirm_ack_label") == "wall_thickness", done
    assert done[0]["confirm_ack_label"] != ""
    # The ack value is BARE (the version has no param_meta — no unit, no
    # axis — the identifier-fallback case keeps the ``:g`` spelling).
    assert done[0].get("confirm_ack_value") == "3", done
    # The message uses the identifier as the label.
    assert done[0]["message"] == "Got it — wall_thickness stays 3.", done
