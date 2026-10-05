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
    offer_tier_signals,
    require_dimensions_confirmed,
    resolution_questions,
    resolve_stated_cues,
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
        _, quoted = offer_tier_signals(
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


class TestNewestWinsPerAxis:
    """Issue #369: per axis, the NEWEST explicit stated value wins; a
    relative word in a newer message releases that axis."""

    def test_later_value_overrides_earlier(self):
        """'make it 20 mm tall' after 'a 40mm wide box, 12mm tall' → W40, H20."""
        axes = stated_axes_from_message(
            "make it 20 mm tall", ["a 40mm wide box, 12mm tall"]
        )
        assert axes == {"W": 40.0, "H": 20.0}

    def test_relative_releases_axis(self):
        """'make it taller' after 'a 40mm wide box, 12mm tall' + 'make it
        20 mm tall' → W40, H released (H absent)."""
        axes = stated_axes_from_message(
            "make it taller",
            ["a 40mm wide box, 12mm tall", "make it 20 mm tall"],
        )
        assert axes == {"W": 40.0}
        assert "H" not in axes

    def test_relative_releases_w(self):
        """'make it wider' → W released; H carried from the 20 mm explicit."""
        axes = stated_axes_from_message(
            "make it wider",
            ["a 40mm wide box, 12mm tall", "make it 20 mm tall", "make it taller"],
        )
        assert "W" not in axes

    def test_two_explicit_values_newest_wins(self):
        """Two explicit H values in different messages → the newest wins."""
        axes = stated_axes_from_message("H: 20", ["H: 12"])
        assert axes == {"H": 20.0}

    def test_same_message_explicit_beats_relative(self):
        """'H: 20, make it taller' → H=20 (explicit axis-letter wins over
        the relative word in the same message)."""
        axes = stated_axes_from_message(
            "H: 20, make it taller", ["a 40mm wide box, 12mm tall"]
        )
        assert axes["H"] == 20.0

class TestRelativeRelease:
    """Issue #369: a relative word releases the axis it names — with the
    per-turn guards (feature clauses, unmapped numbers) that decide
    release vs restate."""

    def test_single_unmapped_number_restates_released_axis(self):
        """'make it taller, 20 mm' → H=20: the message carries EXACTLY ONE
        unmapped mm number and its clause has no feature noun, so the number
        is the released axis's own new value — set, not release."""
        axes = stated_axes_from_message(
            "make it taller, 20 mm", ["a 40mm wide box, 12mm tall"]
        )
        assert axes["H"] == 20.0

    def test_relative_word_in_feature_clause_releases_only(self):
        """'make the lid taller, 20 mm' → H released: the relative word
        ('taller') sits in a clause with a feature noun ('lid'), so the
        releasing turn releases ONLY — an unmapped number in ANOTHER clause
        of the same turn can never restate the part's axis (the 20 mm
        belongs to the feature, not the part)."""
        axes = stated_axes_from_message(
            "make the lid taller, 20 mm", ["a 40mm wide box, 12mm tall"]
        )
        assert "H" not in axes
        assert axes["W"] == 40.0

    def test_feature_clause_unmapped_number_releases_axis(self):
        """'make it taller, keep the 25 mm peg' → H released: the sole
        unmapped number sits in a feature-noun clause ('peg'), so it belongs
        to the feature, not the part — never enforce a stray feature number."""
        axes = stated_axes_from_message(
            "make it taller, keep the 25 mm peg", ["a 40mm wide box, 12mm tall"]
        )
        assert "H" not in axes

    def test_two_unmapped_numbers_release_axis(self):
        """'make it taller, 20 mm, and the hole 5 mm' → H released: two
        unmapped numbers are ambiguous about which restates the axis."""
        axes = stated_axes_from_message(
            "make it taller, 20 mm, and the hole 5 mm",
            ["a 40mm wide box, 12mm tall"],
        )
        assert "H" not in axes

    def test_relative_delta_releases_not_enforces(self):
        """A RELATIVE delta ('by N mm' / 'N mm <axis-word>') is an
        increment, never an absolute target: the axis is released (the
        gate asks for the new value) instead of enforcing the delta as an
        absolute. On a 12 mm part, 'taller by 5 mm' must NOT yield
        H=5.0 — a physically shorter target."""
        for msg in (
            "taller by 5 mm",
            "make it taller by 5 mm",
            "5 mm taller",
            "make it wider by 3 mm",
            "wider by 3 mm",
            "3 mm wider",
            "make it 3 mm wider",
            "deeper by 2 mm",
            "2 mm deeper",
            "make it 2 mm deeper",
        ):
            axes = stated_axes_from_message(msg, ["a 40mm wide box, 12mm tall"])
            target = "H" if "tall" in msg or "short" in msg else "W" if "wide" in msg or "narrow" in msg else "D"
            assert target not in axes, (
                f"{msg!r} is a relative delta — {target} must be released, got {axes}"
            )

class TestAnchoredDeltaMarkers:
    """Issue #369 round 2: delta markers are anchored to their own number
    — a marker on a different number never suppresses the other number's
    absolute statement."""

    def test_delta_marker_does_not_block_other_axes(self):
        """A delta message releases only its own axis: 'make it wider by
        3 mm' releases W while H=12 stays enforced (the delta marker is
        clause-local to the delta's own clause)."""
        axes = stated_axes_from_message(
            "make it wider by 3 mm", ["a 40mm wide box, 12mm tall"]
        )
        assert "W" not in axes
        assert axes["H"] == 12.0

    def test_nonpositive_unmapped_value_never_stated(self):
        """A non-positive explicit value ("0 mm") is never stated — the
        released axis is released, and the gate abstains."""
        for msg in ("make it taller, 0 mm", "make it taller, -5 mm"):
            axes = stated_axes_from_message(msg, ["a 40mm wide box, 12mm tall"])
            assert "H" not in axes

    def test_older_relative_does_not_consume_axis(self):
        """A relative word in an OLDER message must not 'consume' the axis
        such that a NEWER explicit value is lost."""
        axes = stated_axes_from_message(
            "make it shorter",
            ["make it taller", "a 40mm wide box, 12mm tall"],
        )
        assert "H" not in axes  # released by the newer relative word
        assert "W" in axes

    def test_empty_history_message_alone_sets_axes_not_history(self):
        """EMPTY-history extraction (the chat route's deliberate empty-history
        call — rationale in ``chat_loop``'s module comment) reads the message
        alone; a relative-only message extracts nothing."""
        assert stated_axes_from_message("a 40mm wide box", []) == {"W": 40.0}
        # A relative-only message states no axis on its own:
        assert stated_axes_from_message("make it taller", []) == {}

    def test_empty_history_release_drops_carried_axis(self):
        """The end-to-end chat-route release: message alone (empty history)
        states no axis → the lexicon classification releases the carried
        H, and the merge drops it."""
        from d33d.axis_lexicon import classify

        carried = {"W": 40.0, "H": 12.0}
        extracted = stated_axes_from_message("make it taller", [])
        cues = extracted if extracted else classify("make it taller")
        assert effective_stated_dims(carried, cues) == {"W": 40.0}

class TestWindowBoundary:
    """Issue #369: the extraction window is the last 50 prior turns plus
    the current message — the boundary is exact and the current message
    is never dropped."""

    def test_history_window_boundary_is_exact(self):
        """Window semantics, pinned to the EXACT boundary: the window is
        the LAST ``QUOTED_UNMAPPED_MAX_MESSAGES`` (50) prior-history
        turns, mirroring ``user_quoted_unmapped_mm`` (called with the
        prior turns alone — ``list(prior_turns)[-50:]``), plus the
        current message (appended last, index ``len(prior)`` — always in
        the window). An off-by-one in EITHER direction fails this test:
        ``window_start`` too large (e.g. ``len(history) + 1 - 50``)
        drops the boundary turn (idx ``len(prior) - 50``) that the
        aligned rule keeps; too small (e.g. ``len(history) - 51``) keeps
        the turn (idx ``len(prior) - 51``) the aligned rule drops."""
        from d33d.dimension_protocol import QUOTED_UNMAPPED_MAX_MESSAGES

        k = QUOTED_UNMAPPED_MAX_MESSAGES
        # BOUNDARY-INSIDE: ``k - 1`` prior turns → N = k, ``window_start``
        # = 0, so idx 0 (the statement) is the OLDEST turn inside the
        # window → stated:
        inside = ["W: 40"] + [f"filler {i}" for i in range(k - 2)]
        assert len(inside) == k - 1
        axes = stated_axes_from_message("filler X", inside)
        assert axes["W"] == 40.0
        # BOUNDARY-OUTSIDE: one more filler (``k`` prior) → N = k + 1,
        # ``window_start`` = 1; the statement at idx 0 falls one turn out
        # of the window → not stated:
        outside = [*inside, f"filler {k - 2}"]
        assert len(outside) == k
        assert outside[0] == "W: 40"
        axes = stated_axes_from_message("filler X", outside)
        assert "W" not in axes

    def test_window_always_includes_current_message(self):
        """The current message (the newest element, appended last by the
        wrappers) is ALWAYS inside the window — with 51+ prior turns
        its own statement is still scanned. True under both window
        arithmetics (it is never the element the window drops) — this
        pins the invariant, not an off-by-one."""
        from d33d.dimension_protocol import QUOTED_UNMAPPED_MAX_MESSAGES

        filler = [f"filler {i}" for i in range(QUOTED_UNMAPPED_MAX_MESSAGES + 10)]
        axes = stated_axes_from_message("a 40 mm wide box, 20 mm tall", filler)
        assert axes["W"] == 40.0
        assert axes["H"] == 20.0

    def test_window_old_statement_outside_newest_wins(self):
        """An old "H: 12" OUTSIDE the window never wins over the current
        message's "make it 20 mm tall": with 51+ prior turns the current
        message is the newest statement (scanned), and the out-of-window
        "H: 12" is the only other H statement — dropped, so it cannot
        override the current one."""
        from d33d.dimension_protocol import QUOTED_UNMAPPED_MAX_MESSAGES

        filler = [f"filler {i}" for i in range(QUOTED_UNMAPPED_MAX_MESSAGES + 10)]
        filler[5] = "H: 12"
        axes = stated_axes_from_message("make it 20 mm tall", filler)
        assert axes["H"] == 20.0


class TestAnchoredDeltaNewestWins:
    """Issue #369: per axis, the NEWEST explicit stated value wins — the
    anchored-delta discriminator that the round-2 anchoring exists for."""

    def test_unanchored_delta_marker_on_other_number_does_not_release(self):
        """"make it taller by 5 mm, 30 mm" → H=30: the delta marker ("by")
        binds to 5, not 30 — the 30 is an unmapped number with no anchored
        delta marker in its clause, so it restates the released axis."""
        axes = stated_axes_from_message(
            "make it taller by 5 mm, 30 mm",
            ["a 40mm wide box, 12mm tall"],
        )
        assert axes["H"] == 30.0
        assert axes["W"] == 40.0

    def test_same_clause_delta_marker_on_other_number_keeps_absolute(self):
        """DISCRIMINATOR (issue #369 round 2): same clause (no comma) —
        "make it taller by 5 mm and 30 mm". The preposition "by" sits in
        the SAME clause as the unmapped 30. The anchoring excludes 5
        (an anchored delta) from the ambiguity count, and the clause-local
        marker check for 30 must be anchored to 30 itself: with the
        unanchored pattern ("\\bby\\s+(?=\\d)") the "by" of "by 5 mm"
        matches inside 30's own clause and releases H; anchored, the 30
        restates the released axis (H=30). Fails when "_delta_marker_for"
        is reverted to the unanchored form (proven in the PR review:
        revert → this test fails with "H" absent → restore → passes)."""
        axes = stated_axes_from_message(
            "make it taller by 5 mm and 30 mm",
            ["a 40mm wide box, 12mm tall"],
        )
        assert axes["H"] == 30.0
        assert axes["W"] == 40.0

    def test_taller_by_5mm_still_releases(self):
        """"taller by 5 mm" → H released: the delta marker IS anchored to
        the sole unmapped number (5), so the axis is released."""
        axes = stated_axes_from_message(
            "taller by 5 mm", ["a 40mm wide box, 12mm tall"]
        )
        assert "H" not in axes

    def test_make_it_5mm_taller_still_releases(self):
        """"make it 5 mm taller" → H released: the delta marker IS anchored
        to the sole unmapped number (5), so the axis is released."""
        axes = stated_axes_from_message(
            "make it 5 mm taller", ["a 40mm wide box, 12mm tall"]
        )
        assert "H" not in axes


class TestResolveStatedCues:
    """Issue #369 round 2: the ONE helper that does try /
    stated_axes_from_message / classify fallback / effective_stated_dims
    with the degrade-and-warn behaviour."""

    def test_sets_axis_from_message(self):
        """A message that states an axis sets it in the result."""
        result = resolve_stated_cues(
            {"W": 40.0}, "make it 12 mm tall", label="test"
        )
        assert result["H"] == 12.0
        assert result["W"] == 40.0

    def test_releases_axis_via_lexicon_fallback(self):
        """A relative-only message ("make it taller") states nothing on
        its own → falls back to the lexicon's Cues → releases the carried
        H."""
        result = resolve_stated_cues(
            {"W": 40.0, "H": 12.0}, "make it taller", label="test"
        )
        assert "H" not in result
        assert result["W"] == 40.0

    def test_degrades_on_classify_failure(self, monkeypatch):
        """A classify failure degrades to the carried set unchanged."""
        import d33d.dimension_protocol as dp

        def _boom(message: str):
            raise RuntimeError("lexicon on fire")

        monkeypatch.setattr(dp, "classify", _boom)
        result = resolve_stated_cues(
            {"W": 40.0, "H": 12.0}, "make it taller", label="test"
        )
        assert result == {"W": 40.0, "H": 12.0}


class TestGateReleasesAxes:
    """Issue #369: ``require_dimensions_confirmed``'s gate input runs the
    same release pass — a newer relative word releases the axis the gate
    would otherwise enforce (conservative: ask, never enforce a stale
    value the user just disputed)."""

    def test_gate_released_axis_stays_open(self):
        """'a 40mm wide box, 12mm tall' + a slip fit, then 'make it taller'
        → NOT confirmed; W stays stated, H is released (the gate asks
        about H instead of enforcing the stale 12)."""
        c = require_dimensions_confirmed(
            ["a 40mm wide box, 12mm tall", "it's a slip fit", "make it taller"],
            None,
        )
        assert c.confirmed is False
        assert any("H" in q for q in c.questions)

    def test_gate_unchanged_without_release(self):
        """The same conversation without the release confirms W/D/H —
        the release is what keeps the gate open."""
        c = require_dimensions_confirmed(
            [
                "a 40mm wide box, 12mm tall",
                "it's 30mm deep",
                "it's a slip fit",
            ],
            None,
        )
        assert c.confirmed is True
        assert c.params["H"] == 12.0


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

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # An explicit mm unit on the match wins over a following
            # "in" (the preposition, not the inch unit):
            pytest.param(
                "60 x 45 mm in the drawer",
                {"W": 60.0, "D": 45.0},
                id="mm-unit-beats-following-in-preposition",
            ),
            # A second unit-less triple behind a clean first mm triple
            # does NOT state (the first match states); the second
            # triple's numbers are bare (not mm numbers) and stay
            # unmapped/offerable — they are never mm numbers:
            pytest.param(
                "60 x 45 mm in a 70x50x30 box",
                {"W": 60.0, "D": 45.0},
                id="first-clean-match-states-second-unmapped",
            ),
            pytest.param(
                "a 60x45x20mm tray in the kitchen",
                {"W": 60.0, "D": 45.0, "H": 20.0},
                id="glued-mm-triple-followed-by-in",
            ),
            # No mm unit at all: the next-token foreign check still
            # applies — "in"/"inch" are inch units:
            pytest.param("2 x 2 in", {}, id="in-preposition-no-mm-unit"),
            pytest.param("2 × 2 inch", {}, id="inch-token-no-mm-unit"),
        ],
    )
    def test_explicit_mm_unit_beats_following_in(self, message, expected):
        """An explicit mm unit on the match wins over a following "in"
        (issue #275 fix: the foreign-unit next-token check must not read
        the preposition as the inch unit when the match already carries
        an explicit mm unit)."""
        assert stated_axes_from_message(message) == expected

    def test_second_unmapped_triple_numbers_stay_unmapped(self):
        """'60 x 45 mm in a 70x50x30 box' — the second triple's numbers
        are unit-less and, since no mm unit ever attaches to them, they
        are NOT mm numbers and stay out of the mm unmapped set (the
        tier-2 offer only ever offers explicit-mm numbers)."""
        assert user_quoted_unmapped_mm(["60 x 45 mm in a 70x50x30 box"]) == set()

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

    # ------------------------------------------------------------------
    # Issue #305: "by" joiner
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            pytest.param("60 by 45 by 80 mm", {"W": 60.0, "D": 45.0, "H": 80.0}, id="by-triple-spaced"),
            pytest.param("60 by 45 by 80mm", {"W": 60.0, "D": 45.0, "H": 80.0}, id="by-triple-glued-last"),
            pytest.param("60mm by 45mm by 80mm", {"W": 60.0, "D": 45.0, "H": 80.0}, id="by-triple-each-number"),
            pytest.param("a box 60 by 45 by 80 mm", {"W": 60.0, "D": 45.0, "H": 80.0}, id="by-triple-with-noun"),
            pytest.param("60 x 45 by 80 mm", {"W": 60.0, "D": 45.0, "H": 80.0}, id="mixed-x-by"),
        ],
    )
    def test_by_joiner_triple(self, message, expected):
        """Issue #305: 'by' as a whole-word joiner states W/D/H like the
        x-form (each joiner independent — mixed x/by is valid)."""
        axes = stated_axes_from_message(message)
        assert axes == expected

    def test_by_joiner_consumed(self):
        """Issue #305: '60 x 45 by 80 mm' — all three numbers consumed
        (the leaked-offerable-80 path is closed)."""
        unmapped = user_quoted_unmapped_mm(["60 x 45 by 80 mm"])
        assert 60.0 not in unmapped
        assert 45.0 not in unmapped
        assert 80.0 not in unmapped

    # ------------------------------------------------------------------
    # Issue #305: two-number footprint pairs
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            pytest.param("60 x 45 mm plate", {"W": 60.0, "D": 45.0}, id="footprint-x"),
            pytest.param("a 60 by 45 mm tray", {"W": 60.0, "D": 45.0}, id="footprint-by"),
            pytest.param("60mm x 45mm", {"W": 60.0, "D": 45.0}, id="footprint-glued"),
        ],
    )
    def test_footprint_pair_wd_only(self, message, expected):
        """Issue #305: two-number footprint states W and D only (H
        absent), and stated_dims_from_message returns None."""
        axes = stated_axes_from_message(message)
        assert axes == expected
        assert "H" not in axes
        assert stated_dims_from_message(message) is None

    def test_footprint_pair_consumed(self):
        """Issue #305: a stated footprint pair's numbers are consumed
        (excluded from user_quoted_unmapped_mm)."""
        assert user_quoted_unmapped_mm(["60 x 45 mm plate"]) == set()

    # ------------------------------------------------------------------
    # Issue #305: part-noun conditional suppression
    # ------------------------------------------------------------------

    def test_part_noun_lid_primary_object_states(self):
        """Issue #305: 'a 60 × 45 mm lid' — lid is the primary object
        (no earlier match states), so the pair states W/D."""
        axes = stated_axes_from_message("a 60 × 45 mm lid")
        assert axes == {"W": 60.0, "D": 45.0}

    def test_part_noun_lid_primary_object_make(self):
        """Issue #305: 'make a 60 x 45 mm lid' — same, with verb."""
        axes = stated_axes_from_message("make a 60 x 45 mm lid")
        assert axes == {"W": 60.0, "D": 45.0}

    def test_part_noun_lid_suppressed_by_earlier_triple(self):
        """Issue #305: 'a box 60 × 45 × 80 mm with a 55 × 40 mm lid' —
        the box triple states (first match wins); the lid pair is
        suppressed (earlier match already stated the envelope). 40 stays
        unmapped/offerable — 55 is not in the offer scan because it has no
        explicit mm unit in the message (the offer regex only matches
        numbers followed by "mm")."""
        axes = stated_axes_from_message("a box 60 × 45 × 80 mm with a 55 × 40 mm lid")
        assert axes == {"W": 60.0, "D": 45.0, "H": 80.0}
        unmapped = user_quoted_unmapped_mm(["a box 60 × 45 × 80 mm with a 55 × 40 mm lid"])
        assert 40.0 in unmapped
        # 55 is not in the unmapped set — it has no explicit "mm" unit in
        # the message, so the tier-2 offer regex (\b(\d+)\s*mm\b) never
        # matches it. This is NOT because it duplicates a stated box value.
        # The suppressed lid pair's 40 stays offerable (it has "mm"),
        # proving nothing was consumed from the pair.
        # The box numbers are consumed (they have explicit mm).
        assert 60.0 not in unmapped
        assert 45.0 not in unmapped
        assert 80.0 not in unmapped

    def test_part_noun_lid_no_earlier_triple_states(self):
        """Issue #305: 'a box with a 55 × 40 mm lid' — the box has no
        numbers, so the lid pair is the primary object and states W/D."""
        axes = stated_axes_from_message("a box with a 55 × 40 mm lid")
        assert axes == {"W": 55.0, "D": 40.0}

    def test_part_noun_lid_suppressed_by_later_triple(self):
        """Issue #305 (HIGH security regression) — FLIPPED by issue #314
        (operator decision): 'a 55x40 mm lid for the 60x45x80mm box' — the
        lid pair is textually EARLIER but the box triple (later) is now in
        the mating part's zone (after the "for" mating connector), so it is
        SUPPRESSED and the LID pair states W/D (the head noun's own size).
        The box's 60/45/80 stay unmapped/offerable (not consumed). Without
        the #314 mating guard, the box triple would state and the bbox gate
        would enforce a fabricated 60x45x80 target against a 55x40 lid —
        the same anti-pattern, now the mirror image.
        """
        axes = stated_axes_from_message("a 55x40 mm lid for the 60x45x80mm box")
        assert axes == {"W": 55.0, "D": 40.0}
        # The box's 60/45/80 are unmapped/offerable (not consumed by the
        # suppressed mating triple) — the lid pair's 55/40 are consumed.
        unmapped = user_quoted_unmapped_mm(["a 55x40 mm lid for the 60x45x80mm box"])
        assert 60.0 not in unmapped  # "60" in "60x45x80mm" is not an explicit-mm number
        assert 45.0 not in unmapped
        assert 80.0 not in unmapped
        # The lid's 40 IS an explicit-mm number and IS consumed (stated).
        assert 40.0 not in unmapped

    def test_part_noun_lid_suppressed_by_later_triple_comma(self):
        """Issue #305 (HIGH security regression, comma variant): 'make a
        40x60 lid, the box is 60x45x80mm' — the lid pair is textually
        first, the box triple is textually second (separated by a comma).
        The box states, the lid pair does not."""
        axes = stated_axes_from_message("make a 40x60 lid, the box is 60x45x80mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 80.0}

    def test_part_noun_lid_suppressed_by_later_by_form_triple(self):
        """Issue #305 — FLIPPED by issue #314 (operator decision):
        'a 55 by 40 mm lid for the 60 by 45 by 80 mm box' — the lid pair
        (by-joiner) precedes the box triple (also by-joiner); the box is
        now in the mating part's zone (after the "for" mating connector)
        and is SUPPRESSED, so the LID pair states W/D (the head noun's
        own size). The box's 60/45/80 stay unmapped/offerable.
        """
        axes = stated_axes_from_message("a 55 by 40 mm lid for the 60 by 45 by 80 mm box")
        assert axes == {"W": 55.0, "D": 40.0}
        # The box's 80 ("80 mm" — the only box number with an explicit mm
        # unit) stays unmapped/offerable (not consumed). The lid's 40
        # ("40 mm") IS consumed (stated).
        unmapped = user_quoted_unmapped_mm(["a 55 by 40 mm lid for the 60 by 45 by 80 mm box"])
        assert 80.0 in unmapped
        assert 40.0 not in unmapped

    def test_part_noun_lid_suppressed_by_later_comma_variant(self):
        """Issue #305 round-1 (HIGH security regression): 'a 55 x 40 mm
        lid, box is 60x45x80mm' — the lid pair is textually first, the
        box triple is textually second (separated by a comma after "lid").
        The comma makes "lid" a clause boundary, so it does NOT suppress
        the box triple. The box states, the lid pair does not."""
        axes = stated_axes_from_message("a 55 x 40 mm lid, box is 60x45x80mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 80.0}

    def test_part_noun_lid_consumed_when_stated(self):
        """Issue #305: 'a 60 × 45 mm lid' (stated) — numbers consumed."""
        assert user_quoted_unmapped_mm(["a 60 × 45 mm lid"]) == set()

    # ------------------------------------------------------------------
    # Issue #305: by-form guard pass-through
    # ------------------------------------------------------------------

    def test_by_form_letter_glued(self):
        """Issue #305: 'an M3 by 10 mm screw' — letter-glued guard
        suppresses (M before 3)."""
        assert stated_axes_from_message("an M3 by 10 mm screw") == {}

    def test_by_form_foreign_unit(self):
        """Issue #305: '60 by 45 cm' — foreign unit guard suppresses."""
        assert stated_axes_from_message("60 by 45 cm") == {}

    def test_by_form_magnitude(self):
        """Issue #305: 'a 150 by 45 tray' — unit-less pair >100,
        magnitude guard suppresses."""
        assert stated_axes_from_message("a 150 by 45 tray") == {}

    def test_by_form_feature_noun(self):
        """Issue #305: 'a 10 by 10 mm hole' — feature noun 'hole'
        suppresses the pair."""
        assert stated_axes_from_message("a 10 by 10 mm hole") == {}

    def test_by_form_mm_unit_beats_in(self):
        """Issue #305: '60 by 45 mm in the drawer' — explicit mm unit
        on the match beats the following 'in' preposition."""
        axes = stated_axes_from_message("60 by 45 mm in the drawer")
        assert axes == {"W": 60.0, "D": 45.0}

    # ------------------------------------------------------------------
    # Issue #314: mating-connector suppression (triple/pair path)
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        ("message", "expected"),
        [
            # The QA-found-wrong case (the primary bug):
            pytest.param(
                "a 55 × 40 mm lid that fits a 60 × 45 mm box",
                {"W": 55.0, "D": 40.0},
                id="lid-fits-box-lid-states",
            ),
            pytest.param(
                "a 55 x 40 mm lid that fits a 60 x 45 mm box",
                {"W": 55.0, "D": 40.0},
                id="lid-fits-box-x-joiner",
            ),
            # No own size — nothing stated:
            pytest.param(
                "a lid for a 60 × 45 mm box",
                {},
                id="lid-for-box-nothing",
            ),
            # 'to fit over' multi-word connector:
            pytest.param(
                "a 55 × 40 mm lid to fit over a 60 × 45 × 30 mm box",
                {"W": 55.0, "D": 40.0},
                id="lid-to-fit-over-box-triple-suppressed",
            ),
            # Head-noun-independent: 'for' suppresses the phone's pair:
            pytest.param(
                "a 60 mm wide stand for a 100 × 70 mm phone",
                {"W": 60.0},
                id="stand-for-phone-stand-states",
            ),
            # Non-mating 'for': the tray still states (no size after 'for'):
            pytest.param(
                "a tray 60 × 45 × 20 mm for screws",
                {"W": 60.0, "D": 45.0, "H": 20.0},
                id="tray-for-screws-tray-states",
            ),
            # 'for' + feature noun: nothing stated (double guard):
            pytest.param(
                "room for a 30 mm screw",
                {},
                id="room-for-screw-nothing",
            ),
            # Comma after mating phrase: '5 mm thick' is not an axis word
            # pair, so nothing from the box and nothing from the thick:
            pytest.param(
                "a lid that fits a 60 × 45 mm box, 5 mm thick",
                {},
                id="lid-fits-box-comma-thick-nothing",
            ),
            # 'with' clause after mating phrase: the 10 mm rim is a
            # feature noun (suppressed independently), the box pair is
            # mating-suppressed, the lid pair states:
            pytest.param(
                "a 55 × 40 mm lid that fits a 60 × 45 mm box with a 10 mm rim",
                {"W": 55.0, "D": 40.0},
                id="lid-fits-box-with-rim-lid-states",
            ),
        ],
    )
    def test_mating_connector_suppresses_mating_size(self, message, expected):
        """Issue #314: a size after a mating connector (fits/fit/fitting/
        to fit/for/over/onto/on top of/that goes on) belongs to the
        mating part and never states the part's axes. The head noun's own
        size (before the connector) states normally."""
        assert stated_axes_from_message(message) == expected

    def test_mating_connector_x_form_flipped(self):
        """Issue #314: 'a 55x40 mm lid for the 60x45x80mm box' — the
        x-joiner variant of the QA case. The box triple is mating-
        suppressed; the lid pair states W/D."""
        axes = stated_axes_from_message("a 55x40 mm lid for the 60x45x80mm box")
        assert axes == {"W": 55.0, "D": 40.0}

    def test_mating_connector_by_form_flipped(self):
        """Issue #314: 'a 55 by 40 mm lid for the 60 by 45 by 80 mm box'
        — the by-joiner variant. The box triple is mating-suppressed;
        the lid pair states W/D."""
        axes = stated_axes_from_message(
            "a 55 by 40 mm lid for the 60 by 45 by 80 mm box"
        )
        assert axes == {"W": 55.0, "D": 40.0}

    def test_mating_connector_slip_fit_not_connector(self):
        """Issue #314: 'slip fit' is a fit-TYPE, not a mating connector.
        A message containing 'slip fit' alongside a valid triple still
        states the triple (the 'fit' in 'slip fit' is filtered out by
        ``_mating_connector_at``'s fit-type filter)."""
        axes = stated_axes_from_message("a 60 x 45 mm lid with a slip fit")
        assert axes == {"W": 60.0, "D": 45.0}

    def test_mating_connector_unmapped_numbers(self):
        """Issue #314: 'a 55 × 40 mm lid that fits a 60 × 45 mm box' —
        the lid pair's numbers (55/40) are consumed (stated); the box's
        numbers (60/45) are mating-suppressed and stay unmapped/offerable
        (not consumed). The mm-only offer scan sees 40 (lid's D, consumed)
        and 45 (box's D, explicit-mm, unmapped). The box's 60 is NOT an
        explicit-mm number ("60 × 45 mm" — only 45 is adjacent to "mm")."""
        unmapped = user_quoted_unmapped_mm(
            ["a 55 × 40 mm lid that fits a 60 × 45 mm box"]
        )
        # 40 is consumed (the lid's D is stated) → not offerable.
        assert 40.0 not in unmapped
        # 45 is the box's D — mating-suppressed, not consumed → still
        # offerable (explicit-mm number, adjacent to "mm").
        assert 45.0 in unmapped

    def test_mating_connector_no_own_size_unmapped(self):
        """Issue #314: 'a lid for a 60 × 45 mm box' — nothing stated
        (no own size); the box's 45 ("45 mm" — explicit-mm) stays
        unmapped/offerable. The box's 60 is not an explicit-mm number
        ("60 × 45 mm" — only 45 is adjacent to "mm")."""
        axes = stated_axes_from_message("a lid for a 60 × 45 mm box")
        assert axes == {}
        unmapped = user_quoted_unmapped_mm(["a lid for a 60 × 45 mm box"])
        assert 45.0 in unmapped

    def test_mating_connector_triple_suppressed_unmapped(self):
        """Issue #314: 'a 55 × 40 mm lid to fit over a 60 × 45 × 30 mm
        box' — the box triple (60/45/30) is mating-suppressed; the lid
        pair states W/D. The box's 30 ("30 mm" — explicit-mm) stays
        unmapped/offerable; the lid's 40 ("40 mm" — consumed) is not."""
        axes = stated_axes_from_message(
            "a 55 × 40 mm lid to fit over a 60 × 45 × 30 mm box"
        )
        assert axes == {"W": 55.0, "D": 40.0}
        unmapped = user_quoted_unmapped_mm(
            ["a 55 × 40 mm lid to fit over a 60 × 45 × 30 mm box"]
        )
        # The box's 30 is explicit-mm and mating-suppressed → offerable.
        assert 30.0 in unmapped
        # The lid's 40 is consumed (stated) → not offerable.
        assert 40.0 not in unmapped

    # ------------------------------------------------------------------
    # Issue #305: 'by' non-dimension false positives
    # ------------------------------------------------------------------

    @pytest.mark.parametrize(
        "message",
        [
            pytest.param("stand by", id="stand-by"),
            pytest.param("by the edge", id="by-the-edge"),
            pytest.param("made by 3 mm walls", id="made-by-walls"),
            pytest.param("made by Alice", id="made-by-alice"),
        ],
    )
    def test_by_non_dimension_does_not_state(self, message):
        """Issue #305: 'by' as a preposition never becomes a joiner
        (no digit on both sides of the joiner)."""
        assert stated_axes_from_message(message) == {}


class TestTripleFalsePositives:
    """Issue #275 round-1: the triple must not state axes on non-envelope
    number pairs (screw specs, count×size pairs, grid/pixel pairs,
    version labels). Each case states NOTHING — the carry-forward + gate
    would otherwise enforce a fabricated envelope."""

    def test_m3_screw_spec_does_not_state(self):
        """'M3 x 10 mm screw' → nothing (thread spec; 'M3' is a
        letter-glued token, 'screw' a feature noun)."""
        assert stated_axes_from_message("M3 x 10 mm screw") == {}
        assert stated_axes_from_message("M3x10mm screw") == {}

    def test_count_times_size_pair_does_not_state(self):
        """'add 2x magnets 6x3mm' / 'print 2 x 40 mm spacers' → nothing
        (the pair is the count and the feature size, not the envelope;
        'magnets'/'spacers' are feature nouns — issue #275 round-1)."""
        assert stated_axes_from_message("add 2x magnets 6x3mm") == {}
        assert stated_axes_from_message("print 2 x 40 mm spacers") == {}

    def test_grid_pair_does_not_state(self):
        """'a 5x5 grid' → nothing (a cell count; 'grid' is a feature
        noun and the no-unit double is below the mm scale)."""
        assert stated_axes_from_message("a 5x5 grid") == {}

    def test_pixel_resolution_does_not_state(self):
        """'a 1920x1080 screen' → nothing (pixel dimensions without a
        millimetre unit are not a part envelope — issue #275 round-1)."""
        axes = stated_axes_from_message("a 1920x1080 screen")
        assert axes == {}

    def test_version_label_does_not_state(self):
        """'v2 is 60x45' → the 'v2' is NOT immediately before the
        triple's first digit (there's 'is ' in between), so the
        letter-glued guard does not fire and the triple states W/D
        (issue #275 round-3, item 4: check only the character
        IMMEDIATELY before the first digit)."""
        axes = stated_axes_from_message("v2 is 60x45")
        assert axes == {"W": 60.0, "D": 45.0}

    def test_no_unit_double_under_threshold_states(self):
        """'60 × 45' (no unit, ≤ the mm-scale bound) still states W and D —
        the magnitude guard only rejects large pixel/count pairs, not
        ordinary no-unit doubles."""
        axes = stated_axes_from_message("60 × 45")
        assert axes == {"W": 60.0, "D": 45.0}

    def test_followup_feature_triple_does_not_restate_envelope(self):
        """A follow-up 'add a 6x3 mm magnet pocket' restates nothing —
        the feature noun suppresses the pair, so W/D carry forward
        unchanged (the gate keeps the carried envelope, not 6×3).
        """
        axes = stated_axes_from_message(
            "add a 6x3 mm magnet pocket", chat_history=["a tray 60 × 45 × 20 mm"]
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}


class TestTriplePerMatchEvaluation:
    """Issue #275 round-3: per-match guards, clause-local assignment,
    unit-aware guards, and the full acceptance table.

    Every item below gets a regression test (table-driven where
    natural). The principle: text only states a part size when it
    clearly does. When in doubt, don't state; leave the number
    unmapped so it can be offered. But never suppress a clean,
    explicit statement.
    """

    # ------------------------------------------------------------------
    # Item 1: clause-local assignment (HIGH: nearest-number crosses
    # clauses)
    # ------------------------------------------------------------------

    def test_clause_local_deep_and_wide(self):
        """'make it 10 mm deep and 40mm wide' → D=10, W=40 (each axis
        word only sees numbers in its own sub-clause)."""
        from d33d.axis_lexicon import classify

        result = classify("make it 10 mm deep and 40mm wide")
        assert result.absolute == {"D": 10.0, "W": 40.0}

    def test_clause_local_three_axes(self):
        """'40 mm wide, 10 mm deep and 12 mm tall' → all three axes.
        Comma splits into clauses; 'and' splits the second clause
        because each part has a number."""
        from d33d.axis_lexicon import classify

        result = classify("40 mm wide, 10 mm deep and 12 mm tall")
        assert result.absolute == {"W": 40.0, "D": 10.0, "H": 12.0}

    # ------------------------------------------------------------------
    # Item 2: per-match evaluation (first match that passes all guards)
    # ------------------------------------------------------------------

    def test_triple_first_match_wins_slot_suppressed(self):
        """'a 60x45x20mm tray with a 10x8mm slot' → W60 D45 H20, with
        10 and 8 unmapped (the slot's pair is suppressed by the feature
        noun 'slot' in the after-window; its numbers stay unmapped).
        """
        axes = stated_axes_from_message("a 60x45x20mm tray with a 10x8mm slot")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}
        assert user_quoted_unmapped_mm(["a 60x45x20mm tray with a 10x8mm slot"]) == set()

    def test_triple_first_match_wins_slot_suppressed_spaced(self):
        """'a 60 x 45 x 20 mm tray with a 10 x 8 mm slot' → the same,
        and 20 is NOT unmapped (consumed by the triple)."""
        axes = stated_axes_from_message(
            "a 60 x 45 x 20 mm tray with a 10 x 8 mm slot"
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}
        unmapped = user_quoted_unmapped_mm(
            ["a 60 x 45 x 20 mm tray with a 10 x 8 mm slot"]
        )
        assert 20.0 not in unmapped

    def test_triple_second_match_states_when_first_suppressed(self):
        """'a 10 × 10 mm hole in a 60 × 45 × 20 mm tray' → the tray
        (the first match is suppressed by 'hole' in the after-window;
        the second match states)."""
        axes = stated_axes_from_message(
            "a 10 × 10 mm hole in a 60 × 45 × 20 mm tray"
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    # ------------------------------------------------------------------
    # Item 3: magnitude guard (HIGH: unit-aware)
    # ------------------------------------------------------------------

    def test_magnitude_guard_unit_after_last_number(self):
        """'a 150x45mm tray' → W150 D45 (explicit mm unit disables the
        >100 bound)."""
        axes = stated_axes_from_message("a 150x45mm tray")
        assert axes == {"W": 150.0, "D": 45.0}

    def test_magnitude_guard_unit_glued(self):
        """'a 105x45x20mm tray' → full triple (glued mm unit)."""
        axes = stated_axes_from_message("a 105x45x20mm tray")
        assert axes == {"W": 105.0, "D": 45.0, "H": 20.0}

    def test_magnitude_guard_unitless_pair_over_100(self):
        """'1920x1080' → nothing (unit-less pair >100)."""
        axes = stated_axes_from_message("1920x1080")
        assert axes == {}

    def test_magnitude_guard_unitless_pair_150x45(self):
        """'a 150x45 tray' → nothing (unit-less pair >100); 150 and 45
        are unmapped (no mm unit, so not in the mm-only unmapped set).
        """
        axes = stated_axes_from_message("a 150x45 tray")
        assert axes == {}

    # ------------------------------------------------------------------
    # Item 4: letter-glued guard (immediately-before only)
    # ------------------------------------------------------------------

    def test_letter_glued_version_label(self):
        """'v2 is 60x45x20mm' → states (the 'v2' is not immediately
        before the triple's first digit — there's 'is ' in between).
        """
        axes = stated_axes_from_message("v2 is 60x45x20mm")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_letter_glued_thread_spec(self):
        """'M3 x 10 mm screw' → nothing ('M' is immediately before the
        '3')."""
        axes = stated_axes_from_message("M3 x 10 mm screw")
        assert axes == {}

    def test_no_partial_match_inside_longer_number(self):
        r"""'123x45x67' → no partial match (the (?<!\d) lookbehind
        prevents matching '23x45x67' starting inside '123')."""
        from d33d.dimension_protocol import _extract_triple

        axes, _consumed = _extract_triple("123x45x67")
        # The full triple matches (123, 45, 67) — not a partial (23, 45, 67).
        assert axes == {"W": 123.0, "D": 45.0, "H": 67.0}

    # ------------------------------------------------------------------
    # Item 5: foreign-unit after-check (whole token, not 5-char slice)
    # ------------------------------------------------------------------

    def test_foreign_unit_after_check_mirror(self):
        """'a 60 x 45 x 20 mm mirror' → states (the 'mirror' token is
        not a foreign unit)."""
        axes = stated_axes_from_message("a 60 x 45 x 20 mm mirror")
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_foreign_unit_after_check_inch(self):
        """'2 × 2 inch' → nothing (the 'inch' token is a foreign unit)."""
        axes = stated_axes_from_message("2 × 2 inch")
        assert axes == {}

    def test_foreign_unit_after_check_cm(self):
        """'6 x 4 cm' → nothing (the 'cm' token is a foreign unit)."""
        axes = stated_axes_from_message("6 x 4 cm")
        assert axes == {}

    # ------------------------------------------------------------------
    # Item 6: spelled-out units in triple and axis-letter paths
    # ------------------------------------------------------------------

    def test_spelled_out_triple(self):
        """'60 millimeters x 45 millimeters x 20 millimeters' → full
        triple."""
        axes = stated_axes_from_message(
            "60 millimeters x 45 millimeters x 20 millimeters"
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_spelled_out_axis_letter(self):
        """'H 30 millimetres' → H30 (the axis-letter path accepts
        spelled-out units)."""
        axes = stated_axes_from_message("H 30 millimetres")
        assert axes == {"H": 30.0}

    # ------------------------------------------------------------------
    # Item 7: before-window 3 words (was 2)
    # ------------------------------------------------------------------

    def test_before_window_three_words(self):
        """'a 10 × 10 mm square hole in a 60 × 45 × 20 mm tray' → the
        tray (the first triple is suppressed by 'hole' in the
        3-word before-window of the second triple's context)."""
        axes = stated_axes_from_message(
            "a 10 × 10 mm square hole in a 60 × 45 × 20 mm tray"
        )
        assert axes == {"W": 60.0, "D": 45.0, "H": 20.0}

    def test_before_window_three_words_square_hole(self):
        """'a 10 x 10 mm square hole' → nothing (the feature noun
        'hole' is in the after-window of the triple)."""
        axes = stated_axes_from_message("a 10 x 10 mm square hole")
        assert axes == {}

    # ------------------------------------------------------------------
    # Item 8: _has_number foreign-then-continue logic
    # ------------------------------------------------------------------

    def test_has_number_foreign_then_shared(self):
        """'5 cm wide and 12mm tall' → _has_number returns True (the
        foreign number is skipped, the shared-token number is found).
        """
        from d33d.axis_lexicon import _has_number

        assert _has_number("5 cm wide and 12mm tall") is True
        assert _has_number("5 cm wide") is False
        assert _has_number("12mm tall") is True


# ---------------------------------------------------------------------------
# Issue #332 — ground truth: require_dimensions_confirmed with a settled part
# ---------------------------------------------------------------------------


def test_require_dimensions_confirmed_settled_part_closes_gate():
    """Issue #332 sub-issue 3: when a project has a settled/assumed part,
    ``require_dimensions_confirmed`` treats the part's measured W/D/H as
    confirmed — no W/D/H questions are asked, and the gate closes (the
    design loop runs on the part's ground-truth baseline)."""
    # No user-stated dims, but a settled part with a known mm bbox.
    c = require_dimensions_confirmed(
        ["I want to add a lip to this part"],
        stated_dims=None,
        part_scale=1.0,
        part_bbox_mm=(60.0, 45.0, 20.0),
    )
    assert c.confirmed is True
    assert c.stated_dims == (60.0, 45.0, 20.0)
    # The fit type falls to the default (the part's fit is the mesh's).
    assert c.fit_type == "no_fit"


def test_require_dimensions_confirmed_no_part_stays_open():
    """Issue #332 sub-issue 3: without a part, the gate stays open
    (byte-identity regression anchor)."""
    c = require_dimensions_confirmed(
        ["I want to add a lip to this part"],
        stated_dims=None,
    )
    assert c.confirmed is False
    assert len(c.questions) > 0


def test_require_dimensions_confirmed_settled_part_with_partial_user_stated():
    """Issue #332 sub-issue 3: the part's measured W/D/H fills only the
    MISSING axes — a user-stated value for an axis is never overridden."""
    c = require_dimensions_confirmed(
        ["W is 50"],
        stated_dims={"W": 50.0},
        part_scale=1.0,
        part_bbox_mm=(60.0, 45.0, 20.0),
    )
    assert c.confirmed is True
    # W is the user's 50, D and H are the part's 45/20.
    assert c.stated_dims == (50.0, 45.0, 20.0)


def test_require_dimensions_confirmed_unsetled_part_stays_open():
    """Issue #332 sub-issue 3: an unsettled part (``part_scale=None``)
    does NOT fill the gate — the gate stays open (the unsettled
    pre-route stops the loop before this gate runs). The gate only
    fills from the part when both ``part_scale`` and ``part_bbox_mm``
    are present."""
    c = require_dimensions_confirmed(
        ["make it bigger"],
        stated_dims=None,
        part_scale=None,
        part_bbox_mm=None,
    )
    assert c.confirmed is False
