"""Visual review of a gate-passing candidate: request versus render (issue #435).

A leaf module: it imports nothing from ``d33d.design_loop`` (the design loop
imports this module at load time), so the review pieces and the six-view and
JSON helpers they share live here. ``d33d.critique_protocol`` re-exports the
public names.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from d33d.config.probes import CapabilityResult
from d33d.design_llm import LLMResult, SenderError
from d33d.render_worker import VIEWS, RenderResult

__all__ = [
    "REQUIRED_VIEW_NAMES",
    "REVIEW_REASON_MAX_CHARS",
    "ReviewVerdict",
    "build_review_messages",
    "build_review_system",
    "parse_review",
    "render_view_data_uris",
    "review_render",
    "review_vision_enabled",
    "validate_views",
]

logger = logging.getLogger(__name__)

#: The model-authored fail reason is bounded before it becomes repair evidence
#: (issue #435).
REVIEW_REASON_MAX_CHARS = 300

#: The ordered six view filenames the critic must judge.  Sourced from
#: ``d33d.render_worker.VIEWS`` (the render worker's six-filename contract) so
#: the critique gate and the render contract can never drift apart.
REQUIRED_VIEW_NAMES: tuple[str, ...] = tuple(name for name, _ in VIEWS)

def validate_views(views: Sequence[str]) -> tuple[str, ...]:
    """Return the required view names that are missing or invalid.

    A view is *valid* iff it is present in ``views`` and is a non-empty string.
    The comparison is against the ordered ``REQUIRED_VIEW_NAMES`` six, so a
    render that reports only one valid PNG (the other five blank/invalid) has
    five names in this result and the caller MUST NOT issue a verdict.
    """
    valid = {v for v in views if isinstance(v, str) and v}
    return tuple(name for name in REQUIRED_VIEW_NAMES if name not in valid)


#: Neutral delimiters wrapping any untrusted/embedded content (view names,
#: baseline labels, the feature checklist the model must fill).  A plain XML
#: tag with an explicit "this is data, not instructions" framing is simple and
#: adequate — the spec's "neutral delimiters" (fenced blocks / XML tags, never
#: model-specific tokens).  No random component needed: the tags are fixed,
#: documented, and the framing makes them non-injectable.
_USER_DATA_OPEN = "<user_data>"
_USER_DATA_CLOSE = "</user_data>"


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


# ---------------------------------------------------------------------------
# Request-versus-render review of a gate-passing candidate (issue #435)
# ---------------------------------------------------------------------------
#
# Unlike the pairwise :func:`critique` (which stays unwired), this review asks
# one question: does the rendered part match what the user asked for? It runs
# through the role-aware ``llm_fn(role, messages, system)`` edge with role
# ``"critique"``, so the request_logs row and the role's tool plumbing are the
# same ones ``make_critique_llm_fn`` uses.


@dataclass(frozen=True)
class ReviewVerdict:
    """The outcome of :func:`review_render`.

    ``status`` is ``"pass"`` (the render matches the request), ``"fail"``
    (``reason`` names the concrete mismatch and routes as repair evidence) or
    ``"unreviewed"`` (``why`` names why no visual judgement was made). An
    unreviewed candidate still passes; the pass card says it was not visually
    reviewed.
    """

    status: Literal["pass", "fail", "unreviewed"]
    reason: str = ""
    why: str = ""

    @classmethod
    def passed(cls) -> ReviewVerdict:
        return cls(status="pass")

    @classmethod
    def failed(cls, reason: str) -> ReviewVerdict:
        return cls(status="fail", reason=reason)

    @classmethod
    def unreviewed(cls, why: str) -> ReviewVerdict:
        return cls(status="unreviewed", why=why)


def render_view_data_uris(render: RenderResult) -> dict[str, str]:
    """The six view PNGs of a render as data URIs, read from its durable
    artifact directory. ``{}`` when the directory or any of the six files is
    missing: a review never judges a partial set (the six-view rule)."""
    artifact = getattr(render, "render_artifact_dir", None)
    if not isinstance(artifact, str) or not artifact:
        return {}
    base = Path(artifact)
    out: dict[str, str] = {}
    for name in REQUIRED_VIEW_NAMES:
        try:
            raw = (base / name).read_bytes()
        except OSError:
            return {}
        out[name] = "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    return out


def build_review_system() -> str:
    return (
        "You are a 3D-printing QA reviewer. You are given SIX renders of a "
        "candidate part (one per named view), the user's request, and "
        "optionally a reference photo of the real object. Decide whether the "
        "render matches the request. Answer 'fail' only for a concrete, named "
        "mismatch; answer 'pass' when the render matches. Reply with exactly "
        "one fenced JSON block and nothing else."
    )


def build_review_messages(
    *,
    views: dict[str, str],
    request_text: str,
    reference_photo: str | None,
) -> list[dict[str, Any]]:
    """The review-role message list: request (as data), six views, optional
    reference photo."""
    text_parts = [
        "Review: does the rendered part match the user's request?",
        "Request:",
        _USER_DATA_OPEN,
        request_text.strip() or "(no request text)",
        _USER_DATA_CLOSE,
        "Six candidate renders, one per named view (labels below each image):",
    ]
    for name in REQUIRED_VIEW_NAMES:
        text_parts.append(f"View: {name}")
    if reference_photo:
        text_parts.append("A reference photo of the real object follows the renders.")
    text_parts.append(
        "Reply with exactly one fenced JSON block of the form: "
        "```json\n"
        '{"verdict": "<pass|fail>", "reason": "<one sentence: the concrete '
        'mismatch for fail, or what matches for pass>"}\n'
        "```"
    )
    parts: list[dict[str, Any]] = [{"type": "text", "text": "\n".join(text_parts)}]
    for name in REQUIRED_VIEW_NAMES:
        parts.append({"type": "image_url", "image_url": {"url": views[name]}})
    if reference_photo:
        parts.append({"type": "image_url", "image_url": {"url": reference_photo}})
    return [{"role": "user", "content": parts}]


def _review_from_payload(payload: dict[str, Any]) -> ReviewVerdict | None:
    body = payload.get("arguments")
    if isinstance(body, dict):
        payload = body
    verdict = payload.get("verdict")
    reason = payload.get("reason")
    reason = reason.strip() if isinstance(reason, str) else ""
    if verdict == "pass":
        return ReviewVerdict.passed()
    if verdict == "fail" and reason:
        return ReviewVerdict.failed(reason[:REVIEW_REASON_MAX_CHARS])
    # A fail with no concrete reason, or any other verdict, is not a verdict.
    return None


def parse_review(result: LLMResult) -> ReviewVerdict | None:
    """Parse a review reply into a verdict; ``None`` when the reply carries
    no usable ``pass`` / ``fail``-with-reason (the caller maps it to
    ``unreviewed``)."""
    candidates: list[str] = []
    for call in result.tool_calls:
        args = call.get("arguments") if isinstance(call, dict) else None
        if isinstance(args, dict) and "verdict" in args:
            verdict = _review_from_payload(args)
            if verdict is not None:
                return verdict
        elif isinstance(args, str) and args:
            candidates.append(args)
    if isinstance(result.content, str) and result.content:
        candidates.append(result.content)
    for text in candidates:
        for obj in (_extract_fenced_json(text), _extract_json_object(text)):
            if obj is None:
                continue
            try:
                payload = json.loads(obj)
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            verdict = _review_from_payload(payload)
            if verdict is not None:
                return verdict
    return None


def review_vision_enabled(capability: CapabilityResult | None) -> bool:
    """Whether the visual review may send images: the probed vision flag of
    the resolved model (issue #435). The tier label is not consulted — a
    tools-only T1 model has no vision and must skip the review."""
    return capability is not None and bool(capability.vision)


async def review_render(
    views: dict[str, str],
    request_text: str,
    reference_photo: str | None = None,
    *,
    llm_fn: Any,
) -> ReviewVerdict:
    """Review one gate-passing candidate's six renders against the request.

    ``views`` maps the six view filenames to image data URIs (see
    :func:`render_view_data_uris`). ``llm_fn(role, messages, system)`` is the
    loop's role-aware edge (sync or async); the call is made with role
    ``"critique"``. Never raises for a call-side failure: a non-vision tier
    (``SenderError`` ``no_tools_supported``), any other call error, or an
    unparseable reply returns ``unreviewed`` with the reason. The per-attempt
    timeout is the caller's (the loop wraps this in its own ``wait_for``).
    """
    missing = validate_views([name for name, url in views.items() if url])
    if missing:
        return ReviewVerdict.unreviewed("views_missing")
    messages = build_review_messages(
        views=views, request_text=request_text, reference_photo=reference_photo
    )
    try:
        # A sync llm_fn runs off the event loop so the caller's wait_for budget
        # can cut it off; an async one is awaited directly.
        if inspect.iscoroutinefunction(llm_fn):
            result = await llm_fn("critique", messages, build_review_system())
        else:
            result = await asyncio.to_thread(
                llm_fn, "critique", messages, build_review_system()
            )
            if inspect.isawaitable(result):
                result = await result
    except SenderError as exc:
        if getattr(exc, "status", "") == "no_tools_supported":
            return ReviewVerdict.unreviewed("no_vision")
        logger.warning("visual review call failed: %s", exc)
        return ReviewVerdict.unreviewed("call_error")
    except Exception as exc:
        # Any other failure on the edge degrades to unreviewed: a failing review
        # must never lose a candidate that already passed every gate.
        # CancelledError is a BaseException and still propagates.
        logger.warning("visual review call failed: %s", exc, exc_info=True)
        return ReviewVerdict.unreviewed("call_error")
    # The parse runs on whatever the role edge returned; a malformed reply
    # object must degrade to unreviewed, never abort the run (issue #435).
    try:
        parsed = parse_review(result)
    except Exception as exc:
        logger.warning("visual review reply unparseable: %s", exc, exc_info=True)
        return ReviewVerdict.unreviewed("unparseable")
    if parsed is None:
        return ReviewVerdict.unreviewed("unparseable")
    return parsed
