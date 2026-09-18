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
import re
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from d33d.config.catalogue import Catalogue
from d33d.config.probes import CapabilityResult
from d33d.config.resolve import resolve_model
from d33d.design_llm import LLMResult, role_tools, send
from d33d.failure_classes import (
    REPAIRABLE_CLASSES,
    classify_failure,
    detect_magic_numbers,
    route_repair,
)
from d33d.render_worker import RenderResult

__all__ = [
    "MAX_ITERATIONS",
    "NO_IMPROVEMENT_LIMIT",
    "BboxInfo",
    "DesignResult",
    "IterationRecord",
    "Score",
    "is_best",
    "make_llm_fn",
    "no_improvement",
    "run_design_loop",
    "run_design_loop_async",
    "scad_looks_valid",
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
    The bbox gate compares the stated triple against the BEST-MATCHING
    component when ``components`` is non-empty; when it is empty the gate
    takes the whole-part path exactly as before, so every existing caller
    that builds ``BboxInfo(x, y, z, volume)`` sees byte-for-byte the old
    behaviour (an empty breakdown means "no component data", never "one
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

    ``bits`` is the 4-tuple ``(ok, non_blank_views, bbox, named_params)``.
    ``rank`` is the popcount — the monotone-comparable value the loop uses
    for stop conditions. ``tiebreak`` is the raw bitvector: two candidates
    with equal rank compare on it (earlier bits weigh more), so "best" is
    never ambiguous and the metric is unit-testable in isolation.
    """

    bits: tuple[bool, bool, bool, bool]
    rank: int
    tiebreak: tuple[bool, bool, bool, bool]
    #: True iff the bbox bit is True and ANY stated axis is unknown
    #: (``<= 0``) and the gate ABSTAINED on it (ticket #91) — including
    #: partial triples where the known axes happen to match: an unknown
    #: axis was never measured, so the pass must carry the flag even when
    #: every measured axis passed. Never True on an all-known triple.
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
    def perfect(self) -> bool:
        """All four gates pass (v1 FINALIZE's "validation passes")."""
        return self.rank == len(self.bits)


@dataclass(frozen=True)
class IterationRecord:
    """One candidate generation (LLM design call + render + score)."""

    iteration: int
    scad_source: str
    render: RenderResult
    score: Score
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
    bbox: "BboxInfo | None" = None
    #: The named parameters THIS candidate's render was made with — the
    #: ``_dim_params`` defines map passed to ``render_fn``, converted for
    #: the versions table's scalar-param representation (issue #93):
    #: values that parse as numbers are stored as ``float`` (W/D/H must be
    #: numeric — ``latest_version_stated_dims`` and the region-edit route
    #: ``float()`` them on read), other values keep their string form.
    #: An axis the user never stated is OMITTED entirely, never stored as
    #: ``0`` (a stored zero is a false fact — "this part is 0 mm wide" —
    #: whereas absent correctly means "unknown", and the downstream
    #: readers already treat a missing/``<= 0`` axis as unknown). Caller-
    #: supplied non-dimension defines (e.g. FDM clearances) are carried
    #: through as-is — they are real parameters of the render. May be
    #: empty (a dimensionless pass still materialises a version with an
    #: empty param set; the loop's ``_dim_params`` is the source of
    #: truth, and a caller that supplies extra defines plus no known
    #: dimensions still produces a non-empty map from them alone).
    params: dict[str, Any] = field(default_factory=dict)


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
)


# ---------------------------------------------------------------------------
# The improvement metric — named and independently unit-testable
# ---------------------------------------------------------------------------


def _component_per_axis_difference(
    extents: tuple[float, float, float], stated: tuple[float, float, float]
) -> float:
    """Sum of the absolute per-axis differences between a component's
    extents and the stated triple — the best-match metric (issue #100).
    Every stated axis is known here (the caller abstains first), so no
    axis is skipped."""
    return sum(abs(extent - target) for extent, target in zip(extents, stated))


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

    Volume here is only a TIE-BREAKER, never a conformance signal: for
    intersecting/contained shells ``split()`` double-counts the overlap in
    component volumes, so a volume comparison is not a size comparison.
    """
    if not bbox.components:
        return None
    ranked = sorted(
        bbox.components,
        key=lambda c: (
            _component_per_axis_difference(c[:3], stated),
            -(c[3] if len(c) > 3 else 0.0),
            c[4] if len(c) > 4 else 0.0,
            c[5] if len(c) > 5 else 0.0,
            c[6] if len(c) > 6 else 0.0,
        ),
    )
    return tuple(ranked[0][:3])


def _bbox_within_tolerance(
    bbox: BboxInfo, stated: tuple[float, float, float]
) -> bool:
    """True iff every rendered axis is within max(1%, 0.5 mm) of the
    corresponding stated dimension (order x, y, z).

    A stated axis of ``<= 0`` means that dimension is UNKNOWN (the caller
    normalizes absent/zero dimensions into the triple — ticket #91), so
    the gate ABSTAINS (True): an unmeasurable gate must not hard-fail
    every candidate. The abstention is recorded DISTINCTLY by
    :func:`score`'s ``bbox_abstained`` field — it is never a vacuous,
    unmarked pass.

    Issue #100 — multi-part meshes: when ``bbox.components`` is non-empty
    the stated triple is compared against the BEST-MATCHING component
    (the component whose extents are closest to the triple — see
    :func:`best_match_component`) instead of the whole-assembly extents:
    a stated single-body triple can never match a whole-assembly bbox that
    contains additional bodies, so the whole-assembly comparison made any
    multi-part request unsatisfiable by construction. The abstain check
    runs BEFORE any component work (ticket #91 semantics preserved exactly).

    Degradation is explicit (issue #100): when the mesh splits to exactly
    one watertight component the gate compares that component against the
    triple — which is byte-for-byte what the whole-part comparison is for
    a single body (the single component IS the assembly). Fused/
    intersecting geometry that OpenSCAD's CSG merged into one shell also
    yields one component and therefore behaves as today: the gate measures
    the union extents, not a per-feature decomposition. A mesh that splits
    to ZERO components (a genuinely broken/non-watertight mesh) fails the
    gate — a vacuous pass here would repeat the exact defect class issue
    #84 removed, so a zero-component split is never treated as "no bodies,
    nothing to measure". That case is reachable only through the
    production ``bbox_from_render`` seam (which sets ``components``);
    test-built ``BboxInfo``s without a breakdown keep the legacy
    whole-part comparison.
    """
    for target in stated:
        if target <= 0:
            return True
    extents = best_match_component(bbox, stated)
    if extents is None:
        extents = (bbox.x, bbox.y, bbox.z)
    for extent, target in zip(extents, stated):
        tol = max(BBOX_TOLERANCE_REL * target, BBOX_TOLERANCE_MIN_MM)
        if abs(extent - target) > tol:
            return False
    return True


def _views_non_blank(render: RenderResult) -> bool:
    """True iff the render reports exactly six non-empty view filenames."""
    return len(render.views) == 6 and all(
        isinstance(v, str) and v for v in render.views
    )


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
    """
    stated_dimensions: dict[str, float] | None = {
        axis: value for axis, value in zip(("W", "D", "H"), stated_dims) if value > 0
    } or None
    if detect_magic_numbers(
        scad_source, stated_dimensions=stated_dimensions
    ):
        return False
    declared = re.search(r"^\s*\w+\s*=\s*[\d.]+\s*;", scad_source, re.MULTILINE)
    return declared is not None


def score(
    render: RenderResult,
    stated_dims: tuple[float, float, float],
    *,
    bbox: BboxInfo | None = None,
    scad_source: str = "",
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

    ``Score.bbox_abstained`` (ticket #91) marks a bbox bit that is True
    while any stated dimension is unknown (a zero/absent axis —
    :func:`_bbox_within_tolerance` abstains on an unknown target). The flag
    is raised whenever ANY stated axis is unknown and the bit is True (not
    only when the bit is true merely because of the abstention): a partial
    triple whose known axes happen to match still leaves an unmeasured
    axis, and that pass must be distinguishable from a fully measured one.
    It is a SEPARATE field, not a fifth bit, so the bit ordering, the
    ``tiebreak`` tuple, and ``GATE_REASON_BITS`` names are all unchanged
    while the abstention stays distinguishable from a measured pass.
    """
    bits = (
        render.error_class == "ok",
        _views_non_blank(render),
        bbox is not None and _bbox_within_tolerance(bbox, stated_dims),
        _named_params_present(scad_source, stated_dims),
    )
    # An abstained axis is ANY unknown target, independent of whether the
    # other (measured) axes happened to pass — a partial triple whose known
    # axes match still carries an unmeasured axis (ticket #91 round-2: the
    # flag must be True, never left to bit 2's happenstance).
    bbox_abstained = (
        bbox is not None and bits[2] and any(t <= 0 for t in stated_dims)
    )
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
    ``dims`` (e.g. FDM clearances) plus the stated triple as W/D/H when not
    already named."""
    out = {str(k): str(v) for k, v in dims.items()}
    for name, value in zip(("W", "D", "H"), stated):
        out.setdefault(name, str(value))
    return out


def _params_for_record(defines_map: dict[str, str]) -> dict[str, Any]:
    """The ``IterationRecord.params`` conversion of the loop's defines map
    (issue #93): numeric values are stored as ``float`` (the versions table
    stores scalar JSON params and ``latest_version_stated_dims`` / the
    region-edit route read W/D/H via ``float(params.get(axis, 0.0))``),
    non-numeric values keep their string form, and axes the caller never
    stated are OMITTED (the loop's ``_dim_params`` stringifies a zero
    triple to ``"0"`` for unknown axes — a stored ``0`` would be a false
    fact; absent means "unknown", which the readers already honour).
    Non-dimension caller defines (e.g. FDM clearances) pass through as-is.
    """
    out: dict[str, Any] = {}
    for key, raw in defines_map.items():
        if key in ("W", "D", "H"):
            try:
                value: Any = float(raw)
            except ValueError:
                continue  # unparseable dimension — never a fabricated value
            if value <= 0:
                continue  # abstained axis: unknown, never a stored zero
            out[key] = value
        else:
            try:
                out[key] = float(raw)
            except ValueError:
                out[key] = raw
    return out


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
        f"{name}={_dim_axis(value)}"
        for name, value in zip(("W", "D", "H"), stated)
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
        state_block_for_version(state_params, state_bbox)
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
    lines.extend(_design_state_lines(stated, state_params, state_bbox))
    # The current design source (issue #105): the previous version's
    # actual SCAD, rendered between the state block and the reference
    # dimensions (same insertion point as the state block). One mechanism,
    # one wording for both the chat and region-edit paths — the loop is
    # the shared prompt builder, and ``design_source`` is supplied by
    # every caller that knows the project.
    lines.extend(_design_source_lines(design_source))
    lines.append(
        f"Reference dimensions (mm, ground truth): {_dim_axis_list(stated)}"
    )
    lines.append(
        "Emit parametric OpenSCAD. Every stated dimension and any FDM "
        "tolerance must be a named parameter in a top variable block, "
        "never an inline literal. Reply with a single fenced JSON block: "
        '```json {"tool": "emit_design", "arguments": {"scad": <string>}} ```'
    )
    if repair is not None:
        lines.append("REPAIR directive (structured, not raw stderr):")
        lines.append(f"failure_class: {repair.get('failure_class')}")
        lines.append(f"instruction: {repair.get('instruction')}")
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
    on_progress: OnProgressFn | None = None,
    design_source: str | None = None,
    on_progress_iteration: Any = "_current",
) -> DesignResult:
    """Run the bounded iterate-and-score design loop (async core).

    ``photo`` is the reference image (data URI / URL). ``stated_dims`` is
    the ground-truth (W, D, H) triple in mm — never estimated. ``None``
    (or a zero/absent axis inside the triple) means "no dimensions known":
    the bbox gate ABSTAINS (``Score.bbox_abstained``) instead of hard-
    failing on a ``target <= 0`` target — an unmeasurable gate must not
    fail every candidate (ticket #91). ``render_fn(scad, defines)
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
                design_source=design_source,
            ),
            _design_system(stated_dims),
        )
        design_hash = scad.prompt_hash
        if log is not None:
            log("design", design_hash, scad.status)

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
            candidate_score = score(
                empty_render, stated_dims, scad_source=scad_source
            )
            record = IterationRecord(
                iteration=iteration,
                scad_source=scad_source,
                render=empty_render,
                score=candidate_score,
                failure_class="empty_scad",
                repair=None,
                prompt_hashes={"design": design_hash},
                bbox=None,
                params=_params_for_record(defines_map),
            )
            iterations.append(record)
            if best is None or is_best(candidate_score, best_score):
                best = record
                best_score = candidate_score
            if prev_score is not None and no_improvement(
                prev_score, candidate_score
            ):
                consecutive_no_improvement += 1
            else:
                consecutive_no_improvement = 0
            prev_score = candidate_score
            if consecutive_no_improvement >= NO_IMPROVEMENT_LIMIT:
                return _exhausted(iterations, best, best_score)
            continue
        render = await _call(render_fn, scad_source, defines_map)
        bbox = bbox_fn(render) if bbox_fn is not None else None
        candidate_score = score(render, stated_dims, bbox=bbox, scad_source=scad_source)

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
            # iteration sees the mismatch.
            if any(not bit for bit in candidate_score.bits[1:]):
                failure_class = classified.failure_class
                directive = route_repair(classified=classified, scad_source=scad_source)
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
            params=_params_for_record(defines_map),
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
    design_source: str | None = None,
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
            design_source=design_source,
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
            # T0 native tool schema, attached per the role actually being
            # called (design -> emit_design, critique -> emit_critique, ...);
            # send() only puts it in the body on the T0 branch, and T1
            # bodies never carry a tools array regardless.
            tools=role_tools(role) if capability is not None and capability.tier == "T0" else None,
        )

    return llm_fn


def _exhausted(
    iterations: list[IterationRecord],
    best: IterationRecord,
    best_score: Score | None,
) -> DesignResult:
    """Build the exhaustion result: best-scoring candidate + a STRUCTURED
    failure reason (weakest gate bit of the best, never free text, never
    silently the last attempt)."""
    reason: str | None = None
    if best_score is not None:
        for bit, name in zip(best_score.bits, GATE_REASON_BITS):
            if not bit:
                reason = name
                break
    if reason is None and best.render is not None:
        reason = best.render.error_class
    return DesignResult(
        status="exhausted",
        best=best,
        iterations=tuple(iterations),
        failure_reason=reason,
        iterations_used=len(iterations),
    )
