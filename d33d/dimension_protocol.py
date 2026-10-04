"""CLARIFY-before-code dimension protocol (ticket #5, task-protocol workstream).

Step 1 of the mandatory conversational protocol in 04-design-loop.md is the
single biggest divergence from the Meshy/Tripo-style "just guess" agents:

* **Dimensions are ground truth, never estimated.** The agent ASKS (or the
  user draws a dimension line on the photo). Monocular metric-depth models
  are cm-scale at best — infeasible for mm-level CAD — so no model output
  may silently become a fit-critical number.
* **Zero OpenSCAD output before dimensions are clarified.** This is
  enforced by a hard gate: :func:`require_dimensions_confirmed` returns a
  ``DimensionClarification`` with ``confirmed=False`` and a list of
  clarifying questions UNTIL the user has explicitly confirmed a complete
  mm dimension set. The design loop must not generate any ``.scad`` before
  this gate passes.
* **AI pre-fill is a CONFIRMABLE SUGGESTION only.** The AI may pre-fill a
  low-confidence suggestion the user confirms; it is never the source of
  truth. A suggested value the user has not yet confirmed is treated the
  same as a missing value — the gate stays closed.
* **The agent PROACTIVELY ASKS FIT TYPE and applies FDM clearances.** The
  closed fit-type enum is slip / press / interference / snap. The small
  FDM-appropriate clearance table (slip 0.2–0.4 mm total diametral, press
  ~0 to −0.1 mm interference, holes print 0.1–0.25 mm undersize) resolves a
  concrete per-fit tolerance value.
* **Stated dimensions + resolved tolerance are NAMED PARAMETERS, never
  literals.** :func:`emit_named_params` produces the ``{param_name: value}``
  map (and :meth:`DimensionClarification.scad_param_block` renders it as a
  plain-assignment variable block) so the ``.scad`` generator can emit
  ``name = value;`` declarations and reference them in geometry — the
  value never appears as a baked literal inside an expression.
* **Persistence alongside the transcript.** The confirmed parameters are
  written to ``d33d.db`` (``project_dimensions`` table, ``put_dimensions`` /
  ``get_dimensions``) so a downstream bbox gate (#3 gate 4) has a record to
  compare against. "Active version" promotion is issue #7's concern — this
  workstream only persists the confirmed set alongside the transcript, it
  does not manage version history.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from d33d.axis_lexicon import MM_UNIT_ALTERNATION
from d33d.triple_extraction import (
    QUOTED_UNMAPPED_MAX_MESSAGES,
    _extract_triple,
    _mm_cue_values,
    _triple_suppressed_by_feature_noun,
    user_quoted_unmapped_mm,
)

logger = logging.getLogger(__name__)

__all__ = [
    "DIMENSION_AXES",
    "FDM_CLEARANCE_TABLE",
    "HOLES_PRINT_UNDERSIZE_MM",
    "QUOTED_UNMAPPED_MAX_MESSAGES",
    "CuesLike",
    "DimensionClarification",
    "FitType",
    "_extract_triple",
    "_mm_cue_values",
    "_triple_suppressed_by_feature_noun",
    "carried_stated_set",
    "effective_stated_dims",
    "emit_named_params",
    "latest_stated_dims_dict",
    "offer_tier_signals",
    "require_dimensions_confirmed",
    "resolution_questions",
    "resolve_tolerance_mm",
    "stated_axes_from_message",
    "stated_dims_from_message",
    "user_quoted_unmapped_mm",
]

#: The closed fit-type enum the protocol asks about. The spec pins slip
#: (0.2–0.4 mm total diametral) and press (~0 to −0.1 mm interference);
#: ``interference`` covers the deliberate-over-press case and ``snap`` the
#: snap-fit case. ``no_fit`` is the default for parts with no mating
#: interface (no clearance applied — nothing to add).
FitType = Literal["slip", "press", "interference", "snap", "no_fit"]

#: The FDM-appropriate clearance/tolerance table, per fit type: the
#: *total diametral* clearance in mm to ADD to the hole (or subtract from
#: the peg, for interference-negative values) so the part fits on an FDM
#: printer. Spec-pinned: slip 0.2–0.4 mm total diametral; press ~0 to
#: −0.1 mm interference; holes print 0.1–0.25 mm undersize. ``snap`` uses
#: the slip band's lower bound (a snap requires a small elastic
#: deflection, not a large slip clearance).
FDM_CLEARANCE_TABLE: dict[FitType, tuple[float, float]] = {
    "slip": (0.2, 0.4),
    "press": (-0.1, 0.0),
    "interference": (-0.4, -0.1),
    "snap": (0.2, 0.3),
    "no_fit": (0.0, 0.0),
}

#: The FDM "holes print undersize" band, in mm (spec-pinned 0.1–0.25).
#: A hole of nominal diameter ``d`` prints effectively at
#: ``d - hole_undersize``; use the mid-value as the default.
HOLES_PRINT_UNDERSIZE_MM: tuple[float, float] = (0.1, 0.25)

#: The canonical named-parameter axes for the W/D/H stated-dimension triple
#: (mm). These are the param names the design loop's bbox gate compares
#: against the render — they must match ``d33d.design_loop``'s W/D/H
#: naming so the named-parameter structure produced here is directly usable.
DIMENSION_AXES: tuple[str, ...] = ("W", "D", "H")

_FIT_TYPE_SET: frozenset[str] = frozenset(FDM_CLEARANCE_TABLE)
_AXIS_SET: frozenset[str] = frozenset(DIMENSION_AXES)


def _mid(lo: float, hi: float) -> float:
    """Midpoint of the clearance band, rounded to 0.05 mm (FDM is not
    finer than ~0.05 mm reliably)."""
    m = (lo + hi) / 2.0
    return round(round(m * 20.0) / 20.0, 4)


def resolve_tolerance_mm(fit_type: FitType) -> float:
    """The concrete FDM clearance/tolerance (total diametral, mm) for a
    fit type — the midpoint of the spec's band, rounded to 0.05 mm.

    ``no_fit`` resolves to 0.0 (no clearance applied). Negative values are
    interference (press/interference) — the caller subtracts from the hole /
    adds to the peg.
    """
    if fit_type not in _FIT_TYPE_SET:
        raise ValueError(f"unknown fit type: {fit_type!r}")
    return _mid(*FDM_CLEARANCE_TABLE[fit_type])


@dataclass(frozen=True)
class DimensionClarification:
    """The outcome of the CLARIFY gate.

    ``confirmed`` is True iff the user has explicitly confirmed a COMPLETE
    mm dimension set (all of W/D/H) AND a fit type has been chosen (the
    agent proactively asks). When confirmed is False, ``questions`` is the
    list of clarifying questions the agent must ask before any ``.scad`` is
    generated; when True, ``params`` carries the confirmed named parameters
    (stated dims + the resolved FDM tolerance) and ``fit_type`` the chosen
    fit. ``suggested`` carries any AI pre-fill (a confirmable suggestion
    only — never ground truth).
    """

    confirmed: bool
    questions: tuple[str, ...] = ()
    params: dict[str, float] = field(default_factory=dict)
    fit_type: FitType = "no_fit"
    tolerance_mm: float = 0.0
    suggested: dict[str, float] = field(default_factory=dict)

    @property
    def stated_dims(self) -> tuple[float, float, float]:
        """The confirmed (W, D, H) triple in mm, in axis order."""
        return (self.params["W"], self.params["D"], self.params["H"])

    def scad_param_block(self) -> str:
        """The named-parameter block as a plain-assignment variable block
        (one ``name = value;`` per line, dimensions first in W/D/H order,
        then ``fit_type``/``tolerance_mm``).

        This is the exact text the ``.scad`` generator prepends so every
        stated dimension AND the FDM tolerance appear ONLY in a
        declaration, never as an inline literal in a geometry expression.
        Values are rendered with ``:g`` so 30.0 emits ``30`` and 0.25
        emits ``0.25`` — no spurious trailing zeros.
        """
        if not self.confirmed:
            raise ValueError(
                "no confirmed dimensions — clarify before emitting a "
                "parameter block (CLARIFY-before-code mandate)"
            )
        lines = []
        for name in DIMENSION_AXES:
            lines.append(f"{name} = {self.params[name]:g};")
        lines.append(f"fit_type = {self.fit_type!r}[1:-1];")
        lines.append(f"tolerance_mm = {self.tolerance_mm:g};")
        return "\n".join(lines)


def _coerce(value: Any) -> float | None:
    """Coerce a user-stated dimension to a positive float (mm), else None."""
    if value is None:
        return None
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _extract_stated(
    chat_history: list[str],
    stated_dims: dict[str, Any] | None,
    ai_suggested: dict[str, float] | None,
    *,
    _release_history: bool = True,
) -> dict[str, float]:
    """Gather confirmed dimensions from (in priority order) explicit
    ``stated_dims``, then chat text of the form ``W: 42`` / ``42 mm``
    attached to a W/D/H axis, then a USER-confirmed AI suggestion.

    An AI-suggested value is ONLY accepted here if the user has confirmed
    it in the chat (the "confirmable suggestion, never ground truth" rule)
    — a bare pre-fill without user confirmation is NOT a stated dimension
    and will leave the gate closed.

    ``_release_history`` (internal): the issue #369 release pass runs only
    on the OUTERMOST call. The per-turn re-extractions it performs (the
    same-message explicit-beats-release check) pass ``False`` to stop the
    recursion.
    """
    out: dict[str, float] = {}

    # 1. Explicit stated_dims (highest priority — the caller parsed these).
    if stated_dims:
        for axis in DIMENSION_AXES:
            v = _coerce(stated_dims.get(axis))
            if v is not None:
                out[axis] = v

    # 2. Chat-text dimensions like "W: 42", "D is 30mm", "H = 20 mm",
    # plus a W×D×H triple ("60 × 45 × 80 mm" / "60x45x80mm" / "60mm x
    # 45mm x 20mm" — issue #275 task-a), plus an equal-axis size shorthand
    # ("a 20 mm cube" / "a 10mm box" / "a 15mm sphere") — the ONLY chat
    # text allowed to fill all three axes from ONE number, and ONLY when
    # the text also names an equal-axis shape (cube/box/sphere/ball: all
    # three edges equal), in explicit millimetres ("mm" required — a bare
    # "m"/meters must never be read as mm). A single number with no
    # equal-axis shape ("make a 20mm hole in the lid", "a 20mm tall vase",
    # "add a 5mm fillet", "mount a 6mm bolt", "a 3 m beam") is a FEATURE
    # or a one-axis measurement — filling three axes from it would
    # fabricate a part envelope the user never stated, the exact
    # fabricate-don't-measure anti-pattern ticket #91 removes: the gate
    # would then run against a wrong target (spurious FAIL, or worse,
    # spurious PASS), instead of abstaining (None) and leaving the gate
    # unmeasurable. A stated equal-axis shape is the only defensible
    # ground truth the loop can compare a rendered bbox against on a
    # bare "Create a 20mm cube" first turn (no latest version yet).
    # Precedence inside this pass: axis-prefixed form ("W: 42") >
    # W×D×H triple > shorthand — the triple only fills axes the axis pass
    # left empty, and the shorthand only fills axes the axis pass and the
    # triple left empty. A turn like "W is 30mm... make it a 20mm cube"
    # keeps the axis-prefixed value and never completes the triple from
    # the shorthand (the gate abstains rather than mixing sources within
    # one turn).
    if not all(a in out for a in DIMENSION_AXES):
        for idx, turn in enumerate(chat_history or []):
            if idx < len(chat_history) - QUOTED_UNMAPPED_MAX_MESSAGES:
                continue
            text = str(turn)
            # Issue #369: per axis, the NEWEST explicit stated value wins.
            # The loop walks oldest-first, so a value found here is
            # overwritten by any later turn's value; the first-wins lock is
            # gone. Within a single turn, the priority is: axis-letter cues
            # > triple > lexicon (the ticket's own priority order). Across
            # turns, the newest turn's values overwrite older ones.
            turn_axes: dict[str, float] = {}
            for axis in DIMENSION_AXES:
                m = re.search(
                    rf"\b{axis}\b\s*[:=]?\s*(\d+(?:\.\d+)?)"
                    rf"(?:{MM_UNIT_ALTERNATION})?\b",
                    text,
                    re.IGNORECASE,
                )
                if m:
                    v = _coerce(m.group(1))
                    if v is not None:
                        turn_axes[axis] = v
            # W×D×H triple: only fills axes the axis-letter pass left
            # empty in this turn.
            triple_axes, _ = _extract_triple(text)
            for axis, value in triple_axes.items():
                if axis not in turn_axes:
                    turn_axes[axis] = value
            # Single-axis-word lexicon: only fills axes the axis-letter
            # pass and the triple left empty in this turn.
            lexicon_axes = _classify_axis_cues(text)
            for axis, value in lexicon_axes.items():
                if axis not in turn_axes:
                    turn_axes[axis] = value
            # Equal-axis size shorthand (only if the other passes left
            # axes empty in this turn).
            if not turn_axes:
                m = re.search(
                    r"\b(?:a|an)\s+(\d+(?:\.\d+)?)\s*mm\b"
                    r"\s+(?:cube|box|sphere|ball)\b",
                    text,
                    re.IGNORECASE,
                )
                if m:
                    v = _coerce(m.group(1))
                    if v is not None:
                        turn_axes = {axis: v for axis in DIMENSION_AXES}
            # Merge this turn's axes into the global out (newest wins).
            out.update(turn_axes)

    # 3. AI-suggested dimensions, ONLY if the user confirmed them.
    if ai_suggested:
        confirmed_tokens = _confirmed_suggestion_tokens(chat_history or [])
        for axis, v in ai_suggested.items():
            if axis in out:
                continue
            key = f"suggested:{axis}"
            if key in confirmed_tokens or "suggested" in confirmed_tokens:
                cv = _coerce(v)
                if cv is not None:
                    out[axis] = cv

    # 4. Issue #369 release pass: a RELATIVE word for an axis in a NEWER
    #    message releases that axis — the axis's LATEST explicit statement
    #    must be OLDER than the releasing turn. A turn's own explicit
    #    statement beats the release ("make it taller, 20 mm" → H=20),
    #    mirroring ``effective_stated_dims``' existing absolute-over-release
    #    composition. The explicit ``stated_dims`` (step 1) and confirmed AI
    #    suggestions (step 3) are caller-structured ground truth and never
    #    release.
    from d33d.axis_lexicon import RELATIVE_WORDS

    if _release_history:
        # Track which turn set each axis's surviving value (newest wins).
        stated_at: dict[str, int] = {}
        for idx, turn in enumerate(chat_history or []):
            if idx < len(chat_history) - QUOTED_UNMAPPED_MAX_MESSAGES:
                continue
            text = str(turn)
            turn_axes = set(
                _extract_stated([text], None, None, _release_history=False)
            )
            for axis in turn_axes:
                stated_at[axis] = idx
        # Walk oldest-first: each turn's relative words release axes whose
        # latest statement is OLDER than this turn.
        for idx, turn in enumerate(chat_history or []):
            if idx < len(chat_history) - QUOTED_UNMAPPED_MAX_MESSAGES:
                continue
            low = str(turn).lower()
            relative_axes = {
                RELATIVE_WORDS[w] for w in RELATIVE_WORDS if re.search(
                    rf"(?<!\w){re.escape(w)}(?!\w)", low
                )
            }
            for axis in relative_axes:
                if axis in stated_at and stated_at[axis] < idx:
                    out.pop(axis, None)
    return out


def latest_stated_dims_dict(versions: Any, project_id: int) -> dict[str, float] | None:
    """The latest version's persisted per-axis ``stated_dims`` (a dict of
    positive values), or ``None`` — the carry-forward merge's input
    (issue #261). Read via ``latest_version`` (the row's column is
    already JSON-decoded to a dict); values are filtered to positive
    floats (``axes_to_gate_triple``'s cleaning rule — a 0.0 persisted
    axis is unconfirmed, never a carried statement).

    Moved here from ``d33d.projects`` (issue #261 fix batch) so it sits
    next to :func:`effective_stated_dims`, the helper that consumes it;
    the three call sites (chat ``post_chat``, finalize
    ``versions_routes`` and region edit ``app``) import it from here.
    """
    latest = versions.latest_version(project_id)
    if latest is None:
        return None
    raw = latest.get("stated_dims")
    if not isinstance(raw, dict):
        return None
    axes: dict[str, float] = {}
    for axis, value in raw.items():
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if f > 0:
            axes[str(axis)] = f
    return axes or None


def carried_stated_set(
    conn: Any, versions: Any, project_id: int
) -> dict[str, float] | None:
    """The project-level carried per-axis stated set (issue #312, task-a).

    The SINGLE READER for the carry-forward input that feeds all four
    production seams (chat ``post_chat``, finalize ``versions_routes``
    (both sites) and region edit ``app.create_region_edit``). Replaces
    :func:`latest_stated_dims_dict`'s version-row-only read with the
    project-level ``carried_stated_dims`` column (written at the end of
    every chat/finalize turn — pass or fail — so a failed turn's stated
    axes survive into the next successful version).

    Seed: when the column is NULL (a fresh project, or an existing DB
    migrated before this column landed), the latest version's
    ``stated_dims`` is used as the fallback (the pre-task-a behaviour).
    Returns ``None`` when neither source provides a set (the merge helper
    treats ``None`` as an empty carried set — the gate abstains).

    Values are filtered to positive floats (the same cleaning rule as
    :func:`latest_stated_dims_dict`).
    """
    row = conn.get_project(project_id)
    if row is not None:
        raw = row.get("carried_stated_dims")
        if isinstance(raw, str) and raw:
            try:
                loaded = json.loads(raw)
            except (ValueError, TypeError):
                loaded = None
            if isinstance(loaded, dict):
                axes: dict[str, float] = {}
                for axis, value in loaded.items():
                    try:
                        f = float(value)
                    except (TypeError, ValueError):
                        continue
                    if f > 0:
                        axes[str(axis)] = f
                return axes or None
        elif isinstance(raw, dict):
            axes = {}
            for axis, value in raw.items():
                try:
                    f = float(value)
                except (TypeError, ValueError):
                    continue
                if f > 0:
                    axes[str(axis)] = f
            if axes:
                return axes
    # Fallback (seed from the latest version row when the column is NULL
    # or empty — a fresh DB or an un-migrated project).
    return latest_stated_dims_dict(versions, project_id)


class CuesLike(Protocol):
    """Structural type for the lexicon cue object
    (``d33d.axis_lexicon.Cues``) — the ``cues`` input
    :func:`effective_stated_dims` accepts (declared shape instead of
    duck-typing via ``getattr``)."""

    absolute: dict[str, float]
    relative: set[str]
    global_: bool


def offer_tier_signals(
    user_message: str, chat_history: list[str] | tuple[str, ...] = ()
) -> tuple[set[str] | None, set[float] | None]:
    """The offer's tier-1/tier-2 signals for ONE turn (issue #261 fix
    batch — ONE helper for both seams: the chat pass and the finalize
    pass use the same computation, so tier 2 behaves the same on
    finalize as on chat — a user-quoted unmapped number from an EARLIER
    message (``chat_history``) is eligible on finalize, not only when it
    sits in the finalize message itself).

    Returns ``(released_axes, quoted_mm)``:

    * ``released_axes`` — the axes released by THIS message's
      relative/global cue (None when the message carries no release cue);
    * ``quoted_mm`` — the user-quoted unmapped mm numbers over
      ``(*chat_history, user_message)`` (never None).

    Either may degrade to None on a classification failure — the offer
    falls back to tier 3 (never a hard failure).
    """
    try:
        from d33d.axis_lexicon import classify

        released: set[str] | None = None
        quoted: set[float] | None = None
        cues = classify(user_message)
        if cues.relative or cues.global_:
            released = set(cues.relative) | ({"W", "D", "H"} if cues.global_ else set())
        quoted = user_quoted_unmapped_mm((*chat_history, user_message))
        return released, quoted
    except Exception:
        logger.debug("offer tier signals unavailable", exc_info=True)
        return None, None


def effective_stated_dims(
    latest_stated: dict[str, float] | None,
    cues: CuesLike | dict[str, float] | None = None,
) -> dict[str, float]:
    """The carry-forward merge helper (issue #261's operator decision —
    ONE function, THREE call sites: chat ``post_chat``, finalize
    (``versions_routes``) and region edit (``app.create_region_edit``)).

    The effective per-axis stated set for the NEW version row — and, at
    chat and finalize, the gate input (``axes_to_gate_triple``) — computed
    in one place, never two divergent copies:

    * the set STARTS as the latest version's persisted ``stated_dims``
      (axes confirmed on an earlier turn are NOT stale — the user's
      "add a hole" keeps H=12 stated; #247's no-fallback rule dies);
    * a RELATIVE cue on an axis RELEASES that axis ("make it taller" →
      the gate must not enforce the old H — the #247 regression);
    * a GLOBAL cue releases ALL three ("make it bigger");
    * an ABSOLUTE cue SETS that axis ("make it 12 mm tall" → H=12, even
      when the latest row carried nothing or a different value —
      "H: 20" after {H: 12} states 20, the cue overrides the carried
      value);
    * axes with no cue carry forward unchanged.

    ``cues`` is one of: a ``d33d.axis_lexicon.Cues`` (the chat route's
    current-message classification), an explicit ``dict[str, float]``
    (a caller's body-stated or protocol-extracted set — treated as an
    absolute statement: the caller's explicit set OVERRIDES the carried
    set, per the precedence "body field > explicit protocol cues >
    lexicon", and carries no release semantics), or ``None`` / an empty
    set (the region-edit call site: the carried set comes back unchanged,
    no release). Precedence of cue kinds: the explicit set wins over the
    lexicon's absolute; releases and overrides compose.
    """
    carried: dict[str, float] = {}
    if latest_stated:
        for axis, value in latest_stated.items():
            try:
                f = float(value)
            except (TypeError, ValueError):
                continue
            if f > 0:
                carried[str(axis)] = f

    released: set[str] = set()
    absolute: dict[str, float] = {}

    if isinstance(cues, dict):
        # Explicit set (body field / protocol extraction): an absolute
        # override statement — no release semantics (a "W: 42" body does
        # not release H).
        for axis, value in cues.items():
            f = _coerce(value)
            if f is not None:
                absolute[str(axis)] = f
    elif cues is not None:
        absolute = dict(getattr(cues, "absolute", None) or {})
        released = set(getattr(cues, "relative", None) or ())
        if getattr(cues, "global_", False):
            released.update(DIMENSION_AXES)

    effective = {axis: v for axis, v in carried.items() if axis not in released}
    for axis, value in absolute.items():
        f = _coerce(value)
        if f is not None:
            effective[axis] = f
    return effective


def stated_axes_from_message(
    message: str,
    chat_history: list[str] | tuple[str, ...] | None = (),
    explicit: dict[str, float] | None = None,
) -> dict[str, float]:
    """The PER-AXIS set of axes the user stated (issue #246) — a PARTIAL
    statement counts for the axes it states.

    The per-axis variant of :func:`stated_dims_from_message`: the SAME
    ``_extract_stated`` pipeline (never a new parser), returning whatever
    of W/D/H was stated — an empty dict when nothing was stated — instead
    of ``None`` for a partial statement. ``stated_dims_from_message``'s
    full-triple-or-``None`` contract is left intact for existing callers
    (the ``/chat`` route's bbox-gate target, the finalize seam's fallback);
    this function is the design-state seam's source of per-axis
    *evidence*: the persisted set names the axes the user actually said,
    and is what the design-state block's axis rows mark ``stated``.

    Note the deliberate asymmetry with the shorthand: an equal-axis shape
    ("a 20 mm cube") fills ALL THREE axes in ``_extract_stated`` — all
    three are then stated, which is honest (the text names a part with
    three equal 20 mm edges). A bare single number with no equal-axis
    shape fills NO axis — it is a feature size, not an envelope, and
    never becomes a stated axis row.
    """
    turns = [str(t) for t in (chat_history or ())] + [str(message)]
    stated = _extract_stated(turns, explicit, None)
    return {
        axis: float(stated[axis])
        for axis in DIMENSION_AXES
        if axis in stated
    }


def _classify_axis_cues(text: str) -> dict[str, float]:
    """The closed axis lexicon's absolute assignment for ONE message — the
    per-axis seam's floor (issue #314, operator decision: the
    single-axis-word path is part of the per-axis contract, and the
    triple/pair path alone cannot see a single-number statement such as
    "a 60 mm wide stand for a 100 × 70 mm phone", where the stand's own
    width is the part's stated axis). The mate connector rule applies
    here too (the lexicon's own mate-zone guard), and the triple path
    wins on conflicts (the explicit cue, per the ticket's precedence:
    "axis letter cues > triple > cube shorthand > lexicon"), so this
    only fills axes the triple left empty.
    """
    from d33d.axis_lexicon import classify

    return dict(classify(text).absolute)


def stated_dims_from_message(
    message: str,
    chat_history: list[str] | tuple[str, ...] | None = (),
    explicit: dict[str, float] | None = None,
) -> tuple[float, float, float] | None:
    """The complete (W, D, H) triple stated in a chat turn, or ``None``.

    Ticket #91's dimension source for the ``/chat`` caller path: the user's
    own words, via the existing :func:`_extract_stated` pipeline (never a
    new parser). ``message`` is the current turn (the newest text — the
    extraction walks history oldest-first, so it must sit last); prior turns
    are ``chat_history``; ``explicit`` is a caller-supplied dimension map
    (the request's ``stated_dims`` field, keyed by axis) that the extraction
    already ranks highest priority.

    Returns ``None`` — NOT a zero triple — when fewer than all three axes
    are stated. A caller that receives ``None`` must treat the bbox gate as
    unmeasurable (abstain), never fabricate ``0.0`` targets (an unsatisfied
    ``target <= 0`` hard-fail, ticket #91's original bug).
    """
    turns = [str(t) for t in (chat_history or ())] + [str(message)]
    stated = _extract_stated(turns, explicit, None)
    if all(axis in stated for axis in DIMENSION_AXES):
        return (
            float(stated["W"]),
            float(stated["D"]),
            float(stated["H"]),
        )
    return None


def _last_user_turn(chat_history: list[str]) -> str:
    """The most recent turn in the history ('' for an empty history).

    The confirmation contract (see :func:`_confirmed_suggestion_tokens` and
    :func:`_extract_fit_type`) is deliberately narrow: only the LAST turn
    is scanned, so a stray "yes" or "snap" from earlier in the conversation
    can never silently promote an AI suggestion into ground truth used for
    physical part-fit tolerances.
    """
    if not chat_history:
        return ""
    return str(chat_history[-1])


def _is_clean_affirmation(
    text: str, *, fit_keyword_is_affirmative: bool = False
) -> bool:
    """True if ``text`` is a SHORT, unambiguous affirmative response.

    This is a deliberately conservative heuristic soft-gate, NOT a
    confirmation-intent classifier: it only accepts turns that (b) are
    short enough to be a plain acceptance (under ~20 words — a long turn
    that merely contains the word "yes" is more likely conversational),
    and (c) carry none of the disqualifying signals: a question mark (a
    question is not a confirmation), negation words (no/not/...), or a
    hedging/confrontation marker (but/why/because/instead/although).

    ``fit_keyword_is_affirmative=False`` (the default) additionally requires
    an explicit affirmative token (yes/confirm/ok/correct/...); with it True,
    a turn naming the fit-type keyword itself is admitted as affirmative
    (a clean "snap fit" or "it's a slip fit" IS the answer to the fit-type
    question — no "yes" needed). In that mode only, a turn that IS the tight
    no-fit phrase ("no fit" / "no-fit" / "nofit" — no other tokens, and no
    "don't"/"nope"/"nah"/"wrong"-style negation token anywhere in the turn)
    is admitted BEFORE the generic negation check, so the valid ``no_fit``
    value is selectable despite the "no" in that set; the base path and any
    longer sentence stay closed for it.

    The known residual risk — a short turn that happens to be affirmative
    yet not responsive to the pending suggestion — is honestly documented
    as residual: callers building a real UI confirmation flow should prefer
    the explicit structured confirmation signal the protocol already
    exposes — the ``stated_dims`` parameter (including its ``fit_type``
    key) — over this text heuristic, rather than relying on this layer
    alone.
    """
    tokens = re.findall(r"[a-z']+", text.lower())
    if not tokens or len(tokens) > 19:
        return False
    if "?" in text:
        return False
    if fit_keyword_is_affirmative:
        # The ``no_fit`` enum value, spoken, is the phrase "no fit" itself —
        # the valid answer to the fit-type question, not a negation. Admit
        # the TIGHT phrase form only (exactly the two tokens no+fit, before
        # the generic "no" disqualifier); any extra word or any other
        # negation token ("don't", "nope", "wrong", ...) keeps the turn out.
        phrase = re.sub(r"\s+", " ", text.strip().lower())
        phrase = phrase.strip(".?! ")
        phrase_nohyphen = phrase.replace("-", "")
        if phrase_nohyphen in {"no fit", "nofit"} and not (
            set(tokens) & {"don't", "dont", "nope", "nah", "wrong", "incorrect"}
        ):
            return True
    if set(tokens) & {
        "no",
        "not",
        "nope",
        "nah",
        "wrong",
        "incorrect",
        "reconsider",
    }:
        return False
    if set(tokens) & {"but", "why", "because", "instead", "although", "however"}:
        return False
    if re.search(
        r"\b(confirm|confirmed|yes|yep|correct|right|accept|ok|okay)\b",
        text,
        re.IGNORECASE,
    ):
        return True
    if fit_keyword_is_affirmative:
        return bool(
            re.search(r"\b(slip|press|interference|snap)\b", text, re.IGNORECASE)
        )
    return False


def _confirmed_suggestion_tokens(chat_history: list[str]) -> set[str]:
    """Tokens the user used to confirm AI suggestions.

    CONFIRMATION CONTRACT (tightened twice — see
    :func:`_is_clean_affirmation`): only the MOST RECENT chat turn is
    scanned (never the full history), and that turn must be a SHORT,
    unambiguous affirmative response — containing an affirmative token, no
    question mark, no negation (no/not/...), and no hedging marker
    (but/why/...). A turn like "Yes, but why did you pick 40 and not 50?"
    is a QUESTION about the suggestion, not an acceptance of it, and must
    NOT promote the AI pre-fill into ground truth. A stray confirmatory
    token in an earlier, unrelated turn has NO effect. In particular a
    "no fit" turn — an affirmative for the fit-type question (base mode
    never admits it) — is NOT a suggestion confirmation.

    This is a heuristic soft-gate, not a full confirmation-intent
    classifier: a short affirmative turn that is not actually responsive
    to the pending suggestion is a known residual risk. Callers building a
    real UI confirmation flow should prefer the explicit structured
    confirmation signal the protocol already exposes — the ``stated_dims``
    parameter — over this text heuristic.
    """
    tokens: set[str] = set()
    text = _last_user_turn(chat_history)
    if not text:
        return tokens
    if not _is_clean_affirmation(text):
        return tokens
    tokens.add("suggested")
    for axis in DIMENSION_AXES:
        if re.search(rf"\b{axis}\b", text, re.IGNORECASE):
            tokens.add(f"suggested:{axis}")
    return tokens


def _extract_fit_type(
    chat_history: list[str], stated_dims: dict[str, Any] | None
) -> FitType | None:
    """The fit type the user stated in chat or via ``stated_dims``
    (``fit_type``/``fit`` key). None if not yet stated (the agent must ask).

    CONFIRMATION CONTRACT (tightened twice — see
    :func:`_is_clean_affirmation`): only the MOST RECENT chat turn is
    scanned for a fit-type keyword — never the full history — and that
    turn must be a SHORT, unambiguous affirmative response: no question
    mark, no negation (no/not/...), no hedging marker (but/why/...). A
    "snap" (or "slip"/"press"/"interference") in a questioning or negating
    turn (e.g. "that's a snap decision, why would you choose snap fit?")
    must NOT flip the fit type; the fit type is what the user says in a
    clean acceptance in response to the agent's proactive fit-type
    question. The tight "no fit" / "no-fit" / "nofit" phrase is the
    ``no_fit`` answer itself — it takes precedence over the "no" negation
    word (checked before it in :func:`_is_clean_affirmation`), which is
    what makes the ``no_fit`` enum value reachable in natural language.

    As with dimension confirmation, this is a heuristic soft-gate, not a
    full intent classifier: a real UI confirmation flow should prefer the
    explicit structured confirmation signal the protocol already exposes —
    the ``stated_dims`` parameter (including its ``fit_type`` key) — over
    this text heuristic.
    """
    if stated_dims:
        raw = stated_dims.get("fit_type") or stated_dims.get("fit")
        if isinstance(raw, str) and raw.strip().lower() in _FIT_TYPE_SET:
            return raw.strip().lower()  # type: ignore[return-value]
    text = _last_user_turn(chat_history)
    if not text or not _is_clean_affirmation(text, fit_keyword_is_affirmative=True):
        return None
    low = re.sub(r"\s+", " ", text.strip().lower()).strip(".?! ")
    # The tight no-fit phrase is the ``no_fit`` answer; the loose
    # "no...fit" match would also fire on "no, that fit is wrong" and is
    # deliberately NOT admitted here.
    if low.replace("-", "") in {"no fit", "nofit"}:
        return "no_fit"
    for fit in ("slip", "press", "interference", "snap"):
        if re.search(rf"\b{fit}\b", low):
            return fit  # type: ignore[return-value]
    return None


def resolution_questions(
    missing_axes: tuple[str, ...], fit_type: FitType | None
) -> tuple[str, ...]:
    """The clarifying questions the agent must ask before any ``.scad`` is
    generated — one per missing W/D/H axis, plus the proactive fit-type
    question if no fit type has been stated."""
    questions: list[str] = []
    if missing_axes:
        axes = ", ".join(missing_axes)
        questions.append(
            f"Please state the {axes} dimension(s) in millimetres (caliper "
            "entry or a dimension line on the photo)."
        )
    if fit_type is None:
        questions.append(
            "What fit type is intended? (slip, press, interference, snap, "
            "or no-fit) — I apply the matching FDM clearance as a named "
            "parameter, never hard-coded."
        )
    return tuple(questions)


def require_dimensions_confirmed(
    chat_history: list[str],
    stated_dims: dict[str, Any] | None,
    *,
    ai_suggested: dict[str, float] | None = None,
    part_scale: float | None = None,
    part_bbox_mm: tuple[float, float, float] | None = None,
) -> DimensionClarification:
    """The CLARIFY-before-code gate.

    Returns a :class:`DimensionClarification`. ``confirmed`` is True ONLY if
    ALL three of W/D/H are stated in mm AND a fit type has been chosen.
    Otherwise ``confirmed`` is False and ``questions`` holds the exact
    clarifying questions to ask — the design loop MUST ask these (and must
    NOT emit any ``.scad``) until the user's answers flip the gate to True.

    An ``ai_suggested`` pre-fill is surfaced (in ``suggested``) and only
    counts toward confirmation when the user confirms it in the chat — it
    is a confirmable suggestion, never the source of truth for a
    fit-critical number. Surfaces are run through the same ``_coerce``
    helper as the stated-dimension path, so a malformed LLM-derived
    pre-fill (non-numeric / None) degrades to a missing suggested entry
    rather than raising.
    """
    stated = _extract_stated(chat_history, stated_dims, ai_suggested)
    fit_type = _extract_fit_type(chat_history, stated_dims)

    missing = tuple(a for a in DIMENSION_AXES if a not in stated)
    suggested: dict[str, float] = {}
    for a in DIMENSION_AXES:
        if ai_suggested and a in ai_suggested:
            cv = _coerce(ai_suggested[a])
            if cv is not None:
                suggested[a] = cv

    # Issue #332 (sub-issue 3) — the settled/assumed part's W/D/H are
    # MEASURED ground truth (the v1's mm bbox, file bbox × scale, as
    # recorded): the gate does NOT ask the user to state them ("don't
    # ask what the mesh already measures"). The part's extent fills the
    # missing axes (only the axes the user has not stated — a stated
    # value for a NEW feature, or a correction, is never overridden);
    # ``fit_type`` falls to the default ``no_fit`` (the part's fit is the
    # mesh's, not a new interface). The gate closes on the part's
    # measured W/D/H + the default fit — the design loop runs on the
    # part's ground-truth baseline, not on a fabricated user statement.
    # ``None`` part (no part, or an unsettled part — the unsettled
    # pre-route stops the loop before this gate runs) leaves the
    # behaviour verbatim (the byte-identity regression anchor).
    if missing and part_bbox_mm is not None:
        part = tuple(
            float(part_bbox_mm[i]) for i in range(len(DIMENSION_AXES))
        )
        for axis, value in zip(DIMENSION_AXES, part):
            if axis in missing and value > 0:
                stated[axis] = value
        missing = tuple(a for a in DIMENSION_AXES if a not in stated)
        if fit_type is None:
            fit_type = "no_fit"

    if missing or fit_type is None:
        return DimensionClarification(
            confirmed=False,
            questions=resolution_questions(missing, fit_type),
            suggested=suggested,
        )

    tol = resolve_tolerance_mm(fit_type)
    params = {a: stated[a] for a in DIMENSION_AXES}
    return DimensionClarification(
        confirmed=True,
        params=params,
        fit_type=fit_type,
        tolerance_mm=tol,
        suggested=suggested,
    )


def emit_named_params(
    clarification: DimensionClarification,
) -> dict[str, str]:
    """The confirmed dimensions + resolved FDM tolerance as a NAMED
    parameter map ``{param_name: value}`` — the shape the ``.scad``
    generator interpolates as ``param_name = value;`` declarations.

    Keys are the dimension axes (W/D/H) plus ``tolerance_mm`` and
    ``fit_type``. Values are strings in ``:g`` form so the generator can
    emit them verbatim into a parameter block. Raises if the clarification
    is not confirmed (the gate must pass before any params are emitted).
    """
    if not clarification.confirmed:
        raise ValueError("cannot emit named parameters before dimensions are confirmed")
    out = {a: f"{clarification.params[a]:g}" for a in DIMENSION_AXES}
    out["tolerance_mm"] = f"{clarification.tolerance_mm:g}"
    out["fit_type"] = clarification.fit_type
    return out
