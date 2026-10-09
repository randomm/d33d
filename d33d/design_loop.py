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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal

import httpx

from d33d.config.catalogue import Catalogue
from d33d.config.probes import CapabilityResult
from d33d.config.resolve import resolve_model
from d33d.design_llm import LLMResult, send
from d33d.evals.failure_capture import (
    POST_CHECK_REASONS,
    SCREW_CLEARANCE_REASON,
    STACK_HEIGHT_REASON,
    THROUGH_HOLE_REASON,
)
from d33d.failure_classes import (
    REPAIRABLE_CLASSES,
    ClassifiedFailure,
    classify_failure,
    detect_magic_numbers,
    route_repair,
)
from d33d.render_worker import (
    RENDER_WORKER_IMAGE,
    RenderResult,
    _verify_render_worker_image,
    build_hash,
    canonical_build_command,
)
from d33d.unchanged_mesh_check import MESH_UNCHANGED_REASON

logger = logging.getLogger(__name__)

__all__ = [
    "MAX_ITERATIONS",
    "MODEL_UNCONFIGURED",
    "NO_IMPROVEMENT_LIMIT",
    "RENDERER_IMAGE_STALE",
    "RENDERER_PREFLIGHT_CACHE_SECONDS",
    "RENDERER_UNAVAILABLE",
    "BboxInfo",
    "DesignResult",
    "IterationRecord",
    "Score",
    "_axis_param_mismatches",
    "extract_named_params",
    "gate_comparison_extents",
    "import_part_lines",
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

#: The loop-level (NOT an error_class) failure reason emitted when the model
#: pre-flight check finds the configured LLM model cannot be used before
#: the first iteration (issue #303). The run ends at once with this reason
#: and NO LLM call — the same pre-flight pattern as
#: :data:`RENDERER_UNAVAILABLE`, but for the model rather than the renderer.
MODEL_UNCONFIGURED = "model_unconfigured"

#: The loop-level (NOT an error_class) failure reason emitted when the
#: render-worker IMAGE pre-flight check finds the ``d33d/render-worker:
#: local`` image missing, or its build-hash label no longer matching the
#: working tree, before the first iteration (issue #346). The run ends at
#: once with this reason, NO LLM call, and a structured
#: ``renderer_detail`` on the result (``image_missing`` vs
#: ``label_mismatch`` + the exact rebuild command). Terminal — retrying
#: changes nothing until the operator rebuilds the image. Distinct from
#: :data:`RENDERER_UNAVAILABLE` (a docker-query failure stays retryable).
RENDERER_IMAGE_STALE = "renderer_image_stale"

#: How long a SUCCESSFUL renderer pre-flight check is trusted before the
#: next design-loop run re-checks (per-process cache; issue #277 operator
#: decision). A FAILED check is NEVER cached — the next run re-probes, so
#: a Docker start within the window is not masked by a stale failure.
RENDERER_PREFLIGHT_CACHE_SECONDS = 30.0

#: The wall-clock bound on the ``docker info`` probe's ``subprocess.run``
#: (issue #277: "a short timeout (≤ 5 s)").
_PREFLIGHT_PROBE_TIMEOUT_S = 5.0

#: The wall-clock bound (seconds) on the design loop's PRE-FLIGHT probes
#: (issue #417): the ``docker info`` renderer check and the render-worker
#: image check both run blocking ``subprocess.run`` calls off the event
#: loop (``asyncio.to_thread``) — the subprocess has its own ``timeout``,
#: but a probe that ignores it (or whose thread wedges in ``communicate``)
#: would otherwise stall the whole run before any LLM call. A short
#: named bound (30 s, below the per-attempt budget below) makes a hung
#: pre-flight end with the same retryable :data:`RENDERER_UNAVAILABLE`
#: result the OSError path already yields, instead of a silent stall.
PREFLIGHT_PROBE_TIMEOUT_SECONDS = 30.0

#: The design loop's per-attempt wall-clock deadline (issue #417), in
#: seconds: ONE named constant, shared by the loop's ``attempt_timeout``
#: default and the adapter (``d33d.design_loop_events`` imports it —
#: previously a duplicate ``design_loop_events.DESIGN_LOOP_ATTEMPT_TIMEOUT_
#: SECONDS`` name). The full two-tier timeout design (inner per-call
#: bound vs this outer per-attempt deadline, and the adapter's safety
#: net) is documented in one place:
#: :func:`_await_with_per_attempt_deadline`. ``None`` (the seam) disables
#: the deadline; any numeric value bounds it (the previous ``> 0`` guard
#: is dropped — a 0-second budget is meaningless and would be a caller
#: bug, and a negative one is likewise meaningless rather than a
#: disable).
DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS = 120.0

#: The per-process cache of the last SUCCESSFUL pre-flight probe: a
#: timestamp (``time.monotonic``) or ``None`` (never cached / reset).
_preflight_success_at: float | None = None

#: Tolerance for the bbox gate: max(1% of the stated size, 0.5 mm) per
#: axis (v1 FINALIZE gate — render-worker-level, per the issue's proposed
#: resolution of the open question).
BBOX_TOLERANCE_REL = 0.01
BBOX_TOLERANCE_MIN_MM = 0.5

#: The screw-hole clearance post-check's undersize tolerance (issue #317):
#: a hole is undersize when it is BELOW the table's clearance diameter by
#: MORE than this many mm (a hole at 4.45 mm for M4 abstains; at 4.44 mm
#: it triggers).
UNDERSIZE_EPSILON_MM = 0.05

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
    iterations (never silently the last attempt); ``None`` ONLY when the
    per-attempt deadline cut the run off before any render — no candidate
    exists rather than a fabricated placeholder. ``failure_reason`` is one
    of the structured classes in :data:`GATE_REASON_BITS` or a render-worker
    class — never free text.

    ``env_var`` (issue #303) carries the model pre-flight's missing/empty
    ``${ENV}`` variable NAME (never the value) when the loop short-circuits
    on ``model_unconfigured`` — the adapter's terminal frame picks it up.
    ``None`` for every other outcome.

    ``renderer_detail`` (issue #346) carries the structured verified
    fault when the loop short-circuits on :data:`RENDERER_IMAGE_STALE`:
    ``{"reason": "image_missing" | "label_mismatch", "expected"?,
    "actual"?, "rebuild_command"}`` — the terminal frame rides it verbatim
    (omit-not-null). ``None`` for every other outcome.

    ``attempt_latencies`` (issue #417) carries the LOOP'S OWN measured
    wall-clock seconds for the attempts it actually started (one entry per
    STARTED attempt, in order), when a deadline-driven timeout cut the run
    off mid-LLM-call: a killed attempt's time is the budget it burned
    before the deadline fired, a completed one is its full duration. The
    adapter's frame-derived ``AttemptTracker`` never sees a killed attempt
    (no view frames arrive), so the loop's own clock is the only number
    that exists in the primary slow-model case; ``None`` for every
    non-deadline outcome (omit-not-null).
    """

    status: str
    best: IterationRecord | None
    iterations: tuple[IterationRecord, ...]
    failure_reason: str | None = None
    iterations_used: int = 0
    #: The number of attempts the loop STARTED (issue #417) — including
    #: the one killed mid-LLM-call — on a deadline-driven timeout. The
    #: adapter's terminal frame and the failures.jsonl row carry this as
    #: the slow-model copy's "after N tries" count. ``0`` for every
    #: non-deadline outcome (omit-not-null: the adapter only reads it
    #: when the reason is ``design_loop_timed_out``).
    attempts_started: int = 0
    env_var: str | None = None
    renderer_detail: dict[str, str] | None = None
    attempt_latencies: tuple[float, ...] | None = None


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


def _bbox_target(
    stated: tuple[float, float, float],
    part_bbox_mm: tuple[float, float, float] | None,
) -> tuple[float, float, float]:
    """The bbox gate's per-axis target for the run (issue #332 sub-issue 3,
    extended by issue #383 — the part-baseline floor).

    Per axis (the floor is a PER-AXIS rule, never a blanket one):

    - an axis with a CONFIRMED stated value (``> 0``) always wins — the
      gate compares that axis against the stated value, exactly as today
      (a "cut it down to 15 mm tall" request keeps comparing against 15,
      never against the part's 20);
    - an axis with NO confirmed stated value takes the part's own extent
      as its target when ``part_bbox_mm`` is present (issue #383: a pure
      import project previously abstained on every axis and the gate
      passed a 17.7 mm³ garbage fragment of a 20 mm part) — the rendered
      extent on that axis may not come out SMALLER than the part's extent
      beyond the gate's own tolerance (growth is free — add-ons enlarge;
      the shrink direction is the one the floor constrains);
    - no part (``part_bbox_mm is None``) → the stated triple, unchanged —
      the no-part regression anchor, byte-identical gate semantics.
    """
    if part_bbox_mm is None:
        return stated
    target: list[float] = []
    for s, p in zip(stated, part_bbox_mm):
        target.append(s if s > 0 else p)
    return tuple(target)


def gate_comparison_extents(
    bbox: BboxInfo, stated: tuple[float, ...]
) -> tuple[float, float, float] | None:
    """The extents the bbox gate ACTUALLY compares for ``stated`` (issue
    #367, lens review): the SINGLE definition of the gate's component-
    selection decision, shared by :func:`_bbox_within_tolerance` and the
    terminal error frame's ``measured_axes`` (``d33d.design_loop_events``).
    Divergence between the two is structurally impossible — both call this.

    ``None`` when the gate ABSTAINS entirely (no axis confirmed —
    ``stated`` holds no positive axis). Otherwise: a FULL positive (W, D, H)
    triple with a component breakdown compares the BEST-MATCHING component
    (issue #100); a partial confirmed set (or no breakdown) compares the
    whole-mesh extents — the conservative (fail-safe) direction documented
    on :func:`_bbox_within_tolerance`.

    Raises ``ValueError`` (a contract violation, distinct from the ``None``
    abstain return above) when ``stated`` carries more than 3 axes — the
    same ValueError :func:`_bbox_within_tolerance` propagates unchanged.
    """
    stated = tuple(stated) + (0.0,) * (3 - len(stated))
    if not any(t > 0 for t in stated):
        return None  # the gate abstains (no axis confirmed — issue #247)
    if len(stated) > 3:
        # A widened input over the (W, D, H) envelope: caller contract
        # violation — fail loudly, never silently ignore (issue #247).
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
        # documented choice on _bbox_within_tolerance): compare the
        # confirmed axes against the whole-mesh extents.
        extents = (bbox.x, bbox.y, bbox.z)
    return extents


def _gate_selection_extents(
    bbox: BboxInfo,
    user_triple: tuple[float, ...],
    target: tuple[float, ...],
) -> tuple[float, float, float] | None:
    """The comparison extents the bbox gate ACTUALLY measures (issue #389,
    extracted from :func:`_bbox_within_tolerance` and
    ``d33d.design_loop_events._measured_axes`` — the two call sites used
to each duplicate this selection with slightly different branches).

    - a user-confirmed axis exists → the gate's OWN selection decides the
      shape (``gate_comparison_extents`` — a full confirmed triple with a
      component breakdown ranks the best-matching component, issue #100;
      a partial set compares the whole mesh);
    - no user-confirmed axis but a FLOOR target is resolved (issue #383
      part-baseline) → the whole-mesh extents (the gate compared the part's
      overall extents — a component match is never a well-defined target
      when the user confirmed nothing);
    - neither → ``None`` (the gate abstains entirely — no axis confirmed,
      no floor; the caller records the abstention distinctly).

    ``user_triple`` — the user's OWN confirmed set (zero-filled for
    unconfirmed axes). ``target`` — the resolved gate target after
    :func:`_bbox_target` (the part-baseline floor fills unconfirmed axes).
    """
    if any(t > 0 for t in user_triple):
        return gate_comparison_extents(bbox, user_triple)
    if any(t > 0 for t in target):
        return (bbox.x, bbox.y, bbox.z)
    return None


def _bbox_within_tolerance(
    bbox: BboxInfo,
    stated: tuple[float, ...],
    user_stated: tuple[float, ...] | None = None,
) -> bool:
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
    # The selection decision (component vs whole-mesh, per-axis
    # abstention, the >3-axis contract violation) lives in
    # :func:`gate_comparison_extents` — the single definition shared with
    # the terminal error frame's ``measured_axes`` (issue #367).
    # Issue #383: ``stated`` is the RESOLVED gate target (the part-baseline
    # floor fills unconfirmed import-project axes); ``user_stated`` (the
    # caller's own confirmed set) selects the comparison shape — a user-
    # confirmed FULL triple ranks components (issue #100), while the
    # floor-filled axes of a pure-import project keep the whole-mesh
    # comparison (the floor compares the OVERALL extents, not a
    # per-component match).
    user = tuple(stated) if user_stated is None else tuple(user_stated)
    extents = _gate_selection_extents(bbox, user, stated)
    if extents is None:
        # No axis confirmed AND no floor: the gate abstains (True) — an
        # unmeasurable gate must not hard-fail every candidate (ticket #91).
        # The abstention is recorded DISTINCTLY by :func:`score`'s
        # ``bbox_abstained`` field, never a vacuous unmarked pass.
        return True
    for extent, target, user_target in zip(extents, stated, user):
        tol = max(BBOX_TOLERANCE_REL * target, BBOX_TOLERANCE_MIN_MM)
        if user_target > 0:
            # A user-confirmed axis: the two-sided ``abs()`` comparison,
            # exactly as today (issue #247 semantics unchanged).
            if abs(extent - target) > tol:
                return False
        else:
            # A floor axis (issue #383): the part's extent is the target
            # and the user did not confirm it — one-sided. Smaller than
            # the part beyond tolerance fails; larger is allowed (the
            # floor constrains the shrunken-garbage direction only).
            if extent < target - tol:
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


#: The keywords (issue #385, operator decision 2026-10-05) marking a
#: parameter's name or label as a POSITION or OFFSET — the import gate
#: ignores such params (a mistagged position can't fail an import edit).
_POSITION_PARAM_KEYWORDS: tuple[str, ...] = (
    "from", "offset", "distance", "position", "spacing", "margin", "inset", "pitch",
)


def _param_label(meta: dict[str, Any], name: str) -> str:
    """The param's human label, falling back to its name (the gate's
    keyword check scans name and label, so an unlabeled param is checked
    by name)."""
    raw_label = meta.get("label")
    return raw_label if isinstance(raw_label, str) and raw_label else name


def _is_position_param(name: str, label: str) -> bool:
    """True iff the param's name or (lowercased once) label carries a
    :data:`_POSITION_PARAM_KEYWORDS` marker (issue #385)."""
    lower = label.lower()
    return any(kw in name.lower() or kw in lower for kw in _POSITION_PARAM_KEYWORDS)


def _axis_param_mismatches(
    bbox: BboxInfo | None,
    named_params: dict[str, float],
    param_meta: dict[str, Any],
    *,
    part_scale: float | None = None,
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

    Two exemptions (checked in this order, per param): the unconditional
    position/offset exemption (issue #385, scratch AND import) and the
    import-only feature-noun exemption (issue #420, ``part_scale`` is
    ``not None``).
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
        # Issue #385 (operator decision 2026-10-05): the import gate ignores
        # parameters whose names or labels mark them as positions or offsets
        # (a mistagged position can't fail an import edit). Unconditional on
        # scratch AND import — issue #420 leaves it untouched.
        label = _param_label(meta, name)
        if _is_position_param(name, label):
            continue
        # Issue #420: on an import the model axis-tags FEATURE parameters
        # ("Groove depth" tagged D) — the feature's size vs the part's
        # measured extent is not a mismatch. A name or label carrying a
        # feature noun from ``d33d.axis_lexicon._FEATURE_NOUNS`` (imported
        # by reference, never copied — issue #413 may mutate the set) is
        # skipped on imports only; scratch keeps byte-identical bit 5.
        if part_scale is not None and _is_feature_param(name, label):
            continue
        tol = max(
            DISAGREES_MAJOR_THRESHOLD_REL * value,
            DISAGREES_MAJOR_THRESHOLD_MIN_MM,
        )
        if abs(extents[axis] - value) > tol:
            out.append((label, float(value), float(extents[axis]), axis))
    return out


def _axis_params_match_geometry(
    bbox: BboxInfo,
    named_params: dict[str, float],
    param_meta: dict[str, Any],
    part_scale: float | None = None,
) -> bool:
    """Bit 5 (issue #276): True iff every NUMERIC param with a declared
    axis (W/D/H) matches the measured bbox extent on that axis within the
    disagrees-major threshold — ``not _axis_param_mismatches(...)``.

    ``part_scale`` is the issue #420 import signal (``not None`` → an
    import project): it threads the feature-noun exemption through the
    helper so the gate bit can never diverge from the repair evidence.
    """
    return not _axis_param_mismatches(bbox, named_params, param_meta, part_scale=part_scale)


def _is_feature_param(name: str, label: str) -> bool:
    """True iff the param's name or label tokens contain a feature noun
    (issue #420): :func:`d33d.design_state._split_name_tokens` (deferred
    import — the same lazy ``design_state`` import the helper uses for
    the thresholds) over the name, and a space/underscore split over the
    human-prose label, each piece on camelCase boundaries; any token in
    :data:`d33d.axis_lexicon._FEATURE_NOUNS` (imported by reference, never
    copied) is a feature marker.

    Token-based by design: ``groove_depth`` is excluded by ``groove``, but
    ``total_depth`` is not (``depth`` is a length word, not a feature
    noun). Length words in ``_LENGTH_WORDS`` never trigger the exemption.
    """
    from d33d.axis_lexicon import _FEATURE_NOUNS
    from d33d.design_state import _split_name_tokens

    def _has_feature(token: str) -> bool:
        return any(t in _FEATURE_NOUNS for t in _split_name_tokens(token))

    if _has_feature(name):
        return True
    for raw in label.replace("_", " ").split():
        if _has_feature(raw):
            return True
    return False


def score(
    render: RenderResult,
    stated_dims: tuple[float, float, float],
    *,
    bbox: BboxInfo | None = None,
    scad_source: str = "",
    param_meta: dict[str, Any] | None = None,
    named_params: dict[str, float] | None = None,
    part_bbox_mm: tuple[float, float, float] | None = None,
    part_scale: float | None = None,
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

    ``part_bbox_mm``: the part's settled measured bbox (W, D, H) in mm —
    an import project's part baseline (issue #332 sub-issue 3 / #383): the
    bbox gate's target fills unconfirmed axes with the part's extent.

    ``part_scale``: the settled import file→mm factor (``not None`` → an
    import project; issue #420's import signal). It threads the
    import-only feature-noun exemption into bit 5 (``_axis_param_mismatches
``) so a feature param ("Groove depth" tagged D) can't fail the gate or
    the repair evidence on an import; ``None`` (scratch) keeps bit 5
    byte-identical.

    Per-axis semantics (issue #247): ``stated_dims`` is the per-axis
    confirmed set normalized into the triple — a partially confirmed run
    (e.g. only H) checks ONLY H and abstains on W/D. The frame contract
    that reads the flag (``d33d.design_loop_events``: the ``done`` /
    version-created frames) is unchanged: the flag is always present, and
    the old all-or-nothing meaning ("all axes unknown") is a strict
    subset of the new one.
    """
    # Issue #332 (sub-issue 3) / #383: the bbox gate's target — the
    # part's measured extent fills every UNCONFIRMED axis of an import
    # project (the part-baseline floor), else the stated triple (see
    # :func:`_bbox_target`).
    _bbox_stated = _bbox_target(stated_dims, part_bbox_mm)
    bits = (
        render.error_class == "ok",
        _views_non_blank(render),
        bbox is not None
        and _bbox_within_tolerance(bbox, _bbox_stated, user_stated=stated_dims),
        _named_params_present(scad_source, stated_dims),
        _axis_params_match_geometry(
            bbox,
            named_params if named_params is not None else {},
            param_meta if param_meta is not None else {},
            part_scale,
        ),
    )
    # An abstained axis is ANY unknown target, independent of whether the
    # other (measured) axes happened to pass — a partial triple whose known
    # axes match still carries an unmeasured axis (ticket #91 round-2: the
    # flag must be True, never left to bit 2's happenstance). Issue #383:
    # the resolved gate target (``_bbox_stated`` — the part-baseline floor
    # fills unconfirmed import-project axes) is what was actually measured,
    # so the flag now tracks THAT: a pure-import candidate measured against
    # the part's extent is fully measured (flag False), while the old
    # abstaining (0,0,0) target (no part) keeps the flag exactly as before.
    bbox_abstained = bbox is not None and bits[2] and any(t <= 0 for t in _bbox_stated)
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


# The deterministic screw-hole clearance post-check (issue #317) lives in
# ``d33d.screw_hole_check``.  The import is deferred to the call site:
# ``design_prompts`` imports ``design_loop`` at module top (for
# ``_dim_axis_list``), so a top-level ``from d33d.design_prompts import …``
# here would create a real cycle — the deferred import breaks it.

#: The import guard's repair instruction (issue #332, sub-issue 3): the
#: single definition of the fix the routed ``geometrically_wrong``
#: directive carries when the guard fires (the module owns the detection,
#: this constant owns the fix text — the #317 split, kept in one place).
PART_IMPORT_INSTRUCTION_FIX = (
    'every candidate on this project must build on the imported part: '
    'scale(<the settled file-to-mm factor>) import("part.stl") is the '
    "first operation, and you may only ADD (union) or CUT (difference) "
    "on top of it — never rebuild, re-model, resize, or scale the imported "
    "mesh any other way."
)


def _through_hole_post_check(
    request: str,
    stl: str | None,
    through_baseline_genus: int | None,
    scad_source: str,
    through_baseline_genus_source: str | None = None,
) -> tuple[str, str] | None:
    """Issue #386: the through-hole genus post-check (deferred-import
    wrapper, the #317 pattern).

    Delegates to :func:`d33d.through_hole_check.route_through_hole_repair`
    which resolves the baseline, runs the check, and routes the repair
    through ``route_repair``. Returns ``(evidence, instruction)`` on a
    fired repair, ``None`` when the check abstains or the hole passed.

    ``through_baseline_genus_source`` (issue #418) is the adapter seam's
    provenance string — forwarded to the check which logs it with every
    decision so QA can see WHERE the baseline came from.
    """
    from d33d.through_hole_check import route_through_hole_repair

    return route_through_hole_repair(
        request,
        stl,
        through_baseline_genus,
        scad_source,
        through_baseline_genus_source,
    )


def _undersize_screw_hole(
    request: str,
    params: dict[str, float],
    param_meta: dict[str, Any],
) -> tuple[str, float, float, str] | None:
    """Issue #317: the deterministic screw-hole clearance post-check.

    See :func:`d33d.screw_hole_check.undersize_screw_hole` for the full
    contract.  Returns the DETECTION tuple ``(size, value, clearance,
    label)`` (or ``None`` when the check abstains).  The caller (the
    design loop) folds this into the repair dict and routes it through
    ``route_repair``.  This wrapper defers the import to break the
    circular import chain (design_loop → screw_hole_check →
    design_prompts → design_loop)."""
    from d33d.screw_hole_check import undersize_screw_hole

    return undersize_screw_hole(request, params, param_meta)


def _mm(value: float) -> str:
    """The screw-hole check's millimetre renderer (issue #317) — the
    SAME ``mm`` the detection tuple's ``value`` / ``clearance`` are
    rendered with in the check's ``instruction`` / ``evidence``.  The
    import is deferred to the call site (the same circular-import break
    as :func:`_undersize_screw_hole`)."""
    from d33d.screw_hole_check import mm

    return mm(value)


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


def _unchanged_mesh_post_check(
    parent_stl: str | None,
    render: RenderResult,
    parent_fingerprint: tuple[str, int] | None = None,
    parent_volume_mm3: float | None = None,
    parent_face_count: int | None = None,
) -> tuple[str, str] | None:
    """Issue #419: the unchanged-mesh post-check (deferred-import wrapper,
    the #317 pattern).

    Delegates to :func:`d33d.unchanged_mesh_check.unchanged_mesh_check`
    (the v2+ parent baseline ``None`` → the check abstains; a parent
    whose ``model.stl`` is missing or unreadable → the check abstains —
    a fabricated baseline would make the gate lie). Returns
    ``(evidence, instruction)`` on a fired repair, ``None`` when the
    check abstains or the mesh genuinely changed.

    ``parent_fingerprint`` (issue #419 lens fix) is the parent's
    geometry fingerprint measured off the SAME seam load as the genus —
    the check uses it directly and does NOT re-load the parent mesh.
    ``parent_volume_mm3`` / ``parent_face_count`` ride the same seam
    load as a sanity check and the evidence.
    """
    from d33d.unchanged_mesh_check import unchanged_mesh_check

    return unchanged_mesh_check(
        parent_stl=parent_stl,
        candidate_stl=getattr(render, "stl", None),
        parent_fingerprint=parent_fingerprint,
        parent_volume_mm3=parent_volume_mm3,
        parent_face_count=parent_face_count,
    )


def _render_fingerprint(stl: str | None) -> tuple[str, int] | None:
    """Issue #432: the geometry fingerprint of one candidate's rendered STL,
    used by the identical-repair stop (the same fingerprint the unchanged-mesh
    check compares). ``None`` abstains (no STL, or an unreadable mesh)."""
    from d33d.unchanged_mesh_check import _load_mesh, mesh_fingerprint

    if not isinstance(stl, str) or not stl:
        return None
    mesh = _load_mesh(stl)
    if mesh is None:
        return None
    return mesh_fingerprint(mesh)


def _stack_height_post_check(
    scad_source: str,
    measured_z: float | None,
) -> tuple[float, float] | None:
    """Issue #409: the stack-height post-check (deferred-import wrapper,
    the #317 pattern).

    Delegates to :func:`d33d.stack_height_check.stack_height_check`
    (declared derived height sum vs the measured Z the loop already
    computes from the bbox). Returns the DETECTION tuple
    ``(declared, measured)`` when the declared stack sum and the
    measured Z disagree beyond the disagrees-major threshold, ``None``
    when the check abstains or the stack passed.
    """
    from d33d.stack_height_check import stack_height_check

    return stack_height_check(scad_source, measured_z)


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


def _design_system(
    stated: tuple[float, float, float], part_scale: float | None = None
) -> str:
    """Short imperative design-role system prompt (neutral delimiters).

    Carries the metric screw-clearance table (issue #317) rendered from
    the SINGLE definition in ``d33d.design_prompts`` — the same table
    ``design_prompt`` renders and the loop's post-check reads, so the
    clearance numbers live in exactly one module.

    ``part_scale`` (issue #332, sub-issue 3): the project's settled
    file→mm factor when the part is assumed/settled. When present, the
    import-aware section (``import_part_instruction`` — the SINGLE
    definition in ``d33d.design_prompts``) is appended; when ``None``
    (no part, or an unsettled part — the caller never runs a loop on an
    unsettled part) the prompt is byte-identical to today."""
    from d33d.design_prompts import (
        SCREW_CLEARANCE_INSTRUCTION,
        TOTAL_HEIGHT_INSTRUCTION,
        clearance_rows_line,
        import_part_instruction,
    )

    system = (
        "You are a parametric CAD designer. "
        f"Ground-truth dimensions in mm: {_dim_axis_list(stated)}. "
        "Never invent a fit-critical number. Every dimension and any FDM "
        "clearance is a named parameter, never an inline literal. "
        "Screw clearance (through-holes), in mm: "
        f"{clearance_rows_line()}. "
        f"{SCREW_CLEARANCE_INSTRUCTION} "
        # Issue #409 (task-prompt): the derived total_height instruction —
        # the SAME constant the T0 tool schema and design_prompt render.
        f"{TOTAL_HEIGHT_INSTRUCTION} "
    )
    if part_scale is not None:
        system += import_part_instruction(part_scale) + " "
    system += "Reply with exactly one fenced JSON block and nothing else."
    return system


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


def _design_source_lines(
    design_source: str | None, part_scale: float | None = None
) -> list[str]:
    """The current-design source prompt lines (issue #105).

    Thin caller over the SHARED section builder (``d33d.design_source.
    design_source_lines``) — the same callable the route-side tests pin,
    so the prompt cannot drift from the documented section. ``None``
    renders the explicit clean-slate wording; a string renders the
    labelled, size-bounded source (truncated with a VISIBLE marker past
    ``MAX_SCAD_SOURCE_BYTES``).

    ``part_scale`` (issue #332, sub-issue 3): for an import project the
    v1 design source is ``None`` (the mesh IS the design — v1 has no
    ``.scad``). The clean-slate wording would then tell the model to "
    CREATE a complete new design", contradicting the import section — so
    when the part is wired, the ``None`` case renders the import-section
    header instead (a string source — a v2+ candidate that must import
    the part — renders unchanged through the shared builder).
    """
    if design_source is not None:
        from d33d.design_source import design_source_lines

        return design_source_lines(design_source)
    if part_scale is not None:
        from d33d.design_source import DESIGN_SOURCE_HEADER

        return [
            DESIGN_SOURCE_HEADER
            + " the project's imported part — it is MESH, not text (the "
            + f"design source is the file's own geometry: scale({part_scale:g}) "
            + 'import("part.stl") is the design; ADD/CUT onto it, never '
            + "rebuild it)"
        ]
    from d33d.design_source import design_source_lines

    return design_source_lines(design_source)


def import_part_lines(part_scale: float | None) -> list[str]:
    """The import section's prompt lines (issue #332, sub-issue 3).

    ``part_scale`` is the project's settled file→mm factor when the part
    is assumed/settled (``1.0`` for an mm part), else ``None`` (no part,
    or an unsettled part — the loop never runs on an unsettled part).
    ``None`` renders NOTHING (the byte-identity regression anchor: the
    no-part and unsettled-part prompts are byte-identical to today);
    a factor renders :func:`d33d.design_prompts.import_part_instruction`
    (the SINGLE definition, ``scale({:g})`` included) as its own line.
    """
    if part_scale is None:
        return []
    from d33d.design_prompts import import_part_instruction

    return [import_part_instruction(part_scale)]


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
    part_scale: float | None = None,
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
    builds the exact prompt it always built).

    ``part_scale`` (issue #332, sub-issue 3): the project's settled
    file→mm factor when the part is assumed/settled — ``None`` (no part,
    or an unsettled part) renders the EXACT prompt of today (the
    byte-identity regression anchor); a factor renders the import section
    (``import_part_lines`` — the single definition in
    ``d33d.design_prompts``) right after the design-state block, and the
    clean-slate design-source wording becomes the import-aware one (the
    v1 mesh IS the design source; see :func:`_design_source_lines`).
    """
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
    # The import section (issue #332, sub-issue 3): rendered AFTER the
    # state block, BEFORE the design-source section — ``None`` renders
    # nothing (byte-identical to today).
    lines.extend(import_part_lines(part_scale))
    # The current design source (issue #105): the previous version's
    # actual SCAD, rendered between the state block and the reference
    # dimensions (same insertion point as the state block). One mechanism,
    # one wording for both the chat and region-edit paths — the loop is
    # the shared prompt builder, and ``design_source`` is supplied by
    # every caller that knows the project.
    lines.extend(_design_source_lines(design_source, part_scale))
    lines.append(f"Reference dimensions (mm, ground truth): {_dim_axis_list(stated)}")
    lines.append(
        "Emit parametric OpenSCAD. Start the file with one comment line "
        "`// title: <what this version is or what changed, at most 40 "
        "characters>` — e.g. `// title: Bore to 38 mm`. Every stated "
        "dimension and any FDM tolerance must be a named parameter in a "
        "top variable block, never an inline literal. "
        "Name every parameter "
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
        '(omit it otherwise - never guess an axis. Declare an axis ONLY '
        "when the parameter IS the part's own overall W, D or H extent — "
        "a mating part's size (such as a box's inside), a rim drop, a skirt or "
        "any other feature size is NOT the part's W, D or H. "
        "`hole_distance_from_left_edge` is NOT an axis parameter — a position, "
        'offset or distance is never an axis), and the "reason" is '
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
    at least one leg. Both callers treat a reject as absent SCAD: the
    loop's ``empty_scad`` fail-fast never spends a render run compiling
    garbage, and the adapter's timeout-kept version gate (issue #417)
    never persists a truncated stream as a version.
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


async def _call(
    fn: Any, *args: Any, deadline_armed: bool = False
) -> Any:
    """Invoke an injected callable that may be sync or async.

    An async callable is invoked in its OWN task (not awaited inline):
    the loop's per-attempt deadline (issue #417) wraps the awaited
    future with ``asyncio.wait_for``, and ``wait_for`` must be able to
    CANCEL the in-flight call on expiry — an inline ``await`` leaves no
    task to cancel, so the deadline would never fire for a hanging
    coroutine.

    A sync callable runs inline — UNLESS the deadline is armed
    (``deadline_armed`` — issue #417 lens round 4: a blocking sync call
    running inline on the event loop would make the per-attempt deadline
    a no-op, stalling every other task on the loop for the call's full
    duration). With the deadline armed, the sync call is dispatched to a
    worker thread via ``asyncio.to_thread`` BEFORE the deadline awaits
    it, so ``wait_for`` can actually bound it (the deadline's
    ``CancelledError`` is swallowed — the loop treats it as any other
    non-timeout exception; the worker thread cannot be killed mid-flight
    and the call runs to completion in the background, the same
    best-effort-cancellation contract as the async path).
    """
    if asyncio.iscoroutinefunction(fn):
        return await asyncio.ensure_future(fn(*args))
    if deadline_armed:
        # The sync call runs in a worker thread (dispatched NOW — the
        # deadline's ``wait_for`` gets an awaitable it can cancel at the
        # deadline). A sync call that wedges raises nothing the deadline
        # can use (``wait_for`` cancels the future, not the thread), so
        # the loop's per-attempt deadline bounds the WALL CLOCK (the
        # result the caller sees), and the worker thread's own lifetime
        # is bounded separately (the render worker's subprocess timeout
        # one level down in production). An exception in the sync call
        # propagates out of ``to_thread`` unchanged (the original
        # exception type — the deadline only converts its own
        # ``TimeoutError``; see :func:`_await_with_per_attempt_deadline`).
        return await asyncio.to_thread(fn, *args)
    result = fn(*args)
    if asyncio.iscoroutine(result):
        # A sync callable returning a coroutine (an async def called
        # without await — not the production shape, but supported).
        return await asyncio.ensure_future(result)
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
    part_scale: float | None = None,
    part_bbox_mm: tuple[float, float, float] | None = None,
    through_baseline_genus: int | None = None,
    through_baseline_genus_source: str | None = None,
    parent_mesh_stl: str | None = None,
    parent_fingerprint: tuple[str, int] | None = None,
    parent_volume_mm3: float | None = None,
    parent_face_count: int | None = None,
    on_progress_iteration: Any = "_current",
    renderer_check: Callable[[], bool] | None = None,
    image_check: Callable[[], dict[str, str] | None] | None = None,
    attempt_timeout: float | None = DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS,
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
    # The real probes shell out to ``docker`` (blocking subprocess): run
    # them off the event loop so a hung/slow daemon never stalls other SSE
    # streams, and bound them with ``asyncio.wait_for`` (issue #417, the
    # named :data:`PREFLIGHT_PROBE_TIMEOUT_SECONDS`): a probe that wedges
    # (a daemon that ignores its own subprocess timeout) degrades to the
    # SAME retryable :data:`RENDERER_UNAVAILABLE` result the OSError path
    # yields, never a silent stall. An injected ``renderer_check`` /
    # ``image_check`` is a cheap test stub — a plain direct call keeps the
    # stubs trivial (a sync zero-arg callable, no coroutine wiring
    # needed). The image probe (issue #346) follows the same injectability
    # convention: an injected ``image_check`` is a sync stub, the real
    # probe runs off the loop.
    if renderer_check is None:
        try:
            renderer_ok = await asyncio.wait_for(
                asyncio.to_thread(renderer_is_available),
                timeout=PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # A pre-flight probe that wedges is a transient daemon fault:
            # the SAME retryable renderer_unavailable result the OSError
            # path yields — never a silent stall, never image_stale.
            logger.debug(
                "renderer pre-flight probe wedged past the %ss bound — "
                "degrading this run to renderer_unavailable (retryable)",
                PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            )
            renderer_ok = False
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

    # Pre-flight render-worker IMAGE check (issue #346): the daemon being up
    # is not enough — the image must EXIST and carry a build-hash label
    # matching the working tree (``d33d.render_worker._verify_render_worker_
    # image`` — the same guard the per-render path runs, promoted to
    # pre-flight). A VERIFIED missing/stale image ends the run at once with
    # :data:`RENDERER_IMAGE_STALE` + the structured ``renderer_detail``
    # (image missing vs label X vs expected Y, plus the exact rebuild
    # command), still before any LLM call. A docker-query FAILURE (the
    # probe raising ``OSError`` — daemon down, inspect timeout, binary
    # vanished) is NOT staleness: it stays :data:`RENDERER_UNAVAILABLE`
    # (retryable, issue #277 semantics) — never the terminal rebuild fault.
    # The probe is injectable (``image_check``, ``None`` runs the real
    # probe) so the fast suite never shells out to real Docker.
    if image_check is None:
        try:
            image_detail = await asyncio.wait_for(
                asyncio.to_thread(default_image_check),
                timeout=PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            # A pre-flight image probe that wedges is a transient daemon
            # fault: the SAME retryable renderer_unavailable result the
            # OSError path yields — never a silent stall, never the
            # terminal renderer_image_stale.
            logger.debug(
                "render-worker image pre-flight probe wedged past the %ss "
                "bound — degrading this run to renderer_unavailable ("
                "retryable), never renderer_image_stale",
                PREFLIGHT_PROBE_TIMEOUT_SECONDS,
            )
            return DesignResult(
                status="exhausted",
                best=IterationRecord(
                    iteration=0, scad_source="", render=None, score=None
                ),
                iterations=(),
                failure_reason=RENDERER_UNAVAILABLE,
                iterations_used=0,
            )
    else:
        try:
            image_detail = image_check()
        except OSError as exc:
            # A docker-query failure from the image probe (daemon down,
            # inspect timeout, binary vanished — issue #346 operator
            # decision 1) is NOT a verified image fault: fall back to the
            # RETRYABLE ``renderer_unavailable`` path, never the terminal
            # ``renderer_image_stale``
            # (a transient daemon outage must not become a "rebuild the
            # image" notice). Logged at DEBUG so the degradation is
            # observable without spamming a per-run WARN-level line.
            logger.debug(
                "render-worker image pre-flight could not query docker (%s) — "
                "degrading this run to renderer_unavailable (retryable), "
                "never renderer_image_stale",
                exc,
            )
            return DesignResult(
                status="exhausted",
                best=IterationRecord(
                    iteration=0, scad_source="", render=None, score=None
                ),
                iterations=(),
                failure_reason=RENDERER_UNAVAILABLE,
                iterations_used=0,
            )
    if image_detail is not None:
        return _image_stale_result(image_detail)

    repair: dict[str, Any] | None = None
    best: IterationRecord | None = None
    best_score: Score | None = None
    prev_score: Score | None = None
    # Issue #432: the previous iteration's post-check (reason, fingerprint)
    # — a repeat of the same post-check on an identical mesh stops the loop.
    prev_post: tuple[str, tuple[str, int]] | None = None
    # Issue #432: the last RENDERED attempt's record (the identical-repair stop
    # reports it; a non-render attempt never overwrites it).
    prev_rendered: IterationRecord | None = None
    consecutive_no_improvement = 0
    iterations: list[IterationRecord] = []
    # The loop's OWN per-attempt wall clock (issue #417): one
    # ``time.monotonic()`` reading when the iteration's design LLM call
    # STARTS, then the finished call's elapsed is appended exactly once
    # per path — the call the per-attempt deadline bounds (the render
    # that follows is bounded separately by the render worker). A killed
    # attempt keeps the time it actually burned (the budget), a completed
    # one its full duration; the deadline path hands this list to the
    # result so the adapter's terminal frame and the failures.jsonl row
    # carry the LOOP-SOURCED numbers, never the frame-derived ones (the
    # frames never arrive for an attempt killed mid-LLM-call).
    _attempt_latencies: list[float] = []

    # The per-attempt wall-clock deadline (issue #417): each design
    # iteration's LLM CALL gets its OWN budget (``attempt_timeout`` —
    # production defaults it to
    # :data:`DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS`, 120 s) so three slow
    # attempts are never cut off by one flat total, and no single LLM
    # call may hang forever. The loop OWNS this per-attempt LLM-call
    # budget: on expiry it returns the best-so-far rendered candidate
    # through the SAME result path an exhausted loop uses (a real,
    # scored ``IterationRecord`` — never fabricated), and the adapter's
    # derived total is a pure outer safety net (full design in
    # :func:`_await_with_per_attempt_deadline`).
    for iteration in range(1, max_iterations + 1):
        _stamp_on_progress_iteration(on_progress, iteration)
        _attempt_started = time.monotonic()
        scad_co = _call(
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
                part_scale=part_scale,
            ),
            _design_system(stated_dims, part_scale),
            deadline_armed=attempt_timeout is not None,
        )
        _scad_or_result: LLMResult | DesignResult
        if attempt_timeout is not None:
            # The deadline can fire: ``_await_with_per_attempt_deadline``
            # returns ``LLMResult`` on success, the best-so-far
            # exhausted ``DesignResult`` on expiry (the loop's own
            # per-attempt deadline — issue #417). The type union is
            # handled just below: a ``DesignResult`` is the terminal
            # result itself.
            _scad_or_result = await _await_with_per_attempt_deadline(
                scad_co,
                timeout=attempt_timeout,
                iteration=iteration,
                on_timeout=lambda it=iterations, b=best, s=best_score: _exhausted(
                    it, b, s, failure_reason="design_loop_timed_out"
                ),
            )
        else:
            # The deadline is disabled (the seam) — plain await; the
            # result is unambiguously an ``LLMResult``.
            _scad_or_result = await scad_co
        if isinstance(_scad_or_result, DesignResult):
            # The deadline (or the inner per-LLM-call timeout) fired
            # mid-LLM-call: append the killed attempt's burned budget,
            # then rebuild the (frozen) result carrying the loop's own
            # measured numbers — attempt count + per-attempt latencies —
            # and return it. The rebuild goes through
            # ``dataclasses.replace`` (a fresh instance with the same
            # fields, two of them overridden — frozen dataclasses refuse
            # in-place assignment).
            _attempt_latencies.append(time.monotonic() - _attempt_started)
            return replace(
                _scad_or_result,
                attempt_latencies=tuple(_attempt_latencies),
                attempts_started=len(_attempt_latencies),
            )
        scad = _scad_or_result
        _attempt_latencies.append(time.monotonic() - _attempt_started)
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
            # Issue #432: a non-render attempt breaks the consecutive-renders
            # premise of the identical-repair stop - reset the tracker.
            prev_post = None
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
            part_bbox_mm=part_bbox_mm,
            part_scale=part_scale,
        )

        # Failure routing: tagged, structured, NEVER terminal. Compile
        # failures are a low-scoring iteration, not a loop stop.
        failure_class: str | None = None
        next_repair: dict[str, Any] | None = None
        classified = classify_failure(
            error_class=render.error_class,
            stderr=render.stderr,
            scad_source=scad_source,
            render_log=getattr(render, "render_log", ""),
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
                        bbox,
                        _scad_params(scad_source),
                        extract_param_meta(scad),
                        part_scale=part_scale,
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

        # Issue #332 (sub-issue 3): the import guard's deterministic
        # post-check — an ok render on an import project whose candidate
        # does not build on the part (missing import, wrong filename,
        # rescale, or resize) is a repair BEFORE the pass return, routed
        # through the EXISTING ``geometrically_wrong`` class (no new
        # error_class) via ``route_repair`` — the same shape as the #317
        # screw-hole check, which rides on the same branch.
        if (
            render.error_class == "ok"
            and next_repair is None
            and part_scale is not None
        ):
            from d33d.import_guard import import_guard_violation

            _guard_det = import_guard_violation(
                scad_source, part_scale=part_scale
            )
            if _guard_det is not None:
                _reason, _detail = _guard_det
                _ax_classified = ClassifiedFailure(
                    failure_class="geometrically_wrong",
                    evidence=_detail,
                    repairable=True,
                )
                directive = route_repair(
                    classified=_ax_classified, scad_source=scad_source
                )
                if directive is not None:
                    failure_class = "geometrically_wrong"
                    _next_repair = directive.to_dict()
                    _next_repair["instruction"] = (
                        f"{_detail} — {PART_IMPORT_INSTRUCTION_FIX}"
                    )
                    next_repair = _next_repair

        # Issue #317: an ok render whose gates are green can still carry an
        # undersize metric-screw hole (hole size is not a gate bit).  The
        # check runs BEFORE the pass return; a gate-driven repair already
        # routed this iteration wins (the gate evidence is more specific).
        _screw_repair_fired = False
        if render.error_class == "ok" and next_repair is None:
            _screw_det = _undersize_screw_hole(
                request, _scad_params(scad_source), extract_param_meta(scad)
            )
            if _screw_det is not None:
                _size, _val, _clr, _lbl = _screw_det
                _evidence = (
                    f"{_lbl} = {_mm(_val)} mm "
                    f"(below the {_size} clearance of {_mm(_clr)} mm)"
                )
                _ax_classified = ClassifiedFailure(
                    failure_class="geometrically_wrong",
                    evidence=_evidence,
                    repairable=True,
                )
                directive = route_repair(
                    classified=_ax_classified, scad_source=scad_source
                )
                if directive is not None:
                    failure_class = "geometrically_wrong"
                    _instr = (
                        f"{_size} clearance hole is {_mm(_val)} mm; printed "
                        f"{_size} clearance is {_mm(_clr)} mm. Model the hole "
                        f"at the clearance diameter ({_mm(_clr)} mm), not the "
                        f"nominal size, and state the clearance in the "
                        f"parameter's reason."
                    )
                    next_repair = {
                        "failure_class": "geometrically_wrong",
                        "instruction": _instr,
                        "scad_source": scad_source,
                        "evidence": _evidence,
                        "reason": SCREW_CLEARANCE_REASON,
                    }
                    _screw_repair_fired = True

        # Issue #386 (operator decision 2026-10-05): an ok render whose
        # gates are green can still carry a BLIND pocket where the user
        # asked for a through-hole (a pocket is invisible to all five
        # gate bits — the QA repro). The check runs BEFORE the pass
        # return (off the event loop — the STL load is real disk I/O).
        # Rides the EXISTING ``geometrically_wrong`` class — no new
        # class, no new error_class — routed through the same
        # ``route_repair`` path. A gate-driven repair already routed
        # this iteration wins; a fired screw-hole repair suppresses the
        # check (two post-repairs never fire on one iteration).
        # Gate order: screw → through → stack, each checking the prior
        # two (issue #409).
        _through_repair_fired = False
        if (
            render.error_class == "ok"
            and next_repair is None
            and not _screw_repair_fired
        ):
            _through_routed = await asyncio.to_thread(
                _through_hole_post_check,
                request,
                render.stl,
                through_baseline_genus,
                scad_source,
                through_baseline_genus_source,
            )
            if _through_routed is not None:
                _evidence, _instruction = _through_routed
                failure_class = "geometrically_wrong"
                next_repair = {
                    "failure_class": "geometrically_wrong",
                    "instruction": _instruction,
                    "scad_source": scad_source,
                    "evidence": _evidence,
                    "reason": THROUGH_HOLE_REASON,
                }
                _through_repair_fired = True

        # Issue #409: an ok render whose gates are green can still carry
        # a DISCARDED stack — the model wrote ``difference()`` where the
        # stacked feature belongs in a union (the v65 lid: declared
        # stack 11 mm, rendered slab 4 mm — invisible to every gate bit
        # when no H is stated or tagged). The check runs BEFORE the
        # pass return (pure text + the bbox the loop already measured —
        # zero extra renders). Rides the EXISTING
        # ``geometrically_wrong`` class — no new class, no new error
        # class, no new score bit — routed through the same
        # ``route_repair`` path. A gate-driven repair already routed
        # this iteration wins; a fired screw-hole or through-hole
        # repair suppresses the check (two post-repairs never fire on
        # one iteration).
        _stack_repair_fired = False
        if (
            render.error_class == "ok"
            and next_repair is None
            and not _screw_repair_fired
            and not _through_repair_fired
            and part_scale is None
        ):
            _stack_det = _stack_height_post_check(scad_source, bbox.z if bbox else None)
            if _stack_det is not None:
                _declared, _measured = _stack_det
                _evidence = (
                    f"declared stack sum {_declared:g} mm vs measured Z "
                    f"{_measured:g} mm"
                )
                _ax_classified = ClassifiedFailure(
                    failure_class="geometrically_wrong",
                    evidence=_evidence,
                    repairable=True,
                )
                directive = route_repair(
                    classified=_ax_classified, scad_source=scad_source
                )
                if directive is not None:
                    failure_class = "geometrically_wrong"
                    from d33d.stack_height_check import STACK_HEIGHT_INSTRUCTION

                    _stack_repair = directive.to_dict()
                    _stack_repair["instruction"] = (
                        f"The declared stack sums to {_declared:g} mm but the "
                        f"part renders {_measured:g} mm tall. "
                        f"{STACK_HEIGHT_INSTRUCTION}"
                    )
                    _stack_repair["reason"] = STACK_HEIGHT_REASON
                    next_repair = _stack_repair
                    _stack_repair_fired = True

        # Issue #419: an ok render whose gates are green can still be a
        # candidate whose rendered mesh EQUALS the parent's — the QA v100
        # repro (an import's missing semicolon made the difference() a
        # child of import(), so the "38 mm hole" edit re-exported the
        # parent identical, delta 0, and the loop passed "Your design is
        # ready"). The check runs BEFORE the pass return (off the event
        # loop — the STL load is real disk I/O), only on edit turns with
        # a stored parent mesh (``parent_mesh_stl`` — the seam measures
        # the parent's stored ``model.stl``; ``None`` abstains: a v1 has
        # no parent, a 3MF import stores no STL, a missing file is never
        # a fabricated baseline). Rides the EXISTING
        # ``geometrically_wrong`` class — no new class, no new
        # error_class — routed through the same ``route_repair`` path.
        # Gate order: screw → through → stack → unchanged (each checks
        # the prior three); a fired earlier repair suppresses the check
        # (two post-repairs never fire on one iteration).
        _unchanged_repair_fired = False
        if (
            render.error_class == "ok"
            and next_repair is None
            and not _screw_repair_fired
            and not _through_repair_fired
            and not _stack_repair_fired
            and parent_mesh_stl is not None
        ):
            _unchanged_det = await asyncio.to_thread(
                _unchanged_mesh_post_check,
                parent_mesh_stl,
                render,
                parent_fingerprint,
                parent_volume_mm3,
                parent_face_count,
            )
            if _unchanged_det is not None:
                _evidence, _instruction = _unchanged_det
                failure_class = "geometrically_wrong"
                next_repair = {
                    "failure_class": "geometrically_wrong",
                    "instruction": _instruction,
                    "scad_source": scad_source,
                    "evidence": _evidence,
                    "reason": MESH_UNCHANGED_REASON,
                }
                _unchanged_repair_fired = True

        # Issue #432: identical-repair stop. A post-check repair whose
        # rendered mesh equals the PREVIOUS iteration's (same post-check
        # reason) cannot move — stop after this attempt instead of repairing
        # to an identical mesh until the budget runs out.
        _post_reason = (
            next_repair.get("reason") if isinstance(next_repair, dict) else None
        )
        _repeat_post_check = False
        if _post_reason in POST_CHECK_REASONS:
            _post_fp = await asyncio.to_thread(_render_fingerprint, render.stl)
            if _post_fp is not None:
                _repeat_post_check = prev_post == (_post_reason, _post_fp)
                prev_post = (_post_reason, _post_fp)
            else:
                prev_post = None
        else:
            prev_post = None

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

        if candidate_score.perfect and not _screw_repair_fired and not _through_repair_fired and not _stack_repair_fired and not _unchanged_repair_fired:
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

        if _repeat_post_check:
            # Issue #432 operator decision: the repeated attempt N's mesh is
            # identical to attempt N-1's, so N-1 is the reported best and
            # the repeated post-check's reason is the terminal reason.
            # The prior RENDERED attempt's record (never iterations[-2], which
            # may be a non-render record in between).
            _prev = prev_rendered
            if _prev is not None:
                # A gate/error_class reason on N-1 always wins; the repeated
                # post-check reason is only the fallback (issue #432).
                return _exhausted(
                    iterations, _prev, _prev.score, fallback_reason=_post_reason
                )
            # No prior rendered attempt to report: not a stop, keep looping.
        prev_rendered = record

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


def _render_worker_image_detail(
    image: str = RENDER_WORKER_IMAGE,
    *,
    repo_root: Path | None = None,
    expected_hash: str | None = None,
    rebuild_command: str | None = None,
) -> dict[str, str] | None:
    """Pre-flight image probe (issue #346): the render-worker image must
    exist and carry a build-hash label matching the working tree.

    Reuses :func:`d33d.render_worker._verify_render_worker_image` (the same
    guard the per-render path runs) and maps its exceptions to the
    structured fault the terminal frame rides:

    * ``RuntimeError`` (verified image missing / label missing or
      mismatched) → ``{"reason": "image_missing" | "label_mismatch",
      ...}`` — the ``reason`` follows the verifier's own message
      (``image not found`` vs ``label mismatch/missing``), never reworded.
      ``label_mismatch`` carries ``expected`` (the working-tree
      :func:`build_hash`) and ``actual`` (the image's label, when the
      image itself exists); ``image_missing`` carries only ``expected``.
      ``rebuild_command`` is always the canonical command
      (``d33d.render_worker.canonical_build_command``).
    * ``OSError`` (a docker-query FAILURE — daemon down, binary missing,
      inspect timeout: the verifier deliberately re-raises it so callers
      can distinguish "cannot query docker" from a verified fault) →
      ``None``. The caller falls back to the retryable
      :data:`RENDERER_UNAVAILABLE` path (issue #277 semantics) — a daemon
      outage is never a terminal "rebuild the image" fault.

    Returns ``None`` when the image is present and the label matches
    (nothing wrong). A missing build input (``FileNotFoundError`` from
    :func:`build_hash` — an incomplete tree) degrades to ``None`` with one
    WARNING (the per-render guard's established mapping: not an image
    fault) — never a crash, never a fabricated fault.

    Bounded by the verifier's own 15 s inspect timeout (issue #277's
    "bounded by a timeout" requirement); the caller runs it off the event
    loop (``asyncio.to_thread``).
    """
    expected: str | None = None
    if expected_hash is None:
        try:
            expected = build_hash(repo_root)
        except FileNotFoundError as exc:
            logger.warning("render-worker image pre-flight skipped: %s", exc)
            return None
    else:
        expected = expected_hash
    try:
        _verify_render_worker_image(image, repo_root=repo_root, expected_hash=expected)
    except FileNotFoundError as exc:
        logger.warning("render-worker image pre-flight skipped: %s", exc)
        return None
    except OSError as exc:
        # A docker-query failure is "cannot query docker" — NOT staleness.
        # The caller's daemon check (or its absence) degrades this run to
        # renderer_unavailable (retryable), never the terminal rebuild fault.
        logger.debug(
            "render-worker image pre-flight could not query docker (%s) — "
            "returning None (no verified fault); the caller degrades to "
            "renderer_unavailable, never renderer_image_stale",
            exc,
        )
        return None
    except RuntimeError as exc:
        detail: dict[str, str] = {
            "rebuild_command": rebuild_command or canonical_build_command()
        }
        if expected is not None:
            detail["expected"] = expected
        if "image not found" in str(exc):
            detail["reason"] = "image_missing"
        else:
            detail["reason"] = "label_mismatch"
            actual = _image_label_or_none(image)
            if actual is not None:
                detail["actual"] = actual
        return detail
    return None


def default_image_check() -> dict[str, str] | None:
    """The design loop's default pre-flight image probe (issue #346):
    the canonical :func:`_render_worker_image_detail` construction — the
    render-worker image, no injected repo/expected overrides, and the
    canonical rebuild command. Shared by the loop's ``image_check is
    None`` branch and ``d33d.app``'s startup lifespan so the probe
    construction lives in one place. Looked up at call time (module
    global) so the conftest hermetic stub, which patches
    ``d33d.design_loop._render_worker_image_detail`` by name, still
    intercepts both call sites."""
    return _render_worker_image_detail(
        image=RENDER_WORKER_IMAGE,
        rebuild_command=canonical_build_command(),
    )


def _image_label_or_none(image: str) -> str | None:
    """The image's build-hash label, or ``None`` when it cannot be
    established (image absent, label missing/non-string, or a docker-query
    failure). Never a fabricated value (issue #346 disclosure must name
    only established numbers)."""
    try:
        from d33d.render_worker import BUILD_HASH_LABEL, _docker_image_labels

        labels = _docker_image_labels(image)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if not labels:
        return None
    value = labels.get(BUILD_HASH_LABEL)
    return value if isinstance(value, str) and value else None


def _image_stale_result(image_detail: dict[str, str]) -> DesignResult:
    """The synthetic :data:`RENDERER_IMAGE_STALE` pre-flight result (issue
    #346): the same zero-iteration exhausted shape as
    :data:`RENDERER_UNAVAILABLE`, carrying the structured ``renderer_detail``
    the terminal frame rides verbatim (no render ever ran — no fabricated
    render data)."""
    return DesignResult(
        status="exhausted",
        best=IterationRecord(iteration=0, scad_source="", render=None, score=None),
        iterations=(),
        failure_reason=RENDERER_IMAGE_STALE,
        iterations_used=0,
        renderer_detail=dict(image_detail),
    )


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
    part_scale: float | None = None,
    part_bbox_mm: tuple[float, float, float] | None = None,
    through_baseline_genus: int | None = None,
    through_baseline_genus_source: str | None = None,
    parent_mesh_stl: str | None = None,
    parent_fingerprint: tuple[str, int] | None = None,
    parent_volume_mm3: float | None = None,
    parent_face_count: int | None = None,
    renderer_check: Callable[[], bool] | None = None,
    image_check: Callable[[], dict[str, str] | None] | None = None,
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
            part_scale=part_scale,
            part_bbox_mm=part_bbox_mm,
            through_baseline_genus=through_baseline_genus,
            through_baseline_genus_source=through_baseline_genus_source,
            parent_mesh_stl=parent_mesh_stl,
            parent_fingerprint=parent_fingerprint,
            parent_volume_mm3=parent_volume_mm3,
            parent_face_count=parent_face_count,
            renderer_check=renderer_check,
            image_check=image_check,
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


async def _await_with_per_attempt_deadline(
    scad_co: Any,
    *,
    timeout: float,
    iteration: int,
    on_timeout: Callable[[], DesignResult],
) -> LLMResult | DesignResult:
    """Await one design LLM call under the loop's per-attempt deadline
    (issue #417) — THE single home for the design's two-tier timeout
    model.

    Two tiers bound a slow or hung design call, and both lead to the
    same keep-best ``design_loop_timed_out`` result: the INNER per-call
    bound (:data:`d33d.design_llm.LLM_CALL_TIMEOUT_SECONDS`, 120 s —
    the httpx per-request timeout on the production edge; a slow model
    trips it first and httpx raises its own
    ``httpx.TimeoutException``) and this OUTER per-attempt deadline
    (``asyncio.wait_for`` around the awaited call, whose timeout is the
    same 120 s in production — so the inner bound always wins for a
    slow call, and a truly hung one trips this deadline instead). On
    expiry the run ends WITH the best-so-far rendered candidate
    (``on_timeout`` builds the best-so-far exhausted result — the same
    result path an exhausted loop uses, with the structured
    ``design_loop_timed_out`` reason), never a fabricated empty result.

    The budget is per ATTEMPT (never a flat whole-loop total), so three
    slow attempts are not cut off by one flat lid, and no single LLM
    call may hang forever. Cancellation is a best-effort interruption,
    not a kill: the cancelled coroutine (and any ``to_thread`` worker
    thread it awaited on — a different lifetime, bounded by the render
    worker's own 120 s subprocess timeout one level down) may LINGER
    until its task is collected; the deadline here does not wait for the
    cancellation to finish, and must not.

    The loop OWNS this per-attempt LLM-call budget; the render that
    follows is bounded separately by the render worker's subprocess
    timeout, and post-checks by their own logic. The adapter's deadline
    (per-attempt budget × iteration cap + margin, in
    ``d33d.design_loop_events``) is a pure OUTER SAFETY NET that fires
    only for a run that stops yielding frames before the loop's own
    deadline can.
    """
    try:
        return await asyncio.wait_for(scad_co, timeout=timeout)
    except httpx.TimeoutException:
        # The INNER per-LLM-call bound fired (the full two-tier design
        # is in the docstring above): it must lead to the SAME keep-best
        # ``design_loop_timed_out`` result, not crash or exhaust (the
        # bug issue #417 is about). Only the per-call timeout is caught:
        # a cancellation (CancelledError) or a real error propagates
        # unchanged.
        logger.warning(
            "design loop attempt %s hit the per-LLM-call timeout (%ss) — "
            "returning the best-so-far candidate",
            iteration,
            timeout,
        )
        return on_timeout()
    except TimeoutError:
        logger.warning(
            "design loop attempt %s exceeded the %ss per-attempt deadline "
            "— returning the best-so-far candidate",
            iteration,
            timeout,
        )
        return on_timeout()


def _exhausted(
    iterations: list[IterationRecord],
    best: IterationRecord | None,
    best_score: Score | None,
    failure_reason: str | None = None,
    fallback_reason: str | None = None,
) -> DesignResult:
    """Build the exhaustion result: best-scoring candidate + a STRUCTURED
    failure reason (weakest gate bit of the best, never free text, never
    silently the last attempt).

    ``best`` is ``None`` ONLY when the per-attempt deadline cut the run
    off before any render (nothing rendered yet); the gate-bit
    derivation needs a scored candidate and is skipped in that case.

    ``failure_reason`` (issue #417) overrides the derived reason when the
    exhaustion is deadline-driven (the per-attempt deadline fired mid-run
    — the run's structured reason is the loop-level
    ``design_loop_timed_out``, never a gate bit of the best candidate
    which may have been a healthy render that simply didn't score a
    pass): ``None`` (the default) keeps today's derivation.

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
    reason: str | None = failure_reason
    if reason is None and best is not None:
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
        # Issue #419: a v2+ edit whose rendered mesh is unchanged from the
        # parent's (the QA v100 repro — the "38 mm hole" edit that re-exported
        # the parent identical, delta 0) exhausts with every gate bit green
        # (the mesh IS the parent — a valid, well-sized render), so the
        # weakest-gate-bit reason would be ``None`` and the terminal frame
        # would carry no ``reason`` at all. When the BEST iteration's repair
        # was the unchanged-mesh check's (``geometrically_wrong`` with the
        # "unchanged from the parent" evidence), swap in the structured
        # ``mesh_unchanged`` reason — the SPA maps it to the deck's
        # "The change didn't take — nothing in the part moved." copy.
        # A gate-driven or non-unchanged repair (a different failure_class,
        # or an unchanged-mesh repair that is NOT the best iteration's) does
        # NOT trigger the swap: the existing reason stands. The explicit
        # ``failure_reason`` override (the per-attempt deadline, issue #417)
        # wins — it is already set here, so this swap only fires when the
        # derivation found no reason at all.
        # Issue #432: the same rule for the through-hole, screw-clearance and
        # stack-height post-checks — each carries its own reason on the repair.
        if reason is None:
            _repair = getattr(best, "repair", None)
            _repair_reason = (
                _repair.get("reason") if isinstance(_repair, dict) else None
            )
            if _repair_reason in POST_CHECK_REASONS:
                reason = _repair_reason
    if reason is None:
        # Issue #432: the identical-repair stop's post-check reason, used only
        # when no gate or error_class reason could be derived.
        reason = fallback_reason
    return DesignResult(
        status="exhausted",
        best=best,
        iterations=tuple(iterations),
        failure_reason=reason,
        iterations_used=len(iterations),
    )
