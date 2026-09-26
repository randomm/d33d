"""Bounded iterate-and-score design loop (ticket #5, task-loop workstream).

The core agent loop: reference photo + chat + stated mm dimensions in, a
parametric OpenSCAD candidate out — improved by a bounded render-then-score
cycle. This module owns *orchestration only*; the LLM edge
(``d33d.design_llm``), the failure classifier (``d33d.failure_classes``),
the render contract (``d33d.render_worker``) and the role->model resolution
(``d33d.config.resolve``) are injected or imported, never re-implemented.

Loop contract (from 04-design-loop.md, the spec of record):

* **Cap 3 auto-iterations.** The cap counts *candidate generations*
  (render + score), so a compile-repair attempt counts against it — a model
  that keeps emitting trailing-semicolon bugs cannot loop unboundedly by
  hiding behind "it failed before the iteration".
* **Stop conditions**: the deterministic-gate score hits its maximum
  (validation passes at the render-worker gate level, v1 — the 3MF pipeline
  is a later hook, not a blocker), or the cap is reached, or two
  consecutive iterations show no improvement.
* **Best, never last.** On exhaustion the loop returns the
  BEST-scoring candidate seen across all iterations plus a *structured*
  failure reason — never silently the last attempt.
* **Compile failure is repair input, never terminal.** A non-``ok``
  ``error_class`` is routed through ``d33d.failure_classes`` to a tagged
  class fed back into the next iteration; the render-worker classes that
  are not LLM-addressable (``timeout``/``oom``/``container_error``) are
  non-improving steps and are NOT fed back to the LLM as repair.
* **Improvement metric is a named, unit-testable predicate.** The pinned
  score is a gate bitvector rank: ``score(render, stated_dims)`` is the
  popcount of the gate vector, with a defined tiebreak on the raw
  bitvector. "No improvement" is the named predicate :func:`no_improvement`
  — non-increase of that rank across two consecutive iterations.

Dimension handling: the stated dimensions are carried as *named parameters*
(W/D/H in the design prompt, threaded to the render as ``defines`` via the
render worker's ``-Dname=value`` channel), never baked into geometry
expressions. If the LLM's response already declares a named-parameter
block, the patch-application path PRESERVES it verbatim (no stripping, no
re-templating); the named-parameter gate in the score then verifies the
stated values appear only in declarations.

Role aliases: every logical LLM call is addressed by ROLE (``design`` /
``critique`` / ``classification``), never by model id. The loop calls the
injected ``llm_fn(role, messages, system)`` — and when the caller wants the
role->model resolution inside the loop, :func:`make_llm_fn` builds that
closure over ``d33d.config.resolve.resolve_model`` + ``d33d.design_llm.send``
for them (the same dependency-injection seam ``d33d.render_worker`` uses).
Each call's ``LLMResult.prompt_hash`` (``d33d.prompt_hash.canonical_hash``)
is surfaced per call through the optional ``log`` callback so the caller
owns the request_logs write (this module does no DB I/O).
"""

from __future__ import annotations

import asyncio
import functools
import logging
import re
import subprocess
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from d33d.config.catalogue import Catalogue
from d33d.config.probes import CapabilityResult
from d33d.config.resolve import resolve_model
from d33d.design_llm import LLMResult, send
from d33d.failure_classes import (
    REPAIRABLE_CLASSES,
    ClassifiedFailure,
    classify_failure,
    detect_magic_numbers,
    route_repair,
)
from d33d.render_worker import RenderResult

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ITERATIONS",
    "NO_IMPROVEMENT_LIMIT",
    "RENDERER_PREFLIGHT_CACHE_SECONDS",
    "RENDERER_UNAVAILABLE",
    "BboxInfo",
    "DesignResult",
    "IterationRecord",
    "Score",
    "_axis_param_mismatches",
    "extract_named_params",
    "is_best",
    "make_llm_fn",
    "no_improvement",
    "renderer_is_available",
    "reset_renderer_preflight_cache",
    "run_design_loop",
    "run_design_loop_async",
    "scad_looks_valid",
    "scad_title",
    "score",
]

#: The auto-iteration cap (spec: "capped at 3 auto-iterations"). The cap
#: counts candidate generations, so compile-repair attempts count against
#: it — documented here so a refactor cannot quietly re-scope it to
#: critique-only iterations.
MAX_ITERATIONS = 3

#: Two consecutive iterations with a non-increasing score stop the loop
#: early (spec stop condition 3).
NO_IMPROVEMENT_LIMIT = 2

#: The loop-level (NOT an error_class) failure reason emitted when the
#: renderer pre-flight check finds the Docker daemon unreachable before
#: the first iteration (issue #277). The run ends at once with this reason
#: and NO LLM call.
RENDERER_UNAVAILABLE = "renderer_unavailable"

#: How long a SUCCESSFUL renderer pre-flight check is trusted before the
#: next design-loop run re-checks (per-process cache; issue #277 operator
#: decision). A FAILED check is NEVER cached — the next run re-probes, so
#: a Docker start within the window is not masked by a stale failure.
RENDERER_PREFLIGHT_CACHE_SECONDS = 30.0

#: The wall-clock bound on the ``docker info`` probe's ``subprocess.run``
#: (issue #277: "a short timeout (≤ 5 s)").
_PREFLIGHT_PROBE_TIMEOUT_S = 5.0

#: The per-process cache of the last SUCCESSFUL pre-flight probe: a
#: timestamp (``time.monotonic``) or ``None`` (never cached / reset).
_preflight_success_at: float | None = None

#: Tolerance for the bbox gate: max(1% of the stated size, 0.5 mm) per
#: axis (v1 FINALIZE gate — render-worker-level, per the issue's proposed
#: resolution of the open question).
BBOX_TOLERANCE_REL = 0.01
BBOX_TOLERANCE_MIN_MM = 0.5

Roles = Literal["design", "critique", "classification"]

#: ``render_fn(scad_source, defines) -> RenderResult`` (sync or async).
RenderFn = Callable[[str, dict[str, str]], "RenderResult | Awaitable[RenderResult]"]
#: ``llm_fn(role, messages, system) -> LLMResult`` (sync or async).
LLMFn = Callable[
    [str, list[dict[str, Any]], str | None], "LLMResult | Awaitable[LLMResult]"
]
#: ``log(role, prompt_hash, status) -> None`` — the caller's request_logs hook.
LogFn = Callable[[str, str, str], None]
#: ``bbox_fn(render) -> BboxInfo | None`` — per-axis extents from a render.
BboxFn = Callable[[RenderResult], "BboxInfo | None"]
#: ``on_progress(kind, payload) -> None`` — the caller's per-render arrival
#: hook (issue #121). ``kind`` is ``"view-start"`` or ``"view-done"``
#: (``view-failed`` is NOT delivered — a failed view must not report as
#: complete), ``payload`` is a dict carrying at least ``view`` (the view
#: stem, e.g. ``"view_00_front"``) and ``iteration`` (the design-loop
#: iteration index, 1-based). The hook fires from the render subprocess's
#: stderr-drain thread (a worker thread, NOT the event loop) while the
#: container is still running; it must therefore be fast and side-effect-
#: free (it may enqueue a future onto the app's loop but must not block).
OnProgressFn = Callable[[str, dict[str, Any]], None]


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BboxInfo:
    """Per-axis rendered extents (mm) plus volume — the gate inputs.

    ``x``/``y``/``z``/``volume`` are the WHOLE-ASSEMBLY extents (the union
    bounding box of everything the render produced). ``components`` is an
    optional per-component breakdown — extents + volume of each watertight
    connected component of the mesh after ``merge_vertices`` +
    ``split(only_watertight=True)`` (issue #100: a multi-part request like
    "a 20mm cube with a 10mm sphere beside it" renders one STL holding
    several disjoint bodies, and comparing the whole-assembly bbox against
    the single stated triple made the gate unsatisfiable by construction).
    The bbox gate compares the confirmed set against the BEST-MATCHING
    component only for a FULL positive (W, D, H) triple and a non-empty
    ``components`` breakdown (issue #247: with a PARTIAL confirmed set
    there is no well-defined component selection, so a partial triple
    compares its confirmed axes against the whole-mesh extents instead);
    when ``components`` is empty the gate takes the whole-part path
    exactly as before, so every existing caller that builds
    ``BboxInfo(x, y, z, volume)`` sees byte-for-byte the old behaviour
    (an empty breakdown means "no component data", never "one
    component").
    """

    x: float
    y: float
    z: float
    volume: float = 0.0
    #: Per-component ``(x_extent, y_extent, z_extent, volume, min_x, min_y,
    #: min_z)`` tuples from the mesh's watertight connected components
    #: (issue #100). Empty = whole-part comparison (the legacy path).
    components: tuple[tuple[float, float, float, float, float, float, float], ...] = ()


@dataclass(frozen=True)
class Score:
    """The pinned improvement metric: a gate bitvector ranked by popcount.

    ``bits`` is the 5-tuple ``(ok, non_blank_views, bbox, named_params,
    axis_params_match)``.
    ``rank`` is the popcount — the monotone-comparable value the loop uses
    for stop conditions. ``tiebreak`` is the raw bitvector: two candidates
    with equal rank compare on it (earlier bits weigh more), so "best" is
    never ambiguous and the metric is unit-testable in isolation.
    """

    bits: tuple[bool, bool, bool, bool, bool]
    rank: int
    tiebreak: tuple[bool, bool, bool, bool, bool]
    #: True iff the bbox bit is True and ANY stated axis is unknown
    #: (``<= 0``) and the gate ABSTAINED on it (ticket #91) — including
    #: partial triples where the known axes happen to match: an unknown
    #: axis was never measured, so the pass must carry the flag even when
    #: every measured axis passed. Never True on an all-known triple.
    #: Per-axis (issue #247): a partially confirmed run (e.g. only H
    #: confirmed, H passing) carries the flag — it means "not every axis
    #: was checked", and is False only for a fully confirmed, fully
    #: measured pass.
    bbox_abstained: bool = False

    @property
    def ok(self) -> bool:
        return self.bits[0]

    @property
    def non_blank_views(self) -> bool:
        return self.bits[1]

    @property
    def bbox_within_tolerance(self) -> bool:
        return self.bits[2]

    @property
    def named_params(self) -> bool:
        return self.bits[3]

    @property
    def axis_params_match(self) -> bool:
        return self.bits[4]

    @property
    def perfect(self) -> bool:
        """All five gates pass (v1 FINALIZE's "validation passes")."""
        return self.rank == len(self.bits)


@dataclass(frozen=True)
class IterationRecord:
    """One candidate generation (LLM design call + render + score)."""

    iteration: int
    scad_source: str
    #: The render that produced this record. ``None`` ONLY for the synthetic
    #: pre-flight :data:`RENDERER_UNAVAILABLE` result (issue #277) — no
    #: render ever ran; the loop-level placeholder record carries no render
    #: data rather than a fabricated one. Every record built on the normal
    #: iteration path carries a real :class:`RenderResult`.
    render: RenderResult | None
    #: The score for this record. ``None`` ONLY for the synthetic pre-flight
    #: :data:`RENDERER_UNAVAILABLE` record (issue #277) — nothing was
    #: scored because nothing rendered. Every normally-built record carries
    #: a real :class:`Score`.
    score: Score | None
    #: Failure class routed from the render (None for a clean render).
    failure_class: str | None = None
    #: The structured repair dict fed to the NEXT iteration (None when the
    #: failure is non-repairable or there is no failure) — the tagged
    #: class, never a raw stderr dump.
    repair: dict[str, Any] | None = None
    #: The ``role -> prompt_hash`` pairs this iteration produced.
    prompt_hashes: dict[str, str] = field(default_factory=dict)
    #: The per-axis rendered extents this iteration's render measured to
    #: (issue #137: the caller's ``bbox_fn`` output, ``None`` when the
    #: render produced no measurement). Declared — never a duck-typed
    #: read (issue #93's precedent): the version write path persists the
    #: BEST candidate's measurement from this field, and a stub loop
    #: result without it simply carries ``None`` (a missing field is not
    #: a fabricated measurement).
    bbox: BboxInfo | None = None
    #: The named dimension assignments THIS candidate's generated SCAD
    #: actually declares (issue #219): the ``name -> float`` dict from the
    #: shared extraction helper :func:`extract_named_params`, computed over
    #: the record's own ``scad_source``. This is a RECORD of what was built,
    #: not a restatement of the caller's input — when a name appears in
    #: both the SCAD and the caller-stated dimensions, the SCAD value WINS
    #: (the persisted dict is the SCAD extraction, not a merge with
    #: ``_dim_params``). Values are always ``float`` (the downstream
    #: readers ``float(params.get(axis, 0.0))``). A SCAD that declares no
    #: matching ``name = number;`` line persists ``{}`` — honest reporting
    #: of an actual absence. (Issue #93's caller-stated-params semantics —
    #: the render's ``_dim_params`` defines map — are superseded for the
    #: persisted record by this SCAD extraction; the render's ``defines``
    #: channel itself is unchanged and stays caller-sourced.)
    params: dict[str, Any] = field(default_factory=dict)
    #: The model's per-parameter metadata THIS candidate's design-role
    #: reply carried (issue #248): the ``name -> {label?, unit?, axis?,
    #: reason?}`` dict from :func:`extract_param_meta` over the LLM
    #: result that produced this record. Metadata is the model's own
    #: words about the parameters it declared — the values stay sourced
    #: from the SCAD (``params`` above); the caller's write path
    #: persists this as the version row's ``param_meta`` (``{}`` when the
    #: model emitted no array — a missing field is honest absence, never
    #: a fabricated label).
    param_meta: dict[str, Any] = field(default_factory=dict)
    #: The param the model flagged as MOST FIT-CRITICAL to confirm
    #: (issue #250): the ``confirm_first`` field of the design-role reply
    #: (the fenced-JSON / tool-call ``arguments.confirm_first``), or
    #: ``None`` (the model offered no flag — the selection rule then has
    #: only the declared-axis branch). The adapter VALIDATES the name
    #: against the version's own params before offering (a name that is
    #: not an assumed numeric param of the version is rejected — never a
    #: guessed fallback); the record only carries the model's raw flag.
    confirm_first: str | None = None
    #: The model's own OFFER SENTENCE for the flagged param (issue
    #: #250): the ``confirm_sentence`` field of the design-role reply,
    #: or ``None``. The adapter accepts it only when it names the chosen
    #: param's value AND every number in it appears in the design-state
    #: block (the issue #249 number guard); anything else falls to the
    #: deterministic template. The record carries the raw text — the
    #: guard runs server-side, never in the model.
    confirm_sentence: str | None = None


@dataclass(frozen=True)
class DesignResult:
    """The loop's terminal outcome.

    ``status`` is ``'pass'`` (a candidate passed every deterministic gate)
    or ``'exhausted'`` (the 3-iteration cap or the no-improvement stop fired
    without a full pass). ``best`` is the BEST-scoring candidate across all
    iterations (never silently the last attempt); ``failure_reason`` is one
    of the structured classes in :data:`GATE_REASON_BITS` or a render-worker
    class — never free text.
    """

    status: str
    best: IterationRecord
    iterations: tuple[IterationRecord, ...]
    failure_reason: str | None = None
    iterations_used: int = 0


#: Structured failure reasons for an exhausted loop, in bit order — each
#: names the weakest gate of the best candidate so the caller can surface
#: "here is the best + why it failed" without parsing prose.
GATE_REASON_BITS: tuple[str, ...] = (
    "error_class_not_ok",
    "views_blank_or_missing",
    "bbox_out_of_tolerance",
    "stated_dims_not_named_parameters",
    "axis_params_mismatch",
)


# ---------------------------------------------------------------------------
# The improvement metric — named and independently unit-testable
# ---------------------------------------------------------------------------


def best_match_component(
    bbox: BboxInfo, stated: tuple[float, float, float]
) -> tuple[float, float, float] | None:
    """The stated triple's BEST-MATCHING component extents, or ``None``.

    Returns ``None`` when the ``BboxInfo`` carries no component breakdown
    (the legacy whole-part case). Otherwise sorts the components by the
    TOTAL ordering ``(per-axis-difference ASC, volume DESC, min_x ASC,
    min_y ASC, min_z ASC)`` and returns the first's extents (issue #100,
    PM decision 5). The ordering is a total order defined purely on each
    component's own measurements — it is independent of the order
    ``trimesh.split()`` happens to return components in (that order varies
    between merged/unmerged loads of the same bodies), so the selection is
    deterministic across repeated runs and across component orderings.

    The per-axis difference sums over ALL THREE axes — the caller is
    contractually passed only a FULL POSITIVE (W, D, H) triple (issue
    #247: :func:`_bbox_within_tolerance` compares a PARTIAL confirmed set
    against the whole-mesh extents instead, so a component match is only
    ever asked for when every axis is confirmed). ``None`` is never returned
    for a non-empty breakdown, so no ``continue`` sentinel is needed.

    Volume here is only a TIE-BREAKER, never a conformance signal: for
    intersecting/contained shells ``split()`` double-counts the overlap in
    component volumes, so a volume comparison is not a size comparison.
    """
    if not bbox.components:
        return None
    ranked = sorted(
        bbox.components,
        key=lambda c: (
            sum(abs(e - t) for e, t in zip(c[:3], stated)),
            -(c[3] if len(c) > 3 else 0.0),
            c[4] if len(c) > 4 else 0.0,
            c[5] if len(c) > 5 else 0.0,
            c[6] if len(c) > 6 else 0.0,
        ),
    )
    return tuple(ranked[0][:3])


def _bbox_within_tolerance(bbox: BboxInfo, stated: tuple[float, ...]) -> bool:
    """True iff every rendered axis the user CONFIRMED is within
    max(1%, 0.5 mm) of its confirmed dimension (order x, y, z).

    ``stated`` is the per-axis confirmed set normalized into the triple
    (issue #247): an axis the user did NOT confirm is ``<= 0`` in the
    triple and is SKIPPED (per-axis abstention) — an unconfirmed axis is
    not a measurement target, so it neither fails nor passes the gate.
    The gate ABSTAINS entirely (returns True without measuring) only when
    NO axis is confirmed; it is the caller (:func:`score`) that records
    the abstention DISTINCTLY in ``Score.bbox_abstained`` — an abstained
    pass is never a vacuous, unmarked one.

    Issue #100 — multi-part meshes: when ``bbox.components`` is non-empty
    the confirmed set is compared against the BEST-MATCHING component
    (the component whose extents are closest to the confirmed axes — see
    :func:`best_match_component`) instead of the whole-assembly extents:
    a stated single-body triple can never match a whole-assembly bbox that
    contains additional bodies, so the whole-assembly comparison made any
    multi-part request unsatisfiable by construction.

    Documented choice (issue #247): :func:`best_match_component` needs a
    FULL (W, D, H) confirmed triple to rank components — with a PARTIAL
    confirmed set there is no well-defined "best-matching component" (the
    ranking would compare against axes the user never confirmed), so a
    partial set compares its confirmed axes against the WHOLE-MESH extents
    instead. Consequence: a user who confirms only H on a two-body design
    gets the gate measured against the union bbox's z — a component whose
    own z fits can still fail when the assembly's z does not. That is the
    conservative (fail-safe) direction: the assembly can only be LARGER
    than the part, so a whole-mesh comparison cannot under-report a
    confirmed-axis error.

    Degradation is explicit (issue #100): a mesh that splits to exactly
    one watertight component compares that component against the confirmed
    axes — which is byte-for-byte the whole-part comparison for a single
    body (the single component IS the assembly). Fused/intersecting
    geometry that OpenSCAD's CSG merged into one shell also yields one
    component and therefore behaves the same: the gate measures the union
    extents, not a per-feature decomposition. A mesh that splits to ZERO
    components (a genuinely broken/non-watertight mesh) fails the gate —
    a vacuous pass here would repeat the exact defect class issue #84
    removed, so a zero-component split is never treated as "no bodies,
    nothing to measure". That case is reachable only through the
    production ``bbox_from_render`` seam (which sets ``components``);
    test-built ``BboxInfo``s without a breakdown keep the legacy whole-part
    comparison.
    """
    # Normalize to a 3-tuple with ``0.0`` for any absent axis (per-axis
    # semantics — issue #247): callers pass the per-axis confirmed set as
    # either a full 3-tuple (legacy) or a shorter tuple naming only the
    # CONFIRMED axes in W→x, D→y, H→z order (a partial set — the rest are
    # unconfirmed). Zero-filling makes the zip below always 3-wide, so
    # each unconfirmed axis maps to target ``<= 0`` (skipped) and the
    # branch test below sees the true confirmed shape.
    stated = tuple(stated) + (0.0,) * (3 - len(stated))
    if not any(t > 0 for t in stated):
        # No axis confirmed: the gate abstains (True) — an unmeasurable
        # gate must not hard-fail every candidate (ticket #91). The
        # abstention is recorded DISTINCTLY by :func:`score`'s
        # ``bbox_abstained`` field, never a vacuous unmarked pass.
        return True
    if len(stated) > 3:
        # A widened input over the (W, D, H) envelope: the gate measures
        # three axes only — longer input is a caller contract violation,
        # not a fourth measurement target (fail loudly, never silently
        # ignore). ``_dim_params`` and the loop's normalization never
        # produce this; it guards the seam.
        raise ValueError(
            f"stated dimensions must be at most 3 axes (W, D, H), got {len(stated)}"
        )
    if len(stated) == 3 and all(t > 0 for t in stated):
        # Full confirmed triple + component breakdown: rank the components
        # and compare against the best match (issue #100). Without a
        # breakdown (``best_match_component`` returns ``None``) fall
        # through to the whole-mesh extents (the legacy path).
        extents = best_match_component(bbox, stated)
        if extents is None:
            extents = (bbox.x, bbox.y, bbox.z)
    else:
        # A PARTIAL confirmed set (no well-defined component selection —
        # documented choice above): compare the confirmed axes against the
        # whole-mesh extents.
        extents = (bbox.x, bbox.y, bbox.z)
    for extent, target in zip(extents, stated):
        if target <= 0:
            continue  # axis not confirmed: per-axis abstention, skip
        tol = max(BBOX_TOLERANCE_REL * target, BBOX_TOLERANCE_MIN_MM)
        if abs(extent - target) > tol:
            return False
    return True


def _views_non_blank(render: RenderResult) -> bool:
    """True iff the render reports exactly six non-empty view filenames."""
    return len(render.views) == 6 and all(
        isinstance(v, str) and v for v in render.views
    )


#: The declaration scanner shared by the named-parameter gate and the
#: record persistence path (issue #219). Matches one ``name = number;``
#: declaration per line (leading whitespace tolerated). The same regex the
#: gate has always validated against — deliberately NOT broader: a comment
#: prefix, and non-numeric right-hand sides (expressions), do not match,
#: and forcing them to would create a silent scoring/persistence mismatch.
#: ``detect_magic_numbers`` (``d33d.failure_classes``) uses the identical
#: line shape for its declared set, so the extraction and the magic-number
#: gate can never disagree on what counts as a declaration.
_DECLARATION_RE = re.compile(r"^\s*(\w+)\s*=\s*([\d.]+)\s*;", re.MULTILINE)

#: The version-title comment scanner (issue #245): the first line matching
#: ``// title: <text>`` wins; later title lines are ignored. A leading
#: comment the model emits BEFORE the parameter block — ``// title:
#: Bore to 38 mm`` — is not a declaration (it is a comment with a
#: non-numeric RHS, so :data:`_DECLARATION_RE` never matches it) and is
#: read exactly this way: one regex pass, first match, honest absence.
_TITLE_RE = re.compile(r"^\s*//[ \t]*title:[ \t]*(\S.*?)?[ \t]*$", re.MULTILINE)


@functools.lru_cache(maxsize=256)
def extract_named_params(scad_source: str) -> tuple[tuple[str, float], ...]:
    r"""The named dimension assignments the SCAD source actually declares.

    One pass of :data:`_DECLARATION_RE` over the source, collecting every
    ``name = number;`` declaration as ``(name, float)`` pairs (values are
    stored as ``float`` — the downstream readers ``float()`` them on read,
    never a raw string). ``W = 0;`` matches (the regex requires a
    non-empty numeric RHS, not a positive one) and is captured as
    ``0.0``: an honest declaration is recorded, never dropped. The
    result is a tuple of pairs (hashable, so this helper is memoised
    across the loop's repeated gate + persistence calls over the same
    candidate source). An empty source — or one with no declaration line
    — yields ``()``: an honest absence, not a fabricated default. The
    regex's numeric RHS is ``[\d.]+`` and ``float()`` rejects ambiguous
    shapes like ``20.5.0`` (plausible model output): those are SKIPPED
    (mirroring the guarded parse in ``detect_magic_numbers``), never a
    crash — a malformed numeric literal is an honest absence.
    """
    out = []
    for name, value in _DECLARATION_RE.findall(scad_source):
        try:
            out.append((name, float(value)))
        except ValueError:
            continue  # malformed numeric RHS (e.g. 20.5.0) — never a crash
    return tuple(out)


@functools.lru_cache(maxsize=256)
def scad_title(scad_source: str) -> str | None:
    r"""The version title a model supplies as a leading ``// title:`` comment.

    Issue #245: the primary version-name source. One pass of
    :data:`_TITLE_RE` over the source, first match wins (later title lines
    are ignored — a defined rule, mirroring :func:`extract_named_params`
    honest-absence semantics). A title whose value is empty or
    whitespace-only is treated as absent (``None``); the value itself is
    returned UNtrimmed (the caller — ``d33d.versions.clean_name`` —
    trims, sanitises, and caps at ``NAME_MAX_LEN``). A source without a
    title line yields ``None``: an honest absence, never a fabricated
    default.
    """
    m = _TITLE_RE.search(scad_source)
    if m is None:
        return None
    value = m.group(1)
    if value is None or not value.strip():
        return None
    return value


def _named_params_present(
    scad_source: str, stated_dims: tuple[float, float, float]
) -> bool:
    """True iff the .scad declares a named-parameter block AND has no
    un-declared multi-digit geometry literals (the "magic numbers" check
    from ``d33d.failure_classes``).

    The stated dimensions are passed through to the gate (issue #100):
    a literal equal to a stated value (exact float) is not a magic number
    — it is the stated value inlined. Stated axes that are unknown
    (``<= 0``) are omitted: ``0``/absent is "dimension unknown" (the gate
    abstains on it elsewhere), never a stated value — a ``cube([0, 25,
    30])`` must not be exempted by an unknown axis.

    The declaration leg is the SHARED extraction (issue #219): ``re.search``
    for "a declaration exists" and :func:`extract_named_params` for "which
    declarations" are one regex now, so the gate bit and the persisted
    ``IterationRecord.params`` can never silently diverge (gate False
    implies an empty extraction; gate True implies a non-empty one).
    """
    stated_dimensions: dict[str, float] | None = {
        axis: value for axis, value in zip(("W", "D", "H"), stated_dims) if value > 0
    } or None
    if detect_magic_numbers(scad_source, stated_dimensions=stated_dimensions):
        return False
    return bool(extract_named_params(scad_source))


def _axis_param_mismatches(
    bbox: BboxInfo | None,
    named_params: dict[str, float],
    param_meta: dict[str, Any],
) -> list[tuple[str, float, float, str]]:
    """Bit 5 (issue #276) — the per-param evidence behind the bit.

    Returns one ``(label, model_value, measured_extent, axis)`` tuple per
    NUMERIC param with a declared axis (W/D/H) whose SCAD-declared value
    disagrees with the measured bbox extent on that axis by MORE than the
    disagrees-major threshold ``max(DISAGREES_MAJOR_THRESHOLD_REL ×
    param_value, DISAGREES_MAJOR_THRESHOLD_MIN_MM)`` (the 20% / 5 mm pair
    from ``d33d.design_state`` — deliberately NOT the 1% / 0.5 mm bbox
    tolerance). Empty list → bit 5 passes.

    A param is checked only when ALL of the following hold:
    - its ``param_meta`` entry declares an axis (``"W" | "D" | "H"``);
    - its SCAD-declared value is numeric and strictly positive.

    The comparison target is the WHOLE-MESH bbox extent on the declared
    axis (``bbox.x`` / ``bbox.y`` / ``bbox.z``) — the same numbers the
    Brief's axis rows show. A diff STRICTLY greater than the threshold
    mismatches (``>``); a diff exactly at the threshold passes.

    No bbox → ``[]`` (the caller abstains — bit 5 reads as a pass). No
    params with a declared axis → ``[]`` (nothing to check). A param
    with no declared axis, a non-numeric value, or a zero/negative value
    is ignored.
    """
    if bbox is None:
        return []
    from d33d.design_state import (
        DISAGREES_MAJOR_THRESHOLD_MIN_MM,
        DISAGREES_MAJOR_THRESHOLD_REL,
    )

    extents = {"W": bbox.x, "D": bbox.y, "H": bbox.z}
    out: list[tuple[str, float, float, str]] = []
    for name, value in named_params.items():
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            continue
        if value <= 0:
            continue
        meta = param_meta.get(name)
        if not isinstance(meta, dict):
            continue
        axis = meta.get("axis")
        if axis not in extents:
            continue
        tol = max(
            DISAGREES_MAJOR_THRESHOLD_REL * value,
            DISAGREES_MAJOR_THRESHOLD_MIN_MM,
        )
        if abs(extents[axis] - value) > tol:
            raw_label = meta.get("label")
            label = raw_label if isinstance(raw_label, str) and raw_label else name
            out.append((label, float(value), float(extents[axis]), axis))
    return out


def _axis_params_match_geometry(
    bbox: BboxInfo,
    named_params: dict[str, float],
    param_meta: dict[str, Any],
) -> bool:
    """Bit 5 (issue #276): True iff every NUMERIC param with a declared
    axis (W/D/H) matches the measured bbox extent on that axis within the
    disagrees-major threshold — ``not _axis_param_mismatches(...)``."""
    return not _axis_param_mismatches(bbox, named_params, param_meta)


def score(
    render: RenderResult,
    stated_dims: tuple[float, float, float],
    *,
    bbox: BboxInfo | None = None,
    scad_source: str = "",
    param_meta: dict[str, Any] | None = None,
    named_params: dict[str, float] | None = None,
) -> Score:
    """The pinned monotone-comparable improvement metric.

    Gate bitvector (order fixed, earlier bits weigh more in the tiebreak):

    1. ``error_class == ok`` — the render compiled and produced valid
       artifacts (spec gate 1)
    2. six non-empty views — the "all_six_views_nonblank" gate
    3. bbox within max(1%, 0.5 mm) per axis of ``stated_dims`` — gate 4
       has a record to compare against
    4. stated dimensions appear as named parameters in the .scad, never
       as magic-number literals (spec acceptance #4). The gate is called
       WITH the known stated dimensions (issue #100): a literal equal to
       a stated value (exact float) is the stated value inlined, not an
       invented magic number — without it a multi-part request like "a
       20mm cube with a 10mm sphere beside it" (stated W/D/H = 20) fails
       the gate on its own ``translate([20, 0, 0])`` placement literal
       when the model inlines a non-stated dimension (the adversarial
       review's acceptance-criterion finding).

    Ranked by popcount; ties break on the raw bitvector tuple
    (deterministic, earlier bits first).

    ``Score.bbox_abstained`` (ticket #91, issue #247) marks a bbox bit
    that is True while any stated axis is unconfirmed (a zero axis —
    :func:`_bbox_within_tolerance` skips it). The flag is True whenever
    the bit is True and any axis is unconfirmed, INCLUDING a partial
    pass — ``bbox_abstained`` therefore means "not every axis was
    checked" (per-axis reality), and it is False only for a fully
    confirmed, fully measured pass. A partial triple whose confirmed axes
    happen to match still leaves an unmeasured axis, and that pass must
    be distinguishable from a fully measured one. It is a SEPARATE field,
    not a fifth bit, so the bit ordering, the ``tiebreak`` tuple, and
    ``GATE_REASON_BITS`` names are all unchanged while the abstention
    stays distinguishable from a measured pass.

    Per-axis semantics (issue #247): ``stated_dims`` is the per-axis
    confirmed set normalized into the triple — a partially confirmed run
    (e.g. only H) checks ONLY H and abstains on W/D. The frame contract
    that reads the flag (``d33d.design_loop_events``: the ``done`` /
    version-created frames) is unchanged: the flag is always present, and
    the old all-or-nothing meaning ("all axes unknown") is a strict
    subset of the new one.
    """
    bits = (
        render.error_class == "ok",
        _views_non_blank(render),
        bbox is not None and _bbox_within_tolerance(bbox, stated_dims),
        _named_params_present(scad_source, stated_dims),
        _axis_params_match_geometry(
            bbox,
            named_params if named_params is not None else {},
            param_meta if param_meta is not None else {},
        ),
    )
    # An abstained axis is ANY unknown target, independent of whether the
    # other (measured) axes happened to pass — a partial triple whose known
    # axes match still carries an unmeasured axis (ticket #91 round-2: the
    # flag must be True, never left to bit 2's happenstance).
    bbox_abstained = bbox is not None and bits[2] and any(t <= 0 for t in stated_dims)
    return Score(
        bits=bits, rank=sum(bits), tiebreak=bits, bbox_abstained=bbox_abstained
    )


def no_improvement(prev: Score, current: Score) -> bool:
    """True iff ``current`` did not improve on ``prev`` (rank non-increase).

    This is the *named* "no improvement" predicate the spec's stop
    condition ("two consecutive iterations show no improvement") refers to —
    a non-increase of the pinned rank, never a string comparison of
    critiques.
    """
    return current.rank <= prev.rank


def is_best(candidate: Score, incumbent: Score) -> bool:
    """True iff ``candidate`` beats ``incumbent`` on rank, or ties on rank
    and beats it on the (deterministic) tiebreak."""
    if candidate.rank != incumbent.rank:
        return candidate.rank > incumbent.rank
    return candidate.tiebreak > incumbent.tiebreak


# ---------------------------------------------------------------------------
# Message assembly (neutral delimiters, dimensions as named parameters)
# ---------------------------------------------------------------------------


def _dim_params(
    stated: tuple[float, float, float], dims: dict[str, str]
) -> dict[str, str]:
    """The stated dimensions as a NAMED parameter map — extra entries from
    ``dims`` (e.g. FDM clearances) plus each CONFIRMED stated axis as
    W/D/H when not already named. An unconfirmed axis (``<= 0``) becomes
    no define at all (issue #247: ``-DW=0`` would inject a fabricated
    zero into the render's named-parameter channel) — only positive axes
    are injected."""
    out = {str(k): str(v) for k, v in dims.items()}
    for name, value in zip(("W", "D", "H"), stated):
        if value > 0:
            out.setdefault(name, str(value))
    return out


def _scad_params(scad_source: str) -> dict[str, float]:
    """The ``IterationRecord.params`` of one candidate (issue #219): the
    SHARED extraction of the named assignments the candidate's own SCAD
    declares — a record of what was actually built, not a restatement of
    the caller's stated dimensions. A name the SCAD declares over any
    caller-stated value of the same name (replace, not merge); a name the
    SCAD does not declare is absent from the persisted dict even if the
    user stated it. ``{}`` when the SCAD declares nothing (honest
    absence). Values are ``float`` throughout (downstream ``float()``
    readers); ``W = 0;`` is recorded honestly as ``0.0`` (the #93
    never-store-a-zero invariant pertained to the caller-stated path,
    which no longer feeds the record).
    """
    return dict(extract_named_params(scad_source))


def _dim_axis(value: float) -> str:
    """The ``:g`` rendering of one stated-dimension axis, or ``not
    specified`` when the axis is unknown (``<= 0`` — ticket #91 normalizes
    absent dimensions into the triple, so zero IS the "unknown" marker)."""
    if value > 0:
        return f"{value:g}"
    return "not specified"


def _dim_axis_list(stated: tuple[float, float, float]) -> str:
    # Render W=/D=/H= with ``not specified`` on any unknown axis: a dimension
    # the user never stated is never rendered as a number (it must not read
    # as a measured zero, ticket #91).
    return ", ".join(
        f"{name}={_dim_axis(value)}" for name, value in zip(("W", "D", "H"), stated)
    )


def _design_system(stated: tuple[float, float, float]) -> str:
    """Short imperative design-role system prompt (neutral delimiters)."""
    return (
        "You are a parametric CAD designer. "
        f"Ground-truth dimensions in mm: {_dim_axis_list(stated)}. "
        "Never invent a fit-critical number. Every dimension and any FDM "
        "clearance is a named parameter, never an inline literal. "
        "Reply with exactly one fenced JSON block and nothing else."
    )


def _design_state_lines(
    stated: tuple[float, float, float],
    state_params: dict[str, Any] | None,
    state_bbox: dict[str, float] | None = None,
    state_stated: dict[str, float] | None = None,
    state_meta: dict[str, Any] | None = None,
    state_confirmed: dict[str, Any] | None = None,
) -> list[str]:
    """The design-state block's prompt lines (issue #120).

    Builds the block via the SHARED function (``d33d.design_state.
    state_block_for_version`` — the same callable the API route the SPA
    reads calls) and formats it. The block carries ALL declared
    parameters of the latest version (not a fixed {W, D, H} triple) with
    provenance per value; ``unknown`` serialises as ``null`` (never 0,
    never an omitted key). ``state_bbox`` (issue #137) is the latest
    version's persisted measured bbox — when present, the block upgrades
    axis parameters to ``measured``/``disagrees`` (the measurement-aware
    shared function); when ``None`` (no version yet, or no persisted
    measurement) the block renders the pure params-only substrate.

    ``state_params`` is ``None`` when no version exists yet (the block
    renders with zero entries — an honest empty state). When a version
    exists, ``state_params`` is its full params snapshot, so the block is
    the previous version's actual dimensions (the 30/60 bug: the prompt
    carries the description of the existing design, so the model can no
    longer invent 60 for a sphere it had itself made 30).

    ``design_source`` (issue #105) is the CURRENT design's SCAD source
    (the source of the version the project's ``current_version`` points
    at). ``None`` renders the explicit clean-slate wording (never a
    silently-absent section); a string renders the labelled
    ``Current design source (OpenSCAD)`` section — distinct from the
    REPAIR block's ``previous_scad:`` (the failed candidate of THIS turn,
    never the accepted design). The section is orthogonal to #120's
    parameter block: the parameters say what the numbers are, the source
    says what the geometry is; the live prompt carries BOTH (pinned by
    the integration test).

    Inserted as its own labelled lines BETWEEN the chat-history lines and
    the ``Reference dimensions`` line (the gate-resolution's named
    insertion point). The system prompt's ground-truth triple line is
    LEFT ALONGSIDE (unchanged — test_issue91's exact-string assertion
    pins ``W=20, D=25, H=30`` in the system prompt).
    """
    from d33d.design_state import (
        build_design_state_block,
        format_design_state_block,
        state_block_for_version,
    )

    block = build_design_state_block(
        state_block_for_version(
            state_params, state_bbox, state_stated, state_meta, state_confirmed
        )
    )
    # ``format_design_state_block`` renders the header + the entries. The
    # header (``Current design state (mm):``) and the entry lines are
    # rendered verbatim (the block is a self-contained, size-bounded
    # section — the bound ``MAX_STATE_BLOCK_ENTRIES`` is enforced inside
    # ``build_design_state_block``). An empty block renders a single
    # honest ``not specified`` line (never silently absent — the 30/60
    # bug is a prompt that carries no description of the existing design).
    text = format_design_state_block(block)
    # ``format_design_state_block`` returns the header line + the entry
    # lines joined by newlines; split on the first newline to get the
    # header and the body (the body may itself be multiple lines for a
    # multi-entry block).
    parts = text.split("\n", 1)
    lines: list[str] = [parts[0]]
    if len(parts) > 1 and parts[1].strip():
        lines.extend(parts[1].split("\n"))
    return lines


def _design_source_lines(design_source: str | None) -> list[str]:
    """The current-design source prompt lines (issue #105).

    Thin caller over the SHARED section builder (``d33d.design_source.
    design_source_lines``) — the same callable the route-side tests pin,
    so the prompt cannot drift from the documented section. ``None``
    renders the explicit clean-slate wording; a string renders the
    labelled, size-bounded source (truncated with a VISIBLE marker past
    ``MAX_SCAD_SOURCE_BYTES``).
    """
    from d33d.design_source import design_source_lines

    return design_source_lines(design_source)


def _design_messages(
    *,
    photo: str,
    chat_history: Sequence[str],
    stated: tuple[float, float, float],
    repair: dict[str, Any] | None,
    request: str = "",
    state_params: dict[str, Any] | None = None,
    state_bbox: dict[str, float] | None = None,
    state_stated: dict[str, float] | None = None,
    state_meta: dict[str, Any] | None = None,
    state_confirmed: dict[str, Any] | None = None,
    design_source: str | None = None,
) -> list[dict[str, Any]]:
    """The design-role message list: the current user's REQUEST as the
    first line of the user text (issue #97: the current message used to be
    lost — the user text was built from ``chat_history`` alone, and the
    SPA's history excludes the current turn, so the model never saw what
    was asked and invented unrelated geometry) + prior chat turns as
    context + dimensions as named parameters + (on repair iterations) the
    structured failure directive — the tagged class and instruction, never
    a raw stderr dump.

    The request line is rendered VERBATIM (no summarising, rewriting, or
    truncation) and labelled ``Request:`` so the model can distinguish
    the current instruction from the prior-context ``chat:`` lines and
    from the dimensions. Empty/blank requests render no line at all (the
    legacy shape — a caller that supplies no request, e.g. an old stub,
    builds the exact prompt it always built)."""
    lines: list[str] = []
    if request and request.strip():
        lines.append(f"Request: {request}")
    for turn in chat_history:
        lines.append(f"chat: {turn}")
    # The design-state block (issue #120): the previous version's actual
    # parameters with provenance per value, rendered BETWEEN the chat
    # lines and the reference-dimensions line. The shared function
    # (``d33d.design_state``) is the same callable the API route the SPA
    # reads calls (asserted by the tests — not two functions that happen
    # to agree). Empty when no version exists yet (an honest empty state,
    # never a fabricated dimension).
    lines.extend(
        _design_state_lines(
            stated,
            state_params,
            state_bbox,
            state_stated,
            state_meta,
            state_confirmed,
        )
    )
    # The current design source (issue #105): the previous version's
    # actual SCAD, rendered between the state block and the reference
    # dimensions (same insertion point as the state block). One mechanism,
    # one wording for both the chat and region-edit paths — the loop is
    # the shared prompt builder, and ``design_source`` is supplied by
    # every caller that knows the project.
    lines.extend(_design_source_lines(design_source))
    lines.append(f"Reference dimensions (mm, ground truth): {_dim_axis_list(stated)}")
    lines.append(
        "Emit parametric OpenSCAD. Start the file with one comment line "
        "`// title: <what this version is or what changed, at most 40 "
        "characters>` — e.g. `// title: Bore to 38 mm`. Every stated "
        "dimension and any FDM tolerance must be a named parameter in a "
        "top variable block, never an inline literal. Name every parameter "
        "with full words in snake_case — readable identifiers, not "
        "abbreviations (BAD: `fst` for a fillet size; GOOD: `fillet_size_top`). "
        "Reply with a single "
        'fenced JSON block: ```json {"tool": "emit_design", "arguments": '
        '{"scad": <string>, "parameters": [<per parameter: {"name", '
        '"label", "unit", "axis", "reason"}>]}} ``` where "parameters" '
        "lists every parameter you declared, one object each: "
        'the "name" is the exact identifier from the SCAD, the "label" '
        "is a plain-language label a person would recognise (e.g. "
        'fillet_size_top to "Top fillet size"), the "unit" is the unit '
        '("mm" for millimetres), the "axis" is "W" or "D" or "H" ONLY '
        "when the parameter realises that overall dimension of the part "
        '(omit it otherwise - never guess an axis), and the "reason" is '
        "one short clause saying why you picked that value, for values the user did "
        "not give. In the SAME reply, optionally offer to confirm ONE of your own "
        'assumed values (one the design state block marks "assumed") that most '
        'affects fit: "confirm_first" is that parameter' + "'" + "s exact identifier "
        'and "confirm_sentence" is one short plain sentence naming its value (e.g. '
        '"I assumed 3 mm walls. That is sturdy for a shelf spacer. Want it '
        'thinner?") — every number in confirm_sentence must come from the '
        "design state block (never invent one), and omit BOTH fields when "
        "you have no value to offer (never more than one offer, never a "
        "value the state block already marks stated or measured)."
    )
    if repair is not None:
        lines.append("REPAIR directive (structured, not raw stderr):")
        lines.append(f"failure_class: {repair.get('failure_class')}")
        lines.append(f"instruction: {repair.get('instruction')}")
        # The directive's evidence (issue #276): server-built, structured
        # data — for ``axis_params_mismatch`` it names every mismatching
        # param with BOTH numbers ("declared = N but the part measures M on
        # AXIS"). It is part of the directive contract, not optional
        # dressing: rendering only the instruction would make the
        # instruction's promise ("listed below with both numbers") false,
        # because the previous_scad block carries only the SCAD's own
        # numbers, never the measured extents. ``None``/blank renders no
        # line (other classes' evidence is the raw gate class name, which
        # the failure_class line already says).
        evidence = repair.get("evidence")
        if evidence:
            lines.append(f"evidence: {evidence}")
        lines.append("previous_scad:")
        lines.append(str(repair.get("scad_source", "")))

    parts: list[dict[str, Any]] = [
        {"type": "text", "text": "\n".join(lines)},
        {"type": "image_url", "image_url": {"url": photo}},
    ]
    return [{"role": "user", "content": parts}]


#: The upper bound on any .scad source reaching the render worker (bytes),
#: matching the render worker's 256 KiB stderr truncation convention.  An
#: oversized source is a scored failure (never sent to the worker), whether
#: it arrived via the tool call or the fenced fallback.
MAX_SCAD_SOURCE_BYTES = 256 * 1024

#: The fenced-SCAD fallback extractor: ```scad / ```openscad / bare ```
#: fences, non-greedy to the first closing fence.
#:
#: The bare-fence fallback DELIBERATELY accepts ANY fenced code block, not
#: just ```scad / ```openscad-labeled ones. That is intentional and pinned
#: by existing tests (tests/test_design_loop.py); do NOT tighten the regex.
#: A mis-extracted non-SCAD payload is caught by the :func:`scad_looks_valid`
#: pre-render validation (and, if it slips through, by the sandboxed render
#: worker + the size cap at :data:`MAX_SCAD_SOURCE_BYTES` above).
_SCAD_FENCE_RE = re.compile(
    r"```(?:scad|openscad)?[ \t]*\r?\n(.*?)```",
    re.DOTALL,
)

#: Heuristic length cap for extracted SCAD (chars). LLM chat prose can be
#: long; a genuine parametric .scad response is not. Anything over this is
#: treated as not-SCAD before it reaches the render worker.
MAX_SCAD_VALIDATION_CHARS = 5000

#: Token-bounded OpenSCAD keywords a plausible .scad contains at least one
#: of (primitives, transforms, set ops, meta). ``for`` / ``each`` / ``if`` /
#: ``else`` / ``else if`` appear token-bounded so ``if`` never matches
#: inside English prose like "if you..."; bare ``each``/``else`` are
#: excluded because they are common English words.
_SCAD_KEYWORD_RE = re.compile(
    r"\b(?:"
    "cube|cylinder|sphere|hull|minkowski|linear_extrude|rotate|translate|"
    "scale|union|difference|intersection|surface|import|module|polyhedron|"
    "text|resize|mirror|projection|offset|echo|assert|children|"
    r"for\s*\(|each\s*\(|else\s+if\b"
    r")"
)


def scad_looks_valid(scad: str) -> bool:
    """Cheap pre-render heuristic: does this look like OpenSCAD source?

    A candidate must (1) contain at least one ``;`` (a statement
    terminator), (2) be at most :data:`MAX_SCAD_VALIDATION_CHARS` chars,
    and (3) contain at least one known OpenSCAD keyword
    (token-bounded). Prose that slipped past the fenced-JSON protocol
    (a chat response inside a fence, a refusal with a stray ``;``) fails
    at least one leg; anything that fails is treated as empty SCAD so the
    loop's empty_scad fail-fast path handles it — the render worker never
    spends a run compiling garbage.
    """
    if ";" not in scad:
        return False
    if len(scad) > MAX_SCAD_VALIDATION_CHARS:
        return False
    return _SCAD_KEYWORD_RE.search(scad) is not None


"""Extract the ``parameters`` metadata array from a design-role reply
(issue #248).

The model's ``parameters`` array (the fenced-JSON protocol's
``arguments.parameters``, the T0 tool call's ``arguments.parameters`` —
the same shape either way) is a LIST of ``{name, label, unit, axis?,
reason?}`` objects. This normaliser degrades to ``{}`` on ANY malformed
shape (a non-list, a non-dict item, a missing/non-string ``name``) —
"no metadata" is the honest output, and a malformed array NEVER fails
the design pass. Values are never read from here (the SCAD declarations
are the value source); only metadata is joined in by name in
``d33d.design_state``.

Each surviving item keeps only usable fields: ``label``/``unit``/``reason``
as non-empty strings, ``axis`` only when one of ``"W" | "D" | "H"``
(the closed axis set — anything else is dropped, never surfaced). An
item with no usable field is dropped. A name appearing twice: the first
occurrence wins (declaration order — later entries never overwrite).
"""


def _first_tool_call_args_containing(
    result: LLMResult, key: str
) -> dict[str, Any] | None:
    """The arguments dict of the FIRST tool call whose dict-typed
    ``arguments`` carry ``key`` (the shared T0-tool-call / T1-fenced walk
    behind :func:`extract_param_meta` and :func:`extract_confirm_hints`),
    or ``None`` when no tool call carries it."""
    for call in result.tool_calls:
        args = call.get("arguments")
        if isinstance(args, dict) and key in args:
            return args
    return None


def extract_param_meta(result: LLMResult) -> dict[str, Any]:
    """The model's per-parameter metadata from a design-role ``LLMResult``.

    Reads ``tool_calls[*].arguments.parameters`` (the first tool call
    carrying a ``parameters`` field wins — the loop's own tool, whichever
    tier produced the call) and normalises it as above. ``{}`` when no
    tool call carries the field (the T1 fenced path without metadata, or
    a reply that emitted no array) — the loop never raises here.
    """
    args = _first_tool_call_args_containing(result, "parameters")
    if args is None:
        return {}
    raw: Any = args["parameters"]
    if raw is None:
        return {}
    if not isinstance(raw, list):
        return {}
    out: dict[str, Any] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not isinstance(name, str) or not name:
            continue
        if name in out:
            continue  # first occurrence wins (declaration order)
        cleaned: dict[str, Any] = {}
        for key in ("label", "unit", "axis", "reason"):
            v = item.get(key)
            if not isinstance(v, str) or not v:
                continue
            if key == "axis" and v not in ("W", "D", "H"):
                continue
            cleaned[key] = v
        if cleaned:
            out[name] = cleaned
    return out


def extract_confirm_hints(result: LLMResult) -> tuple[str | None, str | None]:
    """The model's offer hints from a design-role ``LLMResult``
    (issue #250): the ``confirm_first`` / ``confirm_sentence`` fields of
    the design tool call (``arguments.confirm_first`` /
    ``arguments.confirm_sentence`` — the same T0-tool-call or T1-fenced
    shape as ``scad`` / ``parameters``).

    BOTH fields are PAIRED from the SAME tool call — the first tool call
    (in order) whose arguments carry either field supplies the pair
    (``_first_tool_call_args_containing`` — the shared walk); a second
    tool call's ``confirm_first`` is never mixed with the first one's
    ``confirm_sentence`` (or vice versa) — an unprompted second call is
    an out-of-contract shape and its fields are ignored, not a
    re-pairing opportunity.

    Returns ``(name_or_None, sentence_or_None)``. Lenient by contract
    (like :func:`extract_param_meta`): a non-dict ``arguments``, a
    missing field, a non-string field, or a blank string all degrade to
    ``None`` for that field — a malformed hint NEVER fails the design
    pass, and ``None`` simply means "the model offered no flag" (the
    selection rule's declared-axis branch still runs)."""
    args = _first_tool_call_args_containing(result, "confirm_first")
    if args is None:
        args = _first_tool_call_args_containing(result, "confirm_sentence")
    if args is None:
        return None, None
    first: str | None = None
    raw_first = args.get("confirm_first")
    if isinstance(raw_first, str) and raw_first.strip():
        first = raw_first.strip()
    sentence: str | None = None
    raw_sentence = args.get("confirm_sentence")
    if isinstance(raw_sentence, str) and raw_sentence.strip():
        sentence = raw_sentence.strip()
    return first, sentence


def _scad_from_result(result: LLMResult) -> str:
    """The .scad source from a design-role LLMResult.

    The fenced-JSON protocol (T0 tool_calls or T1 synthesized tool_calls)
    carries ``arguments.scad``; the fallback is a fenced SCAD block
    (```scad / ```openscad / bare ```) extracted from the content by regex.
    Raw, UNFENCED prose (a refusal, a chat response) is NEVER used as SCAD
    source — the loop routes that through the render worker's failure
    classification as a scored failure instead of sending garbage to the
    render worker. Parameter structure is PRESERVED verbatim — the response
    is never re-templated or stripped here.

    Every extraction path is size-capped at :data:`MAX_SCAD_SOURCE_BYTES`;
    an oversized source is treated as a failure (empty source → the render
    worker's failure classification handles it, the loop's normal
    repair/exhaustion logic applies, and nothing unbounded reaches the
    worker).
    """
    candidate: str | None = None
    for call in result.tool_calls:
        args = call.get("arguments")
        if isinstance(args, dict) and isinstance(args.get("scad"), str):
            candidate = args["scad"]
            break
    if candidate is None and isinstance(result.content, str):
        for m in _SCAD_FENCE_RE.finditer(result.content):
            candidate = m.group(1)
            break
    if candidate is None:
        return ""
    if len(candidate.encode("utf-8", "replace")) > MAX_SCAD_SOURCE_BYTES:
        return ""
    if not scad_looks_valid(candidate):
        return ""
    return candidate


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------


async def _call(fn: Any, *args: Any) -> Any:
    """Invoke an injected callable that may be sync or async."""
    result = fn(*args)
    if asyncio.iscoroutine(result):
        result = await result
    return result


async def run_design_loop_async(
    *,
    photo: str,
    chat_history: Sequence[str] = (),
    stated_dims: tuple[float, float, float] | None,
    render_fn: RenderFn,
    llm_fn: LLMFn,
    defines: dict[str, str] | None = None,
    bbox_fn: BboxFn | None = None,
    log: LogFn | None = None,
    max_iterations: int = MAX_ITERATIONS,
    request: str = "",
    state_params: dict[str, Any] | None = None,
    state_bbox: dict[str, float] | None = None,
    state_stated: dict[str, float] | None = None,
    state_meta: dict[str, Any] | None = None,
    state_confirmed: dict[str, Any] | None = None,
    on_progress: OnProgressFn | None = None,
    design_source: str | None = None,
    on_progress_iteration: Any = "_current",
    renderer_check: Callable[[], bool] | None = None,
) -> DesignResult:
    """Run the bounded iterate-and-score design loop (async core).

    ``photo`` is the reference image (data URI / URL). ``stated_dims`` is
    the ground-truth (W, D, H) triple in mm — never estimated. ``None``
    means "no axis confirmed this run": the whole gate abstains (``Score.
    bbox_abstained``). A zero axis inside the triple means that single
    axis is unconfirmed: it is SKIPPED per-axis (never a measurement
    target) while the positive axes are measured — a partially confirmed
    run (e.g. only H) checks only H, and the pass is recorded as
    abstained unless every axis is confirmed (ticket #91 / issue #247)
    — a gate with no confirmed target must not hard-fail every candidate. ``render_fn(scad, defines)
    -> RenderResult`` and ``llm_fn(role, messages, system) -> LLMResult``
    are injected (dependency injection, the same testable pattern as
    ``d33d.render_worker``); ``defines`` carries extra named parameters
    (e.g. FDM clearances) into both the render and the design prompt.
    ``bbox_fn`` extracts per-axis extents from a render (``None`` → the
    bbox gate fails, e.g. when the render isn't ``ok``).

    ``on_progress`` (issue #121) is the per-view arrival hook. It is
    NOT passed to ``render_fn`` directly (the loop calls it with 2 args,
    the ``RenderFn`` contract); instead the production ``render_fn``
    closure (``app.py`` / ``versions_routes.py``) bakes it in when
    calling ``render_for_design_loop``. The loop OWNS the iteration
    count (the render worker does not inherently know which pass it is
    serving), so once per iteration it stamps the 1-based index onto the
    hook (``_stamp_on_progress_iteration``) and the render worker's
    marker closure forwards it into each marker payload as ``iteration``
    — the documented payload contract actually honoured on the
    production path. A test stub that does not accept the hook simply
    ignores it; the loop's contract is unchanged.
    """
    # Normalize "no dimensions known" (None or a zero/absent axis) into the
    # triple itself: the prompt and the defines map must never carry a
    # fabricated value, and the bbox gate reads abstention off the same
    # triple (ticket #91).
    stated_dims = stated_dims if stated_dims is not None else (0.0, 0.0, 0.0)
    defines_map = _dim_params(stated_dims, defines or {})

    # Pre-flight renderer reachability check (issue #277): a cheap ``docker
    # info`` probe BEFORE the first iteration (and thus before any LLM
    # call). A dead Docker daemon must not burn the design budget blaming
    # the design — the run ends at once with the loop-level reason
    # :data:`RENDERER_UNAVAILABLE`. The probe is injectable (``renderer_check``)
    # so test harnesses default to "available" without shelling out; ``None``
    # runs the real probe.
    #
    # The real probe shells out to ``docker info`` (blocking subprocess):
    # run it off the event loop so a hung/slow daemon never stalls other
    # SSE streams. An injected ``renderer_check`` is assumed to be a cheap
    # test stub — a plain direct call keeps the stubs trivial (a sync
    # zero-arg callable, no coroutine wiring needed).
    if renderer_check is None:
        renderer_ok = await asyncio.to_thread(renderer_is_available)
    else:
        renderer_ok = renderer_check()
    if not renderer_ok:
        return DesignResult(
            status="exhausted",
            best=IterationRecord(iteration=0, scad_source="", render=None, score=None),
            iterations=(),
            failure_reason=RENDERER_UNAVAILABLE,
            iterations_used=0,
        )

    repair: dict[str, Any] | None = None
    best: IterationRecord | None = None
    best_score: Score | None = None
    prev_score: Score | None = None
    consecutive_no_improvement = 0
    iterations: list[IterationRecord] = []

    for iteration in range(1, max_iterations + 1):
        _stamp_on_progress_iteration(on_progress, iteration)
        scad = await _call(
            llm_fn,
            "design",
            _design_messages(
                photo=photo,
                chat_history=chat_history,
                stated=stated_dims,
                repair=repair,
                request=request,
                state_params=state_params,
                state_bbox=state_bbox,
                state_stated=state_stated,
                state_meta=state_meta,
                state_confirmed=state_confirmed,
                design_source=design_source,
            ),
            _design_system(stated_dims),
        )
        design_hash = scad.prompt_hash
        if log is not None:
            log("design", design_hash, scad.status)

        # The model's offer hints (issue #250), extracted ONCE per design
        # call and carried onto the iteration record(s) this call builds.
        _confirm_first, _confirm_sentence = extract_confirm_hints(scad)
        scad_source = _scad_from_result(scad)
        if not scad_source.strip():
            # Fail fast: an empty/blank SCAD would burn a whole render run
            # on nothing. A distinct structured error (not a render class)
            # names the real cause for the next iteration's repair input.
            empty_render = RenderResult(
                ok=False,
                exit_code=1,
                duration_ms=0,
                error_class="empty_model",
                stderr="empty_scad: LLM response contained no SCAD source",
                stl=None,
                csg=None,
                views=(),
            )
            candidate_score = score(empty_render, stated_dims, scad_source=scad_source)
            record = IterationRecord(
                iteration=iteration,
                scad_source=scad_source,
                render=empty_render,
                score=candidate_score,
                failure_class="empty_scad",
                repair=None,
                prompt_hashes={"design": design_hash},
                bbox=None,
                params=_scad_params(scad_source),
                param_meta=extract_param_meta(scad),
                confirm_first=_confirm_first,
                confirm_sentence=_confirm_sentence,
            )
            iterations.append(record)
            if best is None or is_best(candidate_score, best_score):
                best = record
                best_score = candidate_score
            if prev_score is not None and no_improvement(prev_score, candidate_score):
                consecutive_no_improvement += 1
            else:
                consecutive_no_improvement = 0
            prev_score = candidate_score
            if consecutive_no_improvement >= NO_IMPROVEMENT_LIMIT:
                return _exhausted(iterations, best, best_score)
            continue
        render = await _call(render_fn, scad_source, defines_map)
        bbox = bbox_fn(render) if bbox_fn is not None else None
        candidate_score = score(
            render,
            stated_dims,
            bbox=bbox,
            scad_source=scad_source,
            param_meta=extract_param_meta(scad),
            named_params=_scad_params(scad_source),
        )

        # Failure routing: tagged, structured, NEVER terminal. Compile
        # failures are a low-scoring iteration, not a loop stop.
        failure_class: str | None = None
        next_repair: dict[str, Any] | None = None
        classified = classify_failure(
            error_class=render.error_class,
            stderr=render.stderr,
            scad_source=scad_source,
        )
        if render.error_class != "ok":
            # Non-LLM-addressable render classes (timeout/oom/container_
            # error/...) are non-improving steps and NOT fed back.
            if classified.failure_class in REPAIRABLE_CLASSES:
                failure_class = classified.failure_class
                directive = route_repair(classified=classified, scad_source=scad_source)
                if directive is not None:
                    next_repair = directive.to_dict()
        else:
            # ok-but-missing-gates: the vision-catchable class
            # (geometrically_wrong), routed the same way so the next
            # iteration sees the mismatch. Bit 5 (axis_params_mismatch,
            # issue #276) gets a distinct directive that names each
            # mismatching param with both numbers.
            if any(not bit for bit in candidate_score.bits[1:]):
                _mismatches = (
                    _axis_param_mismatches(
                        bbox, _scad_params(scad_source), extract_param_meta(scad)
                    )
                    if not candidate_score.axis_params_match
                    else []
                )
                if not candidate_score.axis_params_match and _mismatches:
                    # Bit 5 fails with per-param evidence (computed ONCE
                    # per iteration above — no re-parsing of the SCAD).
                    # axis_params_mismatch is produced OUTSIDE
                    # ``classify_failure`` on purpose: ``classify_failure``
                    # is a pure stderr/SCAD text classifier (it never
                    # sees the measured bbox), so this gate-evidence class
                    # is built inline here, where the numbers live, and
                    # routed through the same ``route_repair`` as the
                    # classified classes.
                    _evidence = "; ".join(
                        f"{label} = {model:g} but the part measures "
                        f"{measured:g} on {axis}"
                        for label, model, measured, axis in _mismatches
                    )
                    _ax_classified = ClassifiedFailure(
                        failure_class="axis_params_mismatch",
                        evidence=_evidence,
                        repairable=True,
                    )
                    failure_class = "axis_params_mismatch"
                    directive = route_repair(
                        classified=_ax_classified, scad_source=scad_source
                    )
                    if directive is not None:
                        next_repair = directive.to_dict()
                elif not candidate_score.axis_params_match:
                    # Bit 5 false but the mismatch list is empty — the
                    # helper and the gate should never disagree. Log a
                    # WARNING and fall back to the GENERIC classified
                    # path, never the vague "see gate bit 5" evidence.
                    logger.warning(
                        "bit 5 (axis_params_match) is False for "
                        "iteration %s but the per-param mismatch list is "
                        "empty — falling back to the generic classified "
                        "path",
                        iteration,
                    )
                failure_class = (
                    failure_class
                    if failure_class is not None
                    else classified.failure_class
                )
                if failure_class != "axis_params_mismatch":
                    directive = route_repair(
                        classified=classified, scad_source=scad_source
                    )
                    if directive is not None:
                        next_repair = directive.to_dict()

        record = IterationRecord(
            iteration=iteration,
            scad_source=scad_source,
            render=render,
            score=candidate_score,
            failure_class=failure_class,
            repair=next_repair,
            prompt_hashes={"design": design_hash},
            bbox=bbox,
            params=_scad_params(scad_source),
            param_meta=extract_param_meta(scad),
            confirm_first=_confirm_first,
            confirm_sentence=_confirm_sentence,
        )
        iterations.append(record)

        if candidate_score.perfect:
            return DesignResult(
                status="pass",
                best=record,
                iterations=tuple(iterations),
                failure_reason=None,
                iterations_used=iteration,
            )

        if best is None or is_best(candidate_score, best_score):
            best = record
            best_score = candidate_score

        if prev_score is not None and no_improvement(prev_score, candidate_score):
            consecutive_no_improvement += 1
        else:
            consecutive_no_improvement = 0
        prev_score = candidate_score

        # container_error is a dead render ENVIRONMENT (daemon down, image
        # gone — a new source can never fix it): stop the loop immediately
        # after this iteration instead of burning the budget (issue #277).
        # The record above already went through the normal post-render path
        # (score, best and no-improvement bookkeeping) — this is the only
        # container_error-specific step, sitting with the other terminal
        # checks. timeout/oom stay non-stopping (they can be
        # source-dependent), and the synthetic empty-SCAD path never
        # reaches this check (it ``continue``s before any render call).
        if render.error_class == "container_error":
            return _container_error_stop(iterations, best)

        repair = next_repair
        if consecutive_no_improvement >= NO_IMPROVEMENT_LIMIT:
            return _exhausted(iterations, best, best_score)

    return _exhausted(iterations, best, best_score)


def _stamp_on_progress_iteration(on_progress: Any, iteration: int) -> None:
    """Stamp the current 1-based iteration index onto the render worker's
    ``on_progress`` hook (issue #121's payload contract: every per-view
    frame carries the 1-based design-loop iteration index).

    The loop is the one component that knows which pass it is serving —
    the render worker does not inherently know it. The render worker's
    ``_on_marker`` closure reads the hook's ``_current`` attribute when
    stamping each marker payload, so the production path stamps the index
    here, once per iteration, and the worker forwards it verbatim. A
    custom ``on_progress`` (a test stub) without the attribute is left
    untouched — the stamp is only consumed by the worker.
    """
    if callable(on_progress):
        try:
            on_progress._current = iteration  # type: ignore[attr-defined]
        except (AttributeError, TypeError):
            # A function object that refuses attribute assignment: stamping
            # degrades to the adapter's ``iteration: 0`` default (the
            # client's reducer treats 0 as "unknown" and keeps its own
            # count) — the loop's outcome is unaffected.
            pass


def renderer_is_available(probe: Callable[[], bool] | None = None) -> bool:
    """Is the renderer (Docker daemon) reachable right now?

    A cheap ``docker info`` probe via the same ``subprocess.run`` CLI
    invocation the render worker uses (``["docker", "info"]``, a short
    timeout — issue #277). A missing docker binary
    (:class:`FileNotFoundError`), an unreachable daemon (non-zero exit),
    or a hung daemon (``subprocess.TimeoutExpired``) all map to ``False``
    — never a crash. A ``subprocess.run`` stub in a test that raises for
    an unrecognised argv (the render-loop harnesses' convention) degrades
    to ``False`` the same way — the loop's pre-flight then reports
    :data:`RENDERER_UNAVAILABLE`.

    ``probe`` is the injectable seam (issue #277): any zero-arg callable
    returning ``bool`` (e.g. a test stub) replaces the ``docker info``
    call; ``None`` (the default) runs the real probe.

    The 30 s per-process cache (issue #277 operator decision) applies only
    to SUCCESS: a ``True`` result is trusted for
    :data:`RENDERER_PREFLIGHT_CACHE_SECONDS` and the probe is not run
    again inside the window. A ``False`` result is never cached — the
    next call re-probes, so a Docker start within the window is honoured.
    """
    global _preflight_success_at
    if _preflight_success_at is not None and (
        time.monotonic() - _preflight_success_at < RENDERER_PREFLIGHT_CACHE_SECONDS
    ):
        return True
    if probe is None:
        try:
            completed = subprocess.run(
                ["docker", "info"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=_PREFLIGHT_PROBE_TIMEOUT_S,
            )
            available = completed.returncode == 0
        except (OSError, subprocess.SubprocessError):
            # Missing binary (FileNotFoundError is an OSError), daemon
            # unreachable, or a hung daemon (TimeoutExpired) — uniformly
            # "renderer unavailable", never a crash.
            available = False
    else:
        available = bool(probe())
    if available:
        _preflight_success_at = time.monotonic()
    return available


def reset_renderer_preflight_cache() -> None:
    """Drop the per-process pre-flight success cache (the test reset seam).

    The next :func:`renderer_is_available` call re-probes even if a cached
    success is still inside its 30 s window.
    """
    global _preflight_success_at
    _preflight_success_at = None


def run_design_loop(
    *,
    photo: str,
    chat_history: Sequence[str] = (),
    stated_dims: tuple[float, float, float],
    render_fn: RenderFn,
    llm_fn: LLMFn,
    defines: dict[str, str] | None = None,
    bbox_fn: BboxFn | None = None,
    log: LogFn | None = None,
    max_iterations: int = MAX_ITERATIONS,
    request: str = "",
    state_params: dict[str, Any] | None = None,
    state_bbox: dict[str, float] | None = None,
    state_stated: dict[str, float] | None = None,
    state_meta: dict[str, Any] | None = None,
    state_confirmed: dict[str, Any] | None = None,
    design_source: str | None = None,
    renderer_check: Callable[[], bool] | None = None,
) -> DesignResult:
    """Synchronous entry point for the bounded design loop.

    Runs the async core via ``asyncio.run`` (the project has no async
    plugin in CI — the tiers tests use the same pattern). All parameters
    match :func:`run_design_loop_async`.
    """
    return asyncio.run(
        run_design_loop_async(
            photo=photo,
            chat_history=chat_history,
            stated_dims=stated_dims,
            render_fn=render_fn,
            llm_fn=llm_fn,
            defines=defines,
            bbox_fn=bbox_fn,
            log=log,
            max_iterations=max_iterations,
            request=request,
            state_params=state_params,
            state_bbox=state_bbox,
            state_stated=state_stated,
            state_meta=state_meta,
            state_confirmed=state_confirmed,
            design_source=design_source,
            renderer_check=renderer_check,
        )
    )


def make_llm_fn(
    catalogue: Catalogue,
    request_factories: dict[str, Any],
    capabilities: dict[str, CapabilityResult] | None = None,
    dialect: str = "openai",
) -> LLMFn:
    """Build the loop's ``llm_fn`` over the role aliases.

    Every call resolves its role through ``d33d.config.resolve.resolve_model``
    (never a hardcoded model id) and dispatches through
    ``d33d.design_llm.send`` at the tier the capability probe assigned —
    the loop itself never branches on tier or dialect. ``request_factories``
    maps role -> the injected HTTP edge (the probe/test seam);
    ``capabilities`` maps role -> the cached ``CapabilityResult`` (``None``
    per role degrades that role via the sender's T2/T3 ``SenderError``).
    """
    caps = capabilities or {}

    async def llm_fn(
        role: str, messages: list[dict[str, Any]], system: str | None
    ) -> LLMResult:
        resolution = resolve_model(catalogue, role)
        capability = caps.get(role)
        return await send(
            role=role,
            model_id=resolution.entry.model,
            messages=messages,
            request_factory=request_factories[role],
            capability=capability,
            dialect=dialect,  # type: ignore[arg-type]
            system=system,
            # The T0 native tool schema is the role's OWN (resolved by
            # ``send`` from the role registry — ``role_tools(role)``); it
            # is never caller-supplied, so the wire ``tools`` always
            # matches the name the response-side allowlist enforces, for
            # every role (design / critique / classification / question).
        )

    return llm_fn


def _container_error_stop(
    iterations: list[IterationRecord],
    best: IterationRecord,
) -> DesignResult:
    """The immediate-stop result for a container_error render (issue #277).

    A dead render environment (daemon down, image gone) cannot be fixed by
    a new source, so the loop ends after the failing iteration: status
    ``exhausted``, the container_error render as best, and the failure
    reason ``container_error`` — never the generic ``error_class_not_ok``
    that :func:`_exhausted`'s bit 0 name would otherwise report.
    """
    return DesignResult(
        status="exhausted",
        best=best,
        iterations=tuple(iterations),
        failure_reason="container_error",
        iterations_used=len(iterations),
    )


def _exhausted(
    iterations: list[IterationRecord],
    best: IterationRecord,
    best_score: Score | None,
) -> DesignResult:
    """Build the exhaustion result: best-scoring candidate + a STRUCTURED
    failure reason (weakest gate bit of the best, never free text, never
    silently the last attempt).

    Issue #277: when the FIRST failing gate bit is bit 0
    (``error_class_not_ok``) and the best render carries a NON-"ok"
    error_class, the reason is that specific class (one of the closed
    render-worker ErrorClasses) — never the generic ``error_class_not_ok``
    that bit 0's name would otherwise report for every render failure. The
    bit-0 names for every OTHER failing bit (views, bbox, named params,
    axis params) are unchanged. A synthetic render-less record (``best.render
    is None``) and a best render with error_class "ok" keep today's
    behaviour.
    """
    reason: str | None = None
    if best_score is not None:
        for bit, name in zip(best_score.bits, GATE_REASON_BITS):
            if not bit:
                reason = name
                break
    # Issue #277: when the FIRST failing gate bit is bit 0 (the generic
    # ``error_class_not_ok``) and the best render carries a NON-"ok"
    # error_class, swap in that specific class — never the generic name.
    # A render-less record (``best.render is None``) keeps today's
    # behaviour; a best render with error_class "ok" that fails a LATER
    # gate keeps that gate's name.
    if (
        best.render is not None
        and best.render.error_class != "ok"
        and (reason is None or reason == GATE_REASON_BITS[0])
    ):
        reason = best.render.error_class
    return DesignResult(
        status="exhausted",
        best=best,
        iterations=tuple(iterations),
        failure_reason=reason,
        iterations_used=len(iterations),
    )
