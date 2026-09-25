"""Unit tests for d33d.dimension_protocol — CLARIFY-before-code gate.

The spec of record (04-design-loop.md, step 1) pins:

* Zero OpenSCAD output before dimensions are clarified (mm, never
  estimated). The gate :func:`require_dimensions_confirmed` must return
  ``confirmed=False`` + clarifying questions until the user has explicitly
  confirmed a COMPLETE W/D/H set AND a fit type.
* AI pre-fill is a CONFIRMABLE SUGGESTION only — never ground truth. A
  bare pre-fill without user confirmation leaves the gate closed.
* The agent PROACTIVELY ASKS FIT TYPE and applies the FDM clearance table
  (slip 0.2–0.4 mm total diametral, press ~0 to −0.1 mm interference,
  holes print 0.1–0.25 mm undersize). Tolerance resolves to a concrete
  named value.
* Stated dimensions + resolved tolerance are NAMED PARAMETERS, never
  literals in geometry expressions. The emitted structure (the
  ``{name: value}`` map / ``scad_param_block`` text) is the data shape the
  .scad generator interpolates as ``name = value;`` declarations — this
  suite proves that shape is correct and usable (it does not build a
  full .scad templater, which is the design loop / downstream ticket's
  concern).
* The confirmed set is persisted alongside the transcript
  (``d33d.db.put_dimensions`` / ``get_dimensions``) so the #3 bbox gate has
  a record to compare against. "Active version" promotion is issue #7.

All tests are fast (no Docker, no network) and use ``:memory:`` SQLite.
"""

from __future__ import annotations

import re

import pytest

from d33d import db
from d33d.dimension_protocol import (
    DIMENSION_AXES,
    FDM_CLEARANCE_TABLE,
    DimensionClarification,
    effective_stated_dims,
    emit_named_params,
    latest_stated_dims_dict,
    offer_tier_signals,
    require_dimensions_confirmed,
    resolution_questions,
    resolve_tolerance_mm,
    stated_axes_from_message,
    stated_dims_from_message,
    user_quoted_unmapped_mm,
)

# ---------------------------------------------------------------------------
# Fixtures: the three canonical chat shapes
# ---------------------------------------------------------------------------


def _full_chat() -> list[str]:
    """A chat where the user has explicitly stated all three dims + fit."""
    return [
        "Here's the bracket photo.",
        "W: 42",
        "D: 30",
        "H = 20 mm",
        "It's a slip fit for the peg.",
    ]


def _partial_chat() -> list[str]:
    """A chat where one dimension is missing (H not stated)."""
    return [
        "Here's the bracket photo.",
        "W: 42",
        "D: 30",
    ]


def _no_dims_chat() -> list[str]:
    """A chat with no stated dimensions at all."""
    return ["Make me a bracket from this photo."]


# ---------------------------------------------------------------------------
# The gate: zero .scad before clarification
# ---------------------------------------------------------------------------


def test_gate_open_when_no_dimensions_stated() -> None:
    """No stated dims -> confirmed=False, with a clarifying question.
    The design loop must ask (and emit no .scad) until this flips."""
    c = require_dimensions_confirmed(_no_dims_chat(), None)
    assert c.confirmed is False
    assert c.questions  # at least one question to ask
    # Must ask for the dimensions (W/D/H).
    assert any(re.search(r"W|D|H|millimetres|mm", q) for q in c.questions)
    # And must proactively ask fit type.
    assert any("fit" in q.lower() for q in c.questions)


def test_gate_open_when_one_dimension_missing() -> None:
    """W and D stated, H missing -> confirmed=False, H is the question."""
    c = require_dimensions_confirmed(_partial_chat(), None)
    assert c.confirmed is False
    assert c.questions
    assert any("H" in q for q in c.questions)


def test_gate_closed_after_full_confirmation() -> None:
    """All three dims + fit type stated -> confirmed=True, no questions."""
    c = require_dimensions_confirmed(_full_chat(), None)
    assert c.confirmed is True
    assert c.questions == ()
    assert c.stated_dims == (42.0, 30.0, 20.0)


def test_gate_open_when_fit_type_missing() -> None:
    """All three dims stated but no fit type -> the agent must ASK fit
    type (the spec's proactive-ask mandate)."""
    c = require_dimensions_confirmed(["W: 42", "D: 30", "H = 20"], stated_dims=None)
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_gate_ignores_non_positive_dimensions() -> None:
    """Zero/negative stated dims are not a valid dimension (not mm)."""
    c = require_dimensions_confirmed(
        ["W: 0", "D: 30", "H: -5"], stated_dims={"fit_type": "slip"}
    )
    assert c.confirmed is False


# ---------------------------------------------------------------------------
# AI pre-fill is a confirmable suggestion ONLY
# ---------------------------------------------------------------------------


def test_ai_prefill_alone_does_not_confirm() -> None:
    """A bare AI pre-fill (no user confirmation) must NOT satisfy the gate.
    The spec: AI may pre-fill a low-confidence suggestion the user confirms;
    it is never the source of truth."""
    c = require_dimensions_confirmed(
        ["Make me a bracket from this photo."],
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is False
    assert c.questions
    # The suggestion is surfaced (confirmable) but not accepted as truth.
    assert c.suggested == {"W": 40.0, "D": 30.0, "H": 20.0}
    # And no params were applied.
    assert c.params == {}


def test_ai_prefill_confirmed_by_user_satisfies_gate() -> None:
    """An AI pre-fill the USER confirms in chat counts as a stated dim."""
    chat = [
        "Make me a bracket from this photo.",
        "Suggested W=40, D=30, H=20",
        "Yes, that looks right, and it's a slip fit.",
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is True
    assert c.stated_dims == (40.0, 30.0, 20.0)


def test_ai_prefill_user_confirmation_requires_fit_type_too() -> None:
    """Confirming the pre-fill without a fit type still leaves the gate
    open (fit type is a separate, mandatory question)."""
    chat = [
        "Suggested W=40, D=30, H=20",
        "Yes, that's right.",
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_explicit_stated_dims_take_priority_over_suggestion() -> None:
    """If the user states a real dim AND the AI suggested a different one,
    the user's stated value wins (suggestion is never ground truth)."""
    c = require_dimensions_confirmed(
        ["slip fit"],
        stated_dims={"W": 42.0, "D": 30.0, "H": 20.0},
        ai_suggested={"W": 999.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is True
    assert c.stated_dims == (42.0, 30.0, 20.0)
    # The suggestion is still surfaced, but W is the user's value.
    assert c.suggested.get("W") == 999.0


# ---------------------------------------------------------------------------
# Fit type + FDM clearance table
# ---------------------------------------------------------------------------


def test_fit_type_set_is_the_closed_enum() -> None:
    assert set(FDM_CLEARANCE_TABLE) == {
        "slip",
        "press",
        "interference",
        "snap",
        "no_fit",
    }


def test_slip_clearance_band_matches_spec() -> None:
    """Spec: slip 0.2–0.4 mm total diametral."""
    assert FDM_CLEARANCE_TABLE["slip"] == (0.2, 0.4)


def test_press_clearance_band_matches_spec() -> None:
    """Spec: press ~0 to −0.1 mm interference (negative = interference)."""
    assert FDM_CLEARANCE_TABLE["press"] == (-0.1, 0.0)


def test_resolve_tolerance_returns_concrete_midpoint() -> None:
    """Each fit resolves to a concrete float (the band midpoint, rounded
    to 0.05 mm)."""
    assert resolve_tolerance_mm("slip") == 0.3
    assert resolve_tolerance_mm("press") == -0.05
    assert resolve_tolerance_mm("no_fit") == 0.0


def test_resolve_tolerance_rejects_unknown_fit() -> None:
    with pytest.raises(ValueError):
        resolve_tolerance_mm("glue")  # type: ignore[arg-type]


def test_gate_applies_resolved_tolerance() -> None:
    """A confirmed clarification carries the resolved FDM tolerance."""
    c = require_dimensions_confirmed(_full_chat(), None)  # slip fit
    assert c.confirmed is True
    assert c.tolerance_mm == resolve_tolerance_mm("slip")
    assert c.fit_type == "slip"


def test_gate_applies_press_interference() -> None:
    chat = ["W: 42", "D: 30", "H: 20", "press fit"]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is True
    assert c.fit_type == "press"
    assert c.tolerance_mm == -0.05


# ---------------------------------------------------------------------------
# Named-parameter emission — the .scad generator's data shape
# ---------------------------------------------------------------------------


def test_emit_named_params_is_a_name_value_map() -> None:
    """The output is a {param_name: value} map usable for ``name = value;``
    interpolation — W/D/H + the resolved tolerance + fit type."""
    c = require_dimensions_confirmed(_full_chat(), None)
    p = emit_named_params(c)
    assert set(p) == {"W", "D", "H", "tolerance_mm", "fit_type"}
    assert p["W"] == "42"
    assert p["D"] == "30"
    assert p["H"] == "20"
    assert p["tolerance_mm"] == "0.3"  # slip midpoint
    assert p["fit_type"] == "slip"
    # Values are strings, ready to drop into a declaration verbatim.
    assert all(isinstance(v, str) for v in p.values())


def test_emit_named_params_requires_confirmation() -> None:
    """The gate must pass before any params are emitted (CLARIFY-before-
    code). Emitting on an unconfirmed clarification is an error."""
    c = require_dimensions_confirmed(_no_dims_chat(), None)
    assert c.confirmed is False
    with pytest.raises(ValueError):
        emit_named_params(c)


def test_scad_param_block_has_no_magic_literals_in_geometry() -> None:
    """The emitted block is a plain-assignment variable block: every stated
    dim and the tolerance appear ONLY in a declaration (``name = value;``),
    never as a literal in a geometry expression. This is the data shape the
    .scad generator interpolates — the value is declared once, referenced by
    name, and never baked into an expression."""
    c = require_dimensions_confirmed(_full_chat(), None)
    block = c.scad_param_block()
    # Every line is a ``name = value;`` declaration (nothing else).
    lines = [ln for ln in block.splitlines() if ln.strip()]
    for ln in lines:
        assert re.match(r"^\w+\s*=\s*[^;]+;$", ln.strip()), ln
    # The stated dim values appear, and ONLY inside declarations.
    for value in ("42", "30", "20", "0.3"):
        assert value in block
        # They appear in the declaration lines, never as a bare argument.
    # There is NO geometry (no cube(), no translate(), no function call).
    assert not re.search(r"\w+\s*\(", block)


def test_scad_param_block_rejects_unconfirmed() -> None:
    c = require_dimensions_confirmed(_no_dims_chat(), None)
    assert c.confirmed is False
    with pytest.raises(ValueError):
        c.scad_param_block()


def test_emitted_block_is_directly_interpolable() -> None:
    """Prove the data shape is usable for named-param .scad emission: a
    generator that prepends the block and references names by identifier
    yields a .scad where every stated value lives only in a declaration
    and the geometry references the name, not the literal."""
    c = require_dimensions_confirmed(_full_chat(), None)
    block = c.scad_param_block()
    # A minimal .scad that uses the names, not the literals, for geometry.
    scad = block + "\ncube([W, D, H]);\ncylinder(h=1, r=(W + tolerance_mm) / 2);"

    # The geometry line references names, not literals.
    assert "cube([W, D, H]);" in scad
    # No stated-dim literal appears in the geometry (only in declarations).
    geometry = scad.split("\n")[len(block.splitlines()) :]
    geom_text = "\n".join(geometry)
    for value in ("42", "30", "20", "0.3"):
        assert value not in geom_text


# ---------------------------------------------------------------------------
# resolution_questions helper
# ---------------------------------------------------------------------------


def test_resolution_questions_missing_axes_and_fit() -> None:
    q = resolution_questions(missing_axes=("W", "H"), fit_type=None)
    assert len(q) == 2
    assert "W" in q[0] and "H" in q[0]
    assert "fit" in q[1].lower()


def test_resolution_questions_only_fit_when_dims_complete() -> None:
    q = resolution_questions(missing_axes=(), fit_type=None)
    assert len(q) == 1
    assert "fit" in q[0].lower()


def test_resolution_questions_empty_when_all_set() -> None:
    assert resolution_questions(missing_axes=(), fit_type="slip") == ()


# ---------------------------------------------------------------------------
# Persistence — alongside the transcript in d33d.db
# ---------------------------------------------------------------------------


@pytest.fixture
def conn() -> db.Connection:
    c = db.connect(":memory:")
    yield c
    c.close()


def test_put_and_get_dimensions_round_trips(conn: db.Connection) -> None:
    """The confirmed set is persisted with the transcript: params round
    trip as a dict, fit_type and tolerance_mm are denormalised columns."""
    pid = conn.create_project(name="bracket")
    c = require_dimensions_confirmed(_full_chat(), None)
    p = emit_named_params(c)

    conn.put_dimensions(
        project_id=pid,
        fit_type=c.fit_type,
        tolerance_mm=c.tolerance_mm,
        params={
            k: float(v)
            for k, v in p.items()
            if k in set(DIMENSION_AXES) | {"tolerance_mm"}
        },
    )

    row = conn.get_dimensions(pid)
    assert row is not None
    assert row["fit_type"] == "slip"
    assert row["tolerance_mm"] == c.tolerance_mm
    # params decoded back to a dict, W/D/H present.
    assert row["params"]["W"] == 42.0
    assert row["params"]["D"] == 30.0
    assert row["params"]["H"] == 20.0
    assert row["params"]["tolerance_mm"] == c.tolerance_mm


def test_get_dimensions_none_before_confirmation(conn: db.Connection) -> None:
    """No confirmed set yet (gate not passed) -> get_dimensions is None,
    so the #3 bbox gate has nothing to compare against (correct: it must
    not fabricate a dimension)."""
    pid = conn.create_project(name="empty")
    assert conn.get_dimensions(pid) is None


def test_put_dimensions_upserts(conn: db.Connection) -> None:
    """Re-confirming (e.g. user corrects a dim) replaces the row — one row
    per project (UNIQUE project_id), no version history (that's issue #7)."""
    pid = conn.create_project(name="bracket")
    conn.put_dimensions(
        project_id=pid, fit_type="slip", tolerance_mm=0.3, params={"W": 42.0}
    )
    conn.put_dimensions(
        project_id=pid, fit_type="press", tolerance_mm=-0.05, params={"W": 50.0}
    )
    row = conn.get_dimensions(pid)
    assert row is not None
    assert row["fit_type"] == "press"
    assert row["params"]["W"] == 50.0
    # Still exactly one row for the project.
    rows = conn.execute(
        "SELECT COUNT(*) AS n FROM project_dimensions WHERE project_id = ?",
        (pid,),
    ).fetchone()
    assert rows["n"] == 1


def test_dimensions_cascade_delete_with_project(conn: db.Connection) -> None:
    """Deleting the project removes its dimension record (FK ON DELETE
    CASCADE) — the record is the project's, not an independent version."""
    pid = conn.create_project(name="bracket")
    conn.put_dimensions(
        project_id=pid, fit_type="slip", tolerance_mm=0.3, params={"W": 42.0}
    )
    conn.delete_project(pid)
    assert conn.get_dimensions(pid) is None


def test_project_dimensions_table_exists(conn: db.Connection) -> None:
    """The table is part of the schema (created by connect())."""
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='project_dimensions'"
    ).fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# Invariant: the DimensionClarification dataclass is frozen
# ---------------------------------------------------------------------------


def test_clarification_is_frozen() -> None:
    c = require_dimensions_confirmed(_full_chat(), None)
    assert isinstance(c, DimensionClarification)
    with pytest.raises(AttributeError):
        c.confirmed = False  # type: ignore[misassigned-type]


# ---------------------------------------------------------------------------
# HIGH #4: confirmation heuristic tightened to the most-recent turn only
# ---------------------------------------------------------------------------


def test_stray_yes_in_early_turn_does_not_confirm_ai_suggestion() -> None:
    """(a) A stray "yes" in an early, unrelated turn must NOT confirm a
    later AI suggestion — only the most recent turn counts. With the last
    turn being a neutral one, the gate stays open."""
    chat = [
        "yes, that sounds interesting!",  # early stray confirmation
        "Make me a bracket from this photo.",  # last turn — no confirmation
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is False
    # No suggested dimensions were promoted to ground truth.
    assert c.params == {}


def test_yes_in_most_recent_turn_does_confirm_ai_suggestion() -> None:
    """(b) A "yes" in the actual most-recent responsive turn DOES confirm
    correctly (preserving the happy path)."""
    chat = [
        "Make me a bracket from this photo.",
        "Suggested W=40, D=30, H=20",
        "Yes, that's right, and it's a slip fit.",  # most recent — confirms
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is True
    assert c.stated_dims == (40.0, 30.0, 20.0)
    assert c.fit_type == "slip"


def test_stray_snap_in_early_turn_does_not_set_fit_type() -> None:
    """(c, stray) A "snap" mentioned in an early, unrelated turn (e.g. "that's
    a snap decision") must NOT flip the fit type. With the last turn neutral,
    no fit type is extracted → the gate asks for it."""
    chat = [
        "that's a snap decision, let's proceed",  # early stray snap
        "W: 42",  # last turn — no fit type
    ]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_snap_in_most_recent_turn_sets_fit_type() -> None:
    """(c, recent) A "snap" in the actual most-recent turn DOES set the fit
    type (preserving the happy path)."""
    chat = [
        "W: 42",
        "D: 30",
        "H: 20",
        "snap fit",  # most recent — sets fit_type
    ]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is True
    assert c.fit_type == "snap"
    assert c.tolerance_mm == resolve_tolerance_mm("snap")


# ---------------------------------------------------------------------------
# HIGH #5: confirmation heuristic must reject ambiguous/questioning turns
# ---------------------------------------------------------------------------


def test_questioning_yes_does_not_confirm_ai_suggestion() -> None:
    """The review's exact counter-example: "Yes, but why did you pick 40
    and not 50?" is a QUESTION about the suggestion, not an acceptance —
    it must NOT promote the AI pre-fill into ground truth."""
    chat = [
        "Suggested W=40, D=30, H=20",
        "Yes, but why did you pick 40 and not 50?",
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is False
    assert c.params == {}


def test_genuine_short_confirmations_still_confirm() -> None:
    """Happy path preserved: clean, short affirmative last turns confirm
    the AI pre-fill (the fit type stated_dims still closes the gate)."""
    for last in ("yes", "yes that's correct", "ok confirmed"):
        chat = ["Suggested W=40, D=30, H=20", last]
        c = require_dimensions_confirmed(
            chat,
            stated_dims={"fit_type": "slip"},
            ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
        )
        assert c.confirmed is True, last
        assert c.stated_dims == (40.0, 30.0, 20.0), last


def test_negation_with_yes_does_not_confirm_ai_suggestion() -> None:
    """A hedging/negating turn that also contains "yes" ("well yes and no,
    let's not use 40") must NOT confirm the pre-fill."""
    chat = [
        "Suggested W=40, D=30, H=20",
        "well yes and no, let's not use 40",
    ]
    c = require_dimensions_confirmed(
        chat,
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0},
    )
    assert c.confirmed is False
    assert c.params == {}


def test_questioning_snap_does_not_set_fit_type() -> None:
    """A questioning/negating last turn mentioning "snap" must NOT flip the
    fit type to snap ("that's a snap decision, why would you choose snap
    fit?")."""
    chat = [
        "W: 42",
        "D: 30",
        "H: 20",
        "that's a snap decision, why would you choose snap fit?",
    ]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_negating_snap_does_not_set_fit_type() -> None:
    """A negating last turn mentioning "snap" ("no, not a snap fit") must
    NOT set the fit type."""
    chat = [
        "W: 42",
        "D: 30",
        "H: 20",
        "no, not a snap fit",
    ]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_clean_short_snap_still_sets_fit_type() -> None:
    """Happy path preserved: a clean, short affirmative last turn with a
    fit keyword ("yes, snap fit") still sets the fit type."""
    chat = [
        "W: 42",
        "D: 30",
        "H: 20",
        "yes, snap fit",
    ]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is True
    assert c.fit_type == "snap"
    assert c.tolerance_mm == resolve_tolerance_mm("snap")


# ---------------------------------------------------------------------------
# Item 1: the no_fit fit type is reachable via natural language
#
# The generic negation-word set contains "no", which disqualified the valid
# fit-type name "no_fit" — a user typing "no fit" / "no-fit" / "nofit" could
# never select the no-fit fit type. The no-fit phrase match takes precedence
# over the negation disqualifier, so "no fit" (with stated dims) yields a
# confirmed clarification with fit_type "no_fit".
# ---------------------------------------------------------------------------


def test_no_fit_phrase_in_last_turn_sets_no_fit_fit_type() -> None:
    """A last turn of "no fit" / "no-fit" / "nofit" with all dims stated
    yields a confirmed clarification with fit_type "no_fit" (tolerance 0.0),
    even though the generic negation set contains "no"."""
    for last in ("no fit", "no-fit", "nofit"):
        chat = ["W: 42", "D: 30", "H: 20", last]
        c = require_dimensions_confirmed(chat, None)
        assert c.confirmed is True, last
        assert c.fit_type == "no_fit", last
        assert c.tolerance_mm == 0.0, last


def test_bare_no_still_disqualifies_fit_type() -> None:
    """The no_fit phrase branch is a phrase match only — a bare "no" is a
    negation, not a fit-type answer, and must not set any fit type."""
    chat = ["W: 42", "D: 30", "H: 20", "no"]
    c = require_dimensions_confirmed(chat, None)
    assert c.confirmed is False
    assert any("fit" in q.lower() for q in c.questions)


def test_no_fit_turn_does_not_confirm_ai_suggestion() -> None:
    """The no_fit phrase branch lives only in the fit-type path: a "no fit"
    turn must NOT register as a dimension-suggestion confirmation (the base
    affirmative path still rejects it as negation), so the ai_suggested
    pre-fill stays unconfirmed. The gate can still close — but only via the
    user's chat-stated dims + the no_fit fit type, never via the pre-fill
    (the suggested surfacing is untouched)."""
    chat = [
        "W: 42",
        "D: 30",
        "H: 20",
        "no fit",
    ]
    c = require_dimensions_confirmed(
        chat, stated_dims=None, ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0}
    )
    # Ground truth is the user's stated 42/30/20, not the pre-filled 40/30/20.
    assert c.stated_dims == (42.0, 30.0, 20.0)
    assert c.fit_type == "no_fit"
    # The suggestion itself was never promoted to ground truth (no "suggested"
    # token in the last turn) — it is only surfaced as a suggestion.
    assert c.suggested == {"W": 40.0, "D": 30.0, "H": 20.0}


def test_no_fit_turn_alone_keeps_gate_closed() -> None:
    """A "no fit" turn with no stated dims anywhere: the suggestion pre-fill
    is not confirmed (base path rejects it as negation), so the gate stays
    open on the dimensions even though the fit type resolves to no_fit."""
    chat = [
        "Make me a bracket from this photo.",
        "no fit",
    ]
    c = require_dimensions_confirmed(
        chat, stated_dims=None, ai_suggested={"W": 40.0, "D": 30.0, "H": 20.0}
    )
    assert c.confirmed is False
    assert c.params == {}
    # The fit type WAS resolved (that's the whole point of the phrase branch),
    # so no fit-type question is asked — only the dimension question.
    assert not any("fit type" in q.lower() for q in c.questions)
    assert any("H" in q for q in c.questions)


def test_no_fit_phrase_must_be_tight_form() -> None:
    """Only the tight no-fit / no-fit / nofit phrase form clears the
    negation gate for fit-type purposes — unrelated phrases containing
    "no" ("no, that fit is wrong") must not set a fit type."""
    for last in ("no, that fit is wrong", "I don't need no fit"):
        chat = ["W: 42", "D: 30", "H: 20", last]
        c = require_dimensions_confirmed(chat, None)
        assert c.confirmed is False, last
        assert any("fit" in q.lower() for q in c.questions), last


def test_stated_dims_fit_type_no_fit_still_works() -> None:
    """The existing stated_dims structured path for no_fit is unaffected."""
    c = require_dimensions_confirmed(
        ["W: 42", "D: 30", "H: 20"], stated_dims={"fit_type": "no_fit"}
    )
    assert c.confirmed is True
    assert c.fit_type == "no_fit"


# ---------------------------------------------------------------------------
# Item 2: malformed ai_suggested pre-fills degrade, never crash
#
# The suggested-surfacing comprehension runs values through the same
# _coerce helper as _extract_stated, so an LLM-derived malformed pre-fill
# ({"W": "x"} / {"W": None}) degrades to a missing suggested entry instead
# of raising ValueError/TypeError.
# ---------------------------------------------------------------------------


def test_malformed_ai_suggested_does_not_raise() -> None:
    """Non-numeric ai_suggested values degrade to a missing suggested entry
    instead of raising (the parallel _extract_stated path already did this).
    Valid numeric pre-fills (ints, numeric strings) still surface."""
    c = require_dimensions_confirmed(
        ["Make me a bracket from this photo."],
        stated_dims=None,
        ai_suggested={"W": "x", "D": 30.0, "H": None},
    )
    assert c.confirmed is False
    # Only the valid numeric pre-fill surfaces; W and H are absent.
    assert c.suggested == {"D": 30.0}


def test_numeric_string_ai_suggested_surfaces() -> None:
    """Numeric strings coerce like the _extract_stated path does."""
    c = require_dimensions_confirmed(
        ["Make me a bracket from this photo."],
        stated_dims=None,
        ai_suggested={"W": 40.0, "D": "30", "H": 20},
    )
    assert c.suggested == {"W": 40.0, "D": 30.0, "H": 20.0}


# ---------------------------------------------------------------------------
# Issue #261 (task-b): the carry-forward merge helper and the tier-2
# user-quoted helper (the two pure functions the three call sites share)
# ---------------------------------------------------------------------------


class TestEffectiveStatedDims:
    """effective_stated_dims — the carry-forward merge (issue #261's
    operator decision: ONE function, THREE call sites)."""

    def test_cueless_carries_forward(self):
        """No cues (the region-edit call site) → the carried set comes
        back unchanged (a fresh project → {})."""
        assert effective_stated_dims({"H": 12.0}) == {"H": 12.0}
        assert effective_stated_dims({"W": 12.0, "D": 8.0, "H": 5.0}) == {
            "W": 12.0,
            "D": 8.0,
            "H": 5.0,
        }
        assert effective_stated_dims(None) == {}
        assert effective_stated_dims({"H": 12.0}, None) == {"H": 12.0}

    def test_relative_cue_releases_its_axis_only(self):
        """make it taller (relative H) on {H: 12, W: 30} → W carries,
        H is released (never rendered stated)."""
        from d33d.axis_lexicon import classify

        cues = classify("make it taller")
        assert effective_stated_dims({"H": 12.0, "W": 30.0}, cues) == {"W": 30.0}

    def test_global_cue_releases_all_axes(self):
        """make it bigger (global) on a full set → {} (the gate
        abstains entirely)."""
        from d33d.axis_lexicon import classify

        cues = classify("make it bigger")
        assert effective_stated_dims(
            {"W": 12.0, "D": 8.0, "H": 5.0}, cues
        ) == {}

    def test_absolute_cue_overrides_carried_value(self):
        """H: 20 (explicit) on {H: 12} → {H: 20} (the cue OVERRIDES
        the carried value — precedence)."""
        assert effective_stated_dims({"H": 12.0}, {"H": 20.0}) == {"H": 20.0}

    def test_absolute_cue_adds_uncarried_axis(self):
        """make it 12 mm tall (lexicon absolute H) on a fresh project →
        {H: 12} (the cue sets the axis)."""
        from d33d.axis_lexicon import classify

        cues = classify("make it 12 mm tall")
        assert effective_stated_dims(None, cues) == {"H": 12.0}

    def test_lexicon_absolute_and_relative_compose(self):
        """make it 12 mm tall, 40 mm wide — the absolute cues SET H
        and W; uncued D is absent (fresh project)."""
        from d33d.axis_lexicon import classify

        cues = classify("make it 12 mm tall, 40 mm wide")
        assert effective_stated_dims(None, cues) == {"H": 12.0, "W": 40.0}

    def test_explicit_set_overrides_and_carries(self):
        """A caller's explicit set (the body field / protocol extraction)
        is an ABSOLUTE OVERRIDING statement with no release semantics:
        {W: 10} on {H: 12} → {W: 10, H: 12} (the cue sets W, the
        uncued H carries forward — the explicit set does not release
        axes it does not name)."""
        assert effective_stated_dims({"H": 12.0}, {"W": 10.0}) == {
            "H": 12.0,
            "W": 10.0,
        }

    def test_non_positive_and_bad_values_are_dropped(self):
        """A carried 0.0 / negative axis (the unconfirmed marker) is not
        carried (the axes_to_gate_triple cleaning rule)."""
        assert effective_stated_dims({"H": 0.0, "W": -5.0, "D": 8.0}) == {
            "D": 8.0
        }

    def test_cues_with_no_absolute_no_relative_no_global(self):
        """A lexicon classification of a cueless message (add a hole)
        carries the set forward unchanged."""
        from d33d.axis_lexicon import classify

        cues = classify("add a hole")
        assert effective_stated_dims({"H": 12.0}, cues) == {"H": 12.0}

    def test_missed_relative_cue_word_releases_axis(self):
        """The #247 regression class re-entering through the vocabulary
        the closed set does not cover: 'increase the height' carries no
        lexicon word, yet against a carried {H: 12} the gate must NOT
        enforce the old H — the round-2 release fallback emits a relative
        H cue so the carried axis is released, not enforced."""
        from d33d.axis_lexicon import classify

        cues = classify("increase the height")
        assert effective_stated_dims({"H": 12.0, "W": 30.0}, cues) == {"W": 30.0}

    def test_foreign_unit_message_carries_forward(self):
        """'make it 5 cm tall' abstains (no cm/in conversion) — the
        carried H is neither overridden with the 10×-wrong 5.0 nor
        released (no relative cue): the gate keeps the carried value,
        which is the honest outcome for a statement the system cannot
        measure."""
        from d33d.axis_lexicon import classify

        cues = classify("make it 5 cm tall")
        assert effective_stated_dims({"H": 12.0, "W": 30.0}, cues) == {
            "H": 12.0,
            "W": 30.0,
        }

    def test_percent_relative_releases(self):
        """'make it 20% taller' releases H (the 20 is a percentage, not
        a mm value — it never becomes H=20)."""
        from d33d.axis_lexicon import classify

        cues = classify("make it 20% taller")
        assert effective_stated_dims({"H": 12.0, "W": 30.0}, cues) == {"W": 30.0}


class TestUserQuotedUnmappedMm:
    """user_quoted_unmapped_mm — the tier-2 helper (issue #261):
    the full-history scan of explicit-mm numbers no axis was ever
    assigned to."""

    def test_unmapped_lexicon_and_protocol_cues(self):
        """a 20 mm wide thing, lift it 12 mm → {12.0} (20 is mapped
        to W by the lexicon; 12's clause holds no axis word)."""
        assert user_quoted_unmapped_mm(
            ["a 20 mm wide thing, lift it 12 mm"]
        ) == {12.0}

    def test_mapped_numbers_are_excluded(self):
        """12 mm tall, 20 mm wide → {} (both mapped)."""
        assert user_quoted_unmapped_mm(["12 mm tall, 20 mm wide"]) == set()

    def test_bare_numbers_never_count(self):
        """A bare number (no mm unit) is never eligible."""
        assert user_quoted_unmapped_mm(["spacer_height 12"]) == set()

    def test_no_unit_conversion(self):
        """cm/in numbers are not mm numbers — never eligible."""
        assert user_quoted_unmapped_mm(["1.5 cm wide"]) == set()

    def test_unions_across_full_history(self):
        """The scan covers ALL user messages (not just this turn): a
        number from an earlier message is eligible on a later turn."""
        assert user_quoted_unmapped_mm(
            ["make a 15 mm shelf", "add a fillet"]
        ) == {15.0}

    def test_protocol_cue_consumed_number_is_mapped(self):
        """W: 42 mm — the number an explicit protocol cue consumed is
        mapped (never eligible), even though the lexicon has no W.
        (The axis-prefixed form is the protocol's cue, not the
        lexicon's.)"""
        assert user_quoted_unmapped_mm(["W: 42 mm"]) == set()

    def test_empty_history_is_empty(self):
        assert user_quoted_unmapped_mm([]) == set()
        assert user_quoted_unmapped_mm(("",)) == set()

    def test_bare_axis_letter_followed_by_number_not_consumed(self):
        """'H is the axis you want, 12 mm' — the axis letter with NO
        ``:``/``=`` marker and NO mm unit immediately after it does NOT
        consume the 12 (issue #261 round 2: the old ``[:=]?`` + optional
        ``mm`` shape let a stray letter followed by any number suppress a
        tier-2 offer — an offer miss, not a wrong offer)."""
        assert user_quoted_unmapped_mm(["H is the axis you want, 12 mm"]) == {12.0}

    def test_marker_form_still_consumes(self):
        """'H = 20 mm' and 'W: 42' — the ``:``/``=`` marker forms
        consume the number (unchanged behavior)."""
        assert user_quoted_unmapped_mm(["H = 20 mm"]) == set()
        assert user_quoted_unmapped_mm(["W: 42"]) == set()

    def test_axis_letter_no_marker_not_consumed(self):
        """'H 20 mm' (no ``:``/``=`` marker) — the tightened
        ``_mm_cue_values`` regex does NOT match this shape (issue #261
        round 2: a bare axis letter followed by a number is ambiguous —
        "H is the axis you want, 12 mm somewhere" would otherwise mark
        the 12 as consumed). The ``_extract_stated`` pass still reads
        this form for the gate, but the offer helper errs on the side of
        offering (a missed offer is cheaper than a wrong one)."""
        assert user_quoted_unmapped_mm(["H 20 mm"]) == {20.0}

    def test_history_scan_is_capped_at_last_50(self):
        """The tier-2 scan covers at most the LAST
        ``QUOTED_UNMAPPED_MAX_MESSAGES`` (50) user messages: a number
        quoted in an older message does not make it eligible."""
        from d33d.dimension_protocol import QUOTED_UNMAPPED_MAX_MESSAGES

        assert QUOTED_UNMAPPED_MAX_MESSAGES == 50
        old = "a 12 mm spacer"
        recent = [f"filler message {i}" for i in range(50)]
        # The old message sits OUTSIDE the last-50 window → not eligible.
        assert user_quoted_unmapped_mm([old, *recent]) == set()
        # The same message INSIDE the window → eligible.
        assert user_quoted_unmapped_mm([*recent, old]) == {12.0}


class TestOfferTierSignals:
    """offer_tier_signals — the ONE offer-signal helper (issue #261 fix
    batch): (released_axes, quoted_mm) for one turn, computed the same
    way on the chat and the finalize seams (tier 2 sees the chat
    history, not just the finalize message)."""

    def test_tier1_released_axes(self):
        """A relative cue on the current message releases its axis."""
        released, quoted = offer_tier_signals("make it taller")
        assert released == {"H"}
        assert quoted == set()

    def test_tier2_quoted_from_history(self):
        """An unmapped number quoted in an EARLIER message (the chat
        history) is eligible — this is what makes tier 2 behave the
        same on finalize as on chat."""
        released, quoted = offer_tier_signals(
            "finalize the part", ["a spacer to lift a shelf 12 mm"]
        )
        assert released is None
        assert quoted == {12.0}

    def test_tier2_mapped_number_not_eligible(self):
        """A number the lexicon mapped to an axis in the history is not
        eligible (the 20 in "a 20 mm wide thing" is mapped to W)."""
        released, quoted = offer_tier_signals(
            "finalize", ["a 20 mm wide thing, lift it 12 mm"]
        )
        assert quoted == {12.0}

    def test_global_cue_releases_all(self):
        released, quoted = offer_tier_signals("make it bigger")
        assert released == {"W", "D", "H"}
        assert quoted == set()

    def test_no_cues(self):
        released, quoted = offer_tier_signals("make a part")
        assert released is None
        assert quoted == set()


class TestTripleExtraction:
    """W×D×H triple extraction via ``stated_axes_from_message`` / ``stated_dims_from_message``
    (issue #275 task-b). The triple is matched on the raw message by
    ``_extract_triple`` and integrated into ``_extract_stated`` with
    precedence: axis-letter cues > triple > cube shorthand > lexicon.
    """

    def test_three_numbers_triple(self):
        """'60 × 45 × 80 mm' → W 60, D 45, H 80."""
        axes = stated_axes_from_message("60 × 45 × 80 mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 80.0}

    def test_no_space_no_unit(self):
        """'60x45x80mm' → W 60, D 45, H 80."""
        axes = stated_axes_from_message("60x45x80mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 80.0}

    def test_unit_after_each_number(self):
        """'60mm x 45mm x 20mm' → W 60, D 45, H 20."""
        axes = stated_axes_from_message("60mm x 45mm x 20mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_two_numbers_wd_only(self):
        """'60 × 45 mm' → W 60, D 45 (no H)."""
        axes = stated_axes_from_message("60 × 45 mm")
        assert axes == {"W": 60.0, "D": 45.0}
        assert "H" not in axes

    def test_no_unit_triple(self):
        """'60×45×20' → W 60, D 45, H 20 (no unit: states)."""
        axes = stated_axes_from_message("60×45×20")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_foreign_unit_triple_abstains(self):
        """'2 × 2 inch' → nothing (foreign unit)."""
        axes = stated_axes_from_message("2 × 2 inch")
        assert axes == {}

    def test_foreign_unit_triple_cm(self):
        """'6 x 4 cm' → nothing (foreign unit)."""
        axes = stated_axes_from_message("6 x 4 cm")
        assert axes == {}

    def test_feature_noun_after_window_suppresses(self):
        """'a 10 × 10 mm hole' → nothing (feature noun in after-window),
        with [10, 10] unmapped."""
        axes = stated_axes_from_message("a 10 × 10 mm hole")
        assert axes == {}
        # The 10s are unmapped (not consumed by any axis).
        from d33d.axis_lexicon import classify

        cues = classify("a 10 × 10 mm hole")
        assert 10.0 in cues.unmapped_mm_numbers

    def test_tray_with_flared_lip_states(self):
        """'a tray 60 × 45 × 20 mm with a flared lip around the top' →
        W 60, D 45, H 20 (after-window stops at 'with'; 'lip' is outside)."""
        axes = stated_axes_from_message(
            "a tray 60 × 45 × 20 mm with a flared lip around the top"
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_two_triples_first_suppressed_second_states(self):
        """'a 10 × 10 mm hole in a 60 × 45 × 20 mm tray' → first triple
        suppressed (hole in after-window), second states W 60, D 45, H 20."""
        axes = stated_axes_from_message("a 10 × 10 mm hole in a 60 × 45 × 20 mm tray")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_triple_with_10mm_hole(self):
        """'a 60 x 45 x 20 mm tray with a 10 mm hole' → W 60, D 45, H 20,
        with [10] unmapped."""
        axes = stated_axes_from_message("a 60 x 45 x 20 mm tray with a 10 mm hole")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}
        from d33d.axis_lexicon import classify

        cues = classify("a 60 x 45 x 20 mm tray with a 10 mm hole")
        assert 10.0 in cues.unmapped_mm_numbers

    def test_triple_beats_lexicon_for_same_axis(self):
        """'a 60 × 45 × 20 mm tray 40 mm wide' → W 60, D 45, H 20, with 40
        unmapped (the triple overrides the lexicon's W=40)."""
        axes = stated_axes_from_message("a 60 × 45 × 20 mm tray 40 mm wide")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}
        # The 40 is unmapped in the tier-2 offer sense: the triple
        # assigns W=60, overriding the lexicon's W=40, so 40 is
        # eligible for the offer.
        assert user_quoted_unmapped_mm(
            ["a 60 × 45 × 20 mm tray 40 mm wide"]
        ) == {40.0}

    def test_axis_letter_beats_triple(self):
        """'H 30, 60 × 45 × 20 mm' → W 60, D 45, H 30 (axis letter wins
        for H; triple fills W and D)."""
        axes = stated_axes_from_message("H 30, 60 × 45 × 20 mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 30.0}

    def test_stated_dims_full_triple(self):
        """stated_dims_from_message returns the full (W, D, H) tuple for
        a complete triple."""
        dims = stated_dims_from_message("60 × 45 × 80 mm")
        assert dims == (60.0, 45.0, 80.0)

    def test_stated_dims_partial_triple_returns_none(self):
        """A two-number triple (W, D only) → stated_dims_from_message
        returns None (not a full triple)."""
        dims = stated_dims_from_message("60 × 45 mm")
        assert dims is None

    def test_triple_numbers_excluded_from_unmapped(self):
        """Triple-consumed numbers are excluded from
        ``user_quoted_unmapped_mm`` (the tier-2 offer scan). The lexicon's
        own ``unmapped_mm_numbers`` does not know about triples (that's
        the ``dimension_protocol`` helper's job); the tier-2 scan does.
        """
        assert user_quoted_unmapped_mm(["60 × 45 × 80 mm"]) == set()

    def test_triple_numbers_excluded_from_unmapped_no_space(self):
        """'60x45x80mm' — no-space form: numbers are triple-consumed
        and excluded from the tier-2 offer scan."""
        assert user_quoted_unmapped_mm(["60x45x80mm"]) == set()
