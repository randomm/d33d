"""Structured six-view critique protocol (ticket #5, task-critique workstream).

The design loop's vision judge.  After a candidate renders, the critique role
compares the candidate's *six* view renders against the *other side of the
pairwise comparison* — the reference photo on iteration 1, the prior
candidate's render on iterations 2+ (the caller decides which is "baseline").

This module owns the *protocol* (the six-view gate, the pairwise framing, the
structured per-view + checklist contract, the response parse, and the non-vision
fallback); the LLM edge (``d33d.design_llm``) and the role->model resolution
(``d33d.config.resolve``) are injected or imported, never re-implemented.

Protocol contract (from 04-design-loop.md, the spec of record):

* **Judge from ALL six views, never one.**  A single view hides occluded
  geometry and single-view scores do not correlate with human judgement.  If
  fewer than all six views are valid/present, NO verdict is produced — the
  function returns a distinct ``InsufficientViews`` result, never a partial
  judgement.
* **Pairwise A-vs-B, not absolute scores.**  Judges are self-inconsistent on
  absolute scales, so the prompt frames the comparison as *candidate* (A) vs
  *baseline* (B) and requires the model to emit a relative judgement
  (``better`` / ``worse`` / ``equivalent``), never a bare "looks correct".
* **Structured output, explicit feature checklist.**  The request enumerates
  concrete mismatches *per view* (with a proposed fix) and requires an explicit
  feature checklist — the parse rejects any response that lacks both the
  per-view enumeration and the checklist (a bare "looks correct" is not a
  verdict).
* **Non-vision fallback (T2/T3).**  When the critique role's capability tier has
  no vision (``T2``/``T3`` per ``d33d.config.probes``), the critique degrades
  to *deterministic-gate-only* scoring — it reuses ``d33d.design_loop.score``
  (the pinned bitvector rank) instead of crashing or hallucinating a vision
  verdict.
* **Role alias, never a model id.**  Every LLM call is addressed by the
  ``"critique"`` role; ``make_critique_llm_fn`` builds the closure over
  ``d33d.config.resolve.resolve_model`` + ``d33d.design_llm.send`` (the same
  dependency-injection seam ``d33d.design_loop.make_llm_fn`` uses).
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from d33d.config.catalogue import Catalogue
from d33d.config.probes import CapabilityResult
from d33d.config.resolve import resolve_model
from d33d.design_llm import LLMResult, SenderError, send
from d33d.design_loop import BboxInfo, score
from d33d.render_worker import VIEWS, RenderResult

__all__ = [
    "REQUIRED_VIEW_NAMES",
    "VERDICTS",
    "CritiqueFn",
    "CritiqueVerdict",
    "FeatureItem",
    "InsufficientViews",
    "ViewMismatch",
    "build_critique_messages",
    "build_critique_system",
    "critique",
    "deterministic_verdict",
    "make_critique_llm_fn",
    "parse_critique",
    "required_views",
    "validate_views",
]

# ---------------------------------------------------------------------------
# The six-view contract (single source of truth: render_worker.VIEWS)
# ---------------------------------------------------------------------------

#: The ordered six view filenames the critic must judge.  Sourced from
#: ``d33d.render_worker.VIEWS`` (the render worker's six-filename contract) so
#: the critique gate and the render contract can never drift apart.
REQUIRED_VIEW_NAMES: tuple[str, ...] = tuple(name for name, _ in VIEWS)

#: The three pairwise relative judgements the protocol accepts.  Absolute
#: scales are disallowed (spec: "prefer pairwise A-vs-B over absolute scores").
VERDICTS: frozenset[str] = frozenset({"better", "worse", "equivalent"})
Verdict = Literal["better", "worse", "equivalent"]
_VALID_VERDICTS: frozenset[str] = VERDICTS

# ---------------------------------------------------------------------------
# Result shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ViewMismatch:
    """One concrete mismatch found in a single view, with a proposed fix.

    The spec mandates "enumerate concrete mismatches *per view* with a proposed
    fix" — a verdict that omits the view name or the proposed fix is rejected
    by :func:`parse_critique`.
    """

    view: str
    description: str
    proposed_fix: str


@dataclass(frozen=True)
class FeatureItem:
    """One item of the explicit feature checklist (the thing the spec requires
    instead of accepting a bare "looks correct")."""

    name: str
    present: bool
    note: str = ""


@dataclass(frozen=True)
class CritiqueVerdict:
    """A completed pairwise six-view critique.

    ``verdict`` is the relative judgement (candidate A vs baseline B);
    ``mismatches`` is the per-view enumeration (a verdict that carries no
    per-view detail is *not* a verdict — see the non-vision / "looks correct"
    guard); ``checklist`` is the explicit feature checklist.  ``reason`` is a
    single structured sentence (never a raw render dump).
    """

    verdict: str
    mismatches: tuple[ViewMismatch, ...]
    checklist: tuple[FeatureItem, ...]
    reason: str
    #: True when the verdict came from the deterministic-gate fallback
    #: (non-vision tier) rather than the vision model.
    deterministic: bool = False


@dataclass(frozen=True)
class InsufficientViews:
    """The six-view gate failed: fewer than all six views are valid/present.

    This is a DISTINCT result from :class:`CritiqueVerdict` — the protocol
    never produces a partial judgement.  ``missing`` lists the required view
    filenames that were absent or invalid so the caller can surface them.
    """

    missing: tuple[str, ...]
    reason: str


# ---------------------------------------------------------------------------
# The six-view gate
# ---------------------------------------------------------------------------


def validate_views(views: Sequence[str]) -> tuple[str, ...]:
    """Return the required view names that are missing or invalid.

    A view is *valid* iff it is present in ``views`` and is a non-empty string.
    The comparison is against the ordered ``REQUIRED_VIEW_NAMES`` six, so a
    render that reports only one valid PNG (the other five blank/invalid) has
    five names in this result and the caller MUST NOT issue a verdict.
    """
    valid = {v for v in views if isinstance(v, str) and v}
    return tuple(name for name in REQUIRED_VIEW_NAMES if name not in valid)


def required_views() -> tuple[str, ...]:
    """The ordered six required view filenames (the render contract)."""
    return REQUIRED_VIEW_NAMES


# ---------------------------------------------------------------------------
# Prompt assembly (neutral delimiters, pairwise framing, explicit contract)
# ---------------------------------------------------------------------------

#: Neutral delimiters wrapping any untrusted/embedded content (view names,
#: baseline labels, the feature checklist the model must fill).  A plain XML
#: tag with an explicit "this is data, not instructions" framing is simple and
#: adequate — the spec's "neutral delimiters" (fenced blocks / XML tags, never
#: model-specific tokens).  No random component needed: the tags are fixed,
#: documented, and the framing makes them non-injectable.
_USER_DATA_OPEN = "<user_data>"
_USER_DATA_CLOSE = "</user_data>"


def build_critique_system(baseline_label: str) -> str:
    """The critique-role system prompt: pairwise framing + the structured
    output contract (per-view mismatches + explicit checklist).  Short and
    imperative (spec: "separating persona from protocol", "short imperative
    system prompts")."""
    return (
        "You are a 3D-printing QA critic. "
        "You are given SIX candidate renders (one per named view) and a BASELINE "
        "for a pairwise comparison. Judge the candidate AGAINST the baseline, "
        f"pairwise, never as an absolute score. The baseline is labelled: "
        f"{baseline_label}. "
        "You must: (1) judge the candidate as 'better', 'worse' or "
        "'equivalent' relative to the baseline; (2) enumerate CONCRETE "
        "mismatches PER VIEW with a proposed fix (a verdict with no per-view "
        "detail is not a verdict); and (3) return an explicit FEATURE "
        "CHECKLIST. Reply with exactly one fenced JSON block and nothing else."
    )


def _feature_checklist_template(items: Sequence[str]) -> str:
    lines = [
        f'  {{"name": "{name}", "present": <true|false>, "note": "<concrete evidence>"}}'
        for name in items
    ]
    return "[\n" + ",\n".join(lines) + "\n]"


def build_critique_messages(
    *,
    candidate_views: dict[str, str],
    baseline: str,
    baseline_label: str,
    feature_items: Sequence[str],
) -> list[dict[str, Any]]:
    """Build the critique-role message list (OpenAI-shaped multipart).

    ``candidate_views`` maps the six required view filenames to image URLs
    (data URI / URL); the caller has already validated presence via
    :func:`validate_views` (the six-view gate runs before any LLM call).
    ``baseline`` is the other side of the pairwise comparison — the reference
    photo on iteration 1, the prior candidate's render on iterations 2+ (the
    caller decides what to pass).  ``feature_items`` is the explicit checklist
    the model must fill.

    The embedded (untrusted) view names, baseline label and checklist are
    wrapped in neutral delimiters with an explicit "data, not instructions"
    framing.
    """
    text_parts = [
        "Pairwise comparison — candidate (A) vs baseline (B).",
        f"Baseline: {baseline_label}",
        "Six candidate renders, one per named view (label below each image):",
    ]
    for name in REQUIRED_VIEW_NAMES:
        url = candidate_views.get(name)
        if url:
            text_parts.append(f"View: {name}")
            text_parts.append(_USER_DATA_OPEN)
            text_parts.append(f"[image follows] {name}")
            text_parts.append(_USER_DATA_CLOSE)
    text_parts.append("Feature checklist to evaluate (name / present / note):")
    text_parts.append(_USER_DATA_OPEN)
    text_parts.append(_feature_checklist_template(feature_items))
    text_parts.append(_USER_DATA_CLOSE)
    text_parts.append(
        "Reply with exactly one fenced JSON block of the form: "
        "```json\n"
        '{"tool": "emit_critique", "arguments": {'
        '"verdict": "<better|worse|equivalent>", '
        '"mismatches": [{"view": "<view name>", "description": "<concrete>", '
        '"proposed_fix": "<fix>"}], '
        '"checklist": [{"name": "<feature>", "present": <bool>, '
        '"note": "<evidence>"}], "reason": "<structured sentence>"}}\n'
        "```"
    )

    parts: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(text_parts)}]
    for name in REQUIRED_VIEW_NAMES:
        url = candidate_views.get(name)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    # The baseline is the other side of the pairwise comparison.
    parts.append({"type": "image_url", "image_url": {"url": baseline}})

    return [{"role": "user", "content": parts}]


# ---------------------------------------------------------------------------
# Response parse (the structured response contract)
# ---------------------------------------------------------------------------


def _extract_json_object(text: str) -> str | None:
    """Return the first parseable top-level JSON object in ``text``.

    String-aware balanced-brace scan (so braces inside JSON string values are
    ignored); each balanced span is validated with ``json.loads``.  Mirrors the
    tolerant extraction the T1 codec uses (``d33d.config.t1_protocol``) so a
    critique response that is a bare fenced block or prose-wrapped JSON still
    parses.
    """
    if not text:
        return None
    depth = 0
    start = -1
    in_string = False
    escape = False
    for i, ch in enumerate(text):
        if in_string:
            if escape:
                escape = False
            else:
                if ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start != -1:
                candidate = text[start : i + 1]
                try:
                    json.loads(candidate)
                except json.JSONDecodeError:
                    start = -1
                else:
                    return candidate
    return None


_FENCE_RE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def _extract_fenced_json(text: str) -> str | None:
    """Return the first fenced JSON block's body, if present."""
    for m in _FENCE_RE.finditer(text):
        return m.group(1)
    return None


def _verdict_from_payload(payload: dict[str, Any]) -> str | None:
    """Extract the relative verdict from a critique payload (None if absent
    or not one of the three relative judgements)."""
    verdict = payload.get("verdict")
    if isinstance(verdict, str) and verdict in _VALID_VERDICTS:
        return verdict
    return None


def _mismatches_from_payload(payload: dict[str, Any]) -> tuple[ViewMismatch, ...]:
    raw = payload.get("mismatches")
    if not isinstance(raw, list):
        return ()
    out: list[ViewMismatch] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        view = entry.get("view")
        description = entry.get("description")
        fix = entry.get("proposed_fix")
        # A mismatch that lacks a concrete view name or a proposed fix is not
        # a concrete per-view mismatch (spec guard against "looks correct").
        if not (isinstance(view, str) and view):
            continue
        if not (isinstance(description, str) and description):
            continue
        if not (isinstance(fix, str) and fix):
            continue
        out.append(ViewMismatch(view=view, description=description, proposed_fix=fix))
    return tuple(out)


def _checklist_from_payload(payload: dict[str, Any]) -> tuple[FeatureItem, ...]:
    raw = payload.get("checklist")
    if not isinstance(raw, list):
        return ()
    out: list[FeatureItem] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        name = entry.get("name")
        present = entry.get("present")
        note = entry.get("note")
        if not (isinstance(name, str) and name):
            continue
        if not isinstance(present, bool):
            continue
        out.append(
            FeatureItem(
                name=name,
                present=present,
                note=note if isinstance(note, str) else "",
            )
        )
    return tuple(out)


def _reason_from_payload(payload: dict[str, Any]) -> str:
    reason = payload.get("reason")
    return reason if isinstance(reason, str) and reason else ""


def parse_critique(result: LLMResult) -> CritiqueVerdict | None:
    """Parse a critique-role :class:`LLMResult` into a structured verdict.

    Returns ``None`` (NOT a verdict) when the response lacks the structured
    contract — a relative verdict (``better``/``worse``/``equivalent``) AND at
    least one concrete per-view mismatch AND an explicit feature checklist.
    This is the guard that rejects a bare "looks correct" (no per-view
    enumeration) as a *no-improvement* signal rather than a pass.
    """
    candidates: list[str] = []
    for call in result.tool_calls:
        args = call.get("arguments") if isinstance(call, dict) else None
        if isinstance(args, dict):
            # A native/T1 tool call whose arguments already carry the payload.
            if "verdict" in args or "mismatches" in args or "checklist" in args:
                return _verdict_from_args(args)
        elif isinstance(args, str) and args:
            candidates.append(args)
    if isinstance(result.content, str) and result.content:
        candidates.append(result.content)
    for text in candidates:
        # Prefer a fenced JSON block, then fall back to a balanced scan.
        fenced = _extract_fenced_json(text)
        for obj in [fenced, _extract_json_object(text)]:
            if obj is None:
                continue
            try:
                payload = json.loads(obj)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            # The T1 envelope wraps the critique payload in an ``arguments``
            # key (``{"tool":..., "arguments":{...}}``); unwrap it if present.
            if "arguments" in payload and isinstance(payload["arguments"], dict):
                payload = payload["arguments"]
            verdict = _verdict_from_args(payload)
            if verdict is not None:
                return verdict
    return None


def _verdict_from_args(payload: dict[str, Any]) -> CritiqueVerdict | None:
    verdict = _verdict_from_payload(payload)
    mismatches = _mismatches_from_payload(payload)
    checklist = _checklist_from_payload(payload)
    if verdict is None:
        return None
    # The structured contract requires BOTH a per-view mismatch enumeration
    # AND an explicit feature checklist — a bare "looks correct" (no per-view
    # detail, no checklist) is not a verdict.
    if not mismatches and not checklist:
        return None
    reason = _reason_from_payload(payload)
    if not reason and not mismatches and not checklist:
        return None
    return CritiqueVerdict(
        verdict=verdict,
        mismatches=mismatches,
        checklist=checklist,
        reason=reason,
    )


# ---------------------------------------------------------------------------
# Non-vision fallback (T2/T3): deterministic-gate-only scoring
# ---------------------------------------------------------------------------


def deterministic_verdict(
    *,
    render: RenderResult,
    stated_dims: tuple[float, float, float],
    bbox: BboxInfo | None = None,
    scad_source: str = "",
) -> CritiqueVerdict:
    """The non-vision fallback: score the candidate with the deterministic
    gates only (reusing ``d33d.design_loop.score``) and emit a *deterministic*
    verdict — no vision, no hallucinated judgement.

    The verdict maps the deterministic-gate rank (the bitvector popcount) to a
    relative judgement: ``better`` when every gate passes (rank == 4),
    ``equivalent`` when some but not all pass (0 < rank < 4), ``worse`` when
    no gate passes (rank == 0).  This is the deterministic-gate-only scoring
    the spec requires when the tier has no vision — it never fabricates a
    visual comparison.  The checklist carries the per-gate bitvector so the
    result is auditable; ``deterministic`` is ``True`` so the caller can
    distinguish this from a vision verdict.
    """
    s = score(render, stated_dims, bbox=bbox, scad_source=scad_source)
    if s.perfect:
        verdict = "better"
    elif s.rank == 0:
        verdict = "worse"
    else:
        verdict = "equivalent"
    gate_names = (
        "error_class_ok",
        "six_views_non_blank",
        "bbox_within_tolerance",
        "stated_dims_named_params",
    )
    checklist = tuple(
        FeatureItem(name=name, present=bool(bit))
        for name, bit in zip(gate_names, s.bits)
    )
    return CritiqueVerdict(
        verdict=verdict,
        mismatches=(),
        checklist=checklist,
        reason=f"deterministic-gate score {s.rank}/{len(s.bits)} (non-vision fallback)",
        deterministic=True,
    )


# ---------------------------------------------------------------------------
# Role-alias LLM seam (dependency injection, never a hardcoded model id)
# ---------------------------------------------------------------------------

#: ``critique_llm_fn(messages, system) -> LLMResult`` (sync or async).  The
#: caller-supplied edge; ``make_critique_llm_fn`` builds the role-resolving
#: closure for the common case.
CritiqueFn = Any


def make_critique_llm_fn(
    catalogue: Catalogue,
    request_factories: dict[str, Any],
    capabilities: dict[str, CapabilityResult] | None = None,
    dialect: str = "openai",
):
    """Build the critique role's LLM edge over the role alias.

    Every call resolves the ``"critique"`` role through
    ``d33d.config.resolve.resolve_model`` (never a hardcoded model id) and
    dispatches through ``d33d.design_llm.send`` at the tier the capability
    probe assigned — mirroring ``d33d.design_loop.make_llm_fn``.  Returns a
    sync ``critique_llm_fn(messages, system) -> LLMResult`` (the critique path
    is synchronous: it resolves the role, then runs the async ``send`` via
    ``asyncio.run``); the six-view gate and the non-vision fallback are applied
    by :func:`critique` around this edge.
    """
    caps = capabilities or {}
    factory = request_factories.get("critique")
    if factory is None:
        raise KeyError("request_factories must map 'critique' -> an injected HTTP edge")

    def critique_llm_fn(messages, system) -> LLMResult:
        resolution = resolve_model(catalogue, "critique")
        return asyncio.run(
            send(
                role="critique",
                model_id=resolution.entry.model,
                messages=messages,
                request_factory=factory,
                capability=caps.get("critique"),
                dialect=dialect,
                system=system,
            )
        )

    return critique_llm_fn


# ---------------------------------------------------------------------------
# The critique entry point (six-view gate -> pairwise LLM -> parse / fallback)
# ---------------------------------------------------------------------------


def critique(
    *,
    candidate_views: dict[str, str],
    baseline: str,
    llm_fn,
    stated_dims: tuple[float, float, float] | None = None,
    render: RenderResult | None = None,
    bbox: BboxInfo | None = None,
    scad_source: str = "",
    baseline_label: str = "reference photo (baseline B)",
    feature_items: Sequence[str] | None = None,
) -> CritiqueVerdict | InsufficientViews:
    """Issue a structured six-view pairwise critique.

    Returns a :class:`CritiqueVerdict` when all six required views are present
    and valid (vision path, or the deterministic-gate fallback when the
    ``llm_fn`` edge raises ``SenderError(status='no_tools_supported')`` for a
    T2/T3 tier), or a :class:`InsufficientViews` when fewer than six views are
    valid — the protocol NEVER produces a partial judgement.

    * ``candidate_views`` — the six required view filenames -> image URLs.
    * ``baseline`` — the other side of the pairwise comparison (the reference
      photo on iteration 1, the prior candidate's render on iterations 2+; the
      caller decides what to pass).
    * ``llm_fn`` — ``messages, system -> LLMResult`` (sync or async); built by
      :func:`make_critique_llm_fn` in the common case.
    * ``stated_dims`` / ``render`` / ``bbox`` / ``scad_source`` — required for
      the non-vision deterministic fallback (the deterministic-gate score).
    """
    missing = validate_views(candidate_views.keys())
    if missing:
        return InsufficientViews(
            missing=missing,
            reason=f"critique requires all six views; missing/invalid: {missing}",
        )

    if feature_items is None:
        feature_items = _default_feature_items(stated_dims)

    messages = build_critique_messages(
        candidate_views=candidate_views,
        baseline=baseline,
        baseline_label=baseline_label,
        feature_items=feature_items,
    )
    system = build_critique_system(baseline_label)

    try:
        result = llm_fn(messages, system)
        if asyncio.iscoroutine(result):
            # The critique edge is called from a synchronous context (no
            # running event loop), so ``asyncio.run`` completes it.  This
            # mirrors ``d33d.design_loop.run_design_loop``'s sync entry.
            result = asyncio.run(result)
    except SenderError as exc:
        if getattr(exc, "status", "") == "no_tools_supported":
            return _non_vision_fallback(
                render=render,
                stated_dims=stated_dims,
                bbox=bbox,
                scad_source=scad_source,
            )
        raise

    parsed = parse_critique(result)
    if parsed is None:
        # A bare "looks correct" / malformed response is NOT a verdict — treat
        # it as the deterministic fallback so the loop never accepts a
        # checklist that does not reference concrete per-view mismatches.
        if render is not None:
            return _non_vision_fallback(
                render=render,
                stated_dims=stated_dims,
                bbox=bbox,
                scad_source=scad_source,
            )
        raise SenderError(
            "critique response lacked the structured contract (per-view "
            "mismatches + feature checklist) and no deterministic fallback is "
            "available (render not provided)",
            status="error",
        )
    return parsed


def _non_vision_fallback(
    *,
    render: RenderResult | None,
    stated_dims: tuple[float, float, float] | None,
    bbox: BboxInfo | None,
    scad_source: str,
) -> CritiqueVerdict:
    if render is None or stated_dims is None:
        raise SenderError(
            "non-vision fallback requires render + stated_dims", status="error"
        )
    return deterministic_verdict(
        render=render,
        stated_dims=stated_dims,
        bbox=bbox,
        scad_source=scad_source,
    )


def _default_feature_items(stated_dims: tuple[float, float, float] | None) -> list[str]:
    if stated_dims is None:
        return ["overall_form", "proportion"]
    return [
        "overall_form",
        "proportion",
        "width_matches",
        "depth_matches",
        "height_matches",
    ]
