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

Issue #393: the statement-extraction half (``_extract_stated`` + helpers,
the per-message wrappers, the carry-forward readers/merge, and
``resolve_stated_cues``) lives in :mod:`d33d.statement_extraction`; this
module keeps the clarification/gate/fit half and imports the extraction
surface from there. The dependency edge is strictly
``axis_lexicon`` -> ``triple_extraction`` -> ``statement_extraction`` ->
``dimension_protocol`` (never back). ``DIMENSION_AXES`` is re-exported
from :mod:`d33d.statement_extraction` (the extraction half is the primary
user); the historical ``from d33d.dimension_protocol import DIMENSION_AXES``
importers keep working unchanged.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any, Literal

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
    "DimensionClarification",
    "FitType",
    "_extract_triple",
    "_mm_cue_values",
    "_triple_suppressed_by_feature_noun",
    "emit_named_params",
    "offer_tier_signals",
    "require_dimensions_confirmed",
    "resolution_questions",
    "resolve_tolerance_mm",
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
#: naming so the named-parameter structure produced here is directly
#: usable. Defined here (the clarification/gate half owns the closed
#: protocol constants); :mod:`d33d.statement_extraction` imports it for
#: the extraction pass (issue #393) — the import graph stays strictly
#: unidirectional: ``axis_lexicon`` -> ``triple_extraction`` ->
#: ``dimension_protocol`` -> ``statement_extraction`` (never back).
DIMENSION_AXES: tuple[str, ...] = ("W", "D", "H")

# Late import (issue #393): the statement-extraction half now lives in
# :mod:`d33d.statement_extraction`; it needs ``DIMENSION_AXES`` above and
# must not import this module, so the edge is one-way (dimension_protocol
# -> statement_extraction).
from d33d.statement_extraction import (
    _coerce,
    _extract_stated,
    _is_clean_affirmation,
    _last_user_turn,
)

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
            released = set(cues.relative) | (set(DIMENSION_AXES) if cues.global_ else set())
        quoted = user_quoted_unmapped_mm((*chat_history, user_message))
        return released, quoted
    except Exception:
        logger.debug("offer tier signals unavailable", exc_info=True)
        return None, None


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
