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
    emit_named_params,
    require_dimensions_confirmed,
    resolution_questions,
    resolve_tolerance_mm,
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
