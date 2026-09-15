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


# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BboxInfo:
    """Per-axis rendered extents (mm) plus volume — the gate inputs."""

    x: float
    y: float
    z: float
    volume: float = 0.0


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
    #: True iff the bbox bit is True only because no stated dimension was
    #: known (a zero/absent axis) and the gate ABSTAINED (ticket #91).
    #: Separate from the bitvector on purpose: the bit ordering, the
    #: ``tiebreak`` tuple, and ``GATE_REASON_BITS`` are unchanged, yet a
    #: downstream consumer can never claim dimensions were VERIFIED on an
    #: abstained gate.
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
    unmarked pass."""
    for extent, target in zip((bbox.x, bbox.y, bbox.z), stated):
        if target <= 0:
            return True
        tol = max(BBOX_TOLERANCE_REL * target, BBOX_TOLERANCE_MIN_MM)
        if abs(extent - target) > tol:
            return False
    return True


def _views_non_blank(render: RenderResult) -> bool:
    """True iff the render reports exactly six non-empty view filenames."""
    return len(render.views) == 6 and all(
        isinstance(v, str) and v for v in render.views
    )


def _named_params_present(scad_source: str) -> bool:
    """True iff the .scad declares a named-parameter block AND has no
    un-declared multi-digit geometry literals (the "magic numbers" check
    from ``d33d.failure_classes``)."""
    if detect_magic_numbers(scad_source):
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
       as magic-number literals (spec acceptance #4)

    Ranked by popcount; ties break on the raw bitvector tuple
    (deterministic, earlier bits first).

    ``Score.bbox_abstained`` (ticket #91) marks a bbox bit that is True
    merely because no stated dimension was known (a zero/absent axis —
    :func:`_bbox_within_tolerance` abstains on an unknown target). It is a
    SEPARATE field, not a fifth bit, so the bit ordering, the ``tiebreak``
    tuple, and ``GATE_REASON_BITS`` names are all unchanged while the
    abstention stays distinguishable from a measured pass.
    """
    bits = (
        render.error_class == "ok",
        _views_non_blank(render),
        bbox is not None and _bbox_within_tolerance(bbox, stated_dims),
        _named_params_present(scad_source),
    )
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


def _design_messages(
    *,
    photo: str,
    chat_history: Sequence[str],
    stated: tuple[float, float, float],
    repair: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """The design-role message list: photo + chat + dimensions as named
    parameters + (on repair iterations) the structured failure directive —
    the tagged class and instruction, never a raw stderr dump."""
    lines: list[str] = []
    for turn in chat_history:
        lines.append(f"chat: {turn}")
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
        scad = await _call(
            llm_fn,
            "design",
            _design_messages(
                photo=photo,
                chat_history=chat_history,
                stated=stated_dims,
                repair=repair,
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
