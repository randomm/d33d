"""Vision judge (issue #9, workstream task-harness).

The one non-deterministic stage of the eval harness. It takes a
gate-passing candidate — the case's renders (view PNGs when present on
disk, base64 ``data:`` URIs) plus the marked-region PNG, the design
call's OpenSCAD source, and the case's expected outcome — and asks the
configured vision model (an OpenAI-compatible endpoint, resolved from
the model catalogue — never hardcoded here) for a structured
pass/fail with a reason.

Design rules:

* **Server-side, key-in-factory.** The judge holds no key: the caller
  resolves the ``design`` role via ``d33d.config.resolve.resolve_model``
  and injects the request factory (the same seam the design loop's
  ``llm_fn`` uses — ``d33d.design_llm.send``). The key stays inside the
  factory's ``Authorization`` header and never appears in a message, a
  hash, or a report. The API key never reaches the browser.
* **Structured verdict, closed outcome classes.** The judge returns a
  :class:`JudgeVerdict` with ``passed`` (bool) and a ``reason`` (the
  model's one-line explanation, truncated). For adversarial cases the
  verdict's ``failure_class`` is one of the two outcome classes
  (``graceful_refusal`` / ``clearance_applied``) — the classes
  adversarial cases are scored against, never compile failures.
* **The judge is the last stage.** ``d33d.evals.harness.run_case``
  invokes the judge only after the gate phase passed — the spec's
  "7 deterministic gates before any vision judge" is enforced by the
  harness's short-circuit, not by a flag in the judge.

The judge's own LLM call is injected as ``request_factory`` (so tests
mock it without a real endpoint); when the model answers with
unparseable prose the judge fails closed (``passed=False``,
``failure_class='unclassified_syntax_error'``) rather than guessing.
"""

from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: The LLM request seam (same shape as the harness's): one JSON request
#: body -> one OpenAI-shaped response. The API key stays inside the
#: factory (the ``Authorization`` header) — never in a message or a
#: report.
RequestFactory = Callable[[dict[str, Any]], Awaitable[Any]]

#: The judge's fixed prompt. The model is asked for a structured
#: ``{"pass": bool, "reason": str}`` JSON object — the fenced-JSON
#: fallback (T1) parses the same shape.
JUDGE_PROMPT = (
    "You are evaluating a 3D-printable design generated from a request. "
    "Compare the reference image(s) and the rendered views against the "
    "OpenSCAD source. Reply with a single JSON object: "
    '{"pass": true|false, "reason": "<one sentence>"}'
)

#: Hard cap on the reason string in the report (a runaway model answer
#: must not dominate the report row).
MAX_REASON_CHARS = 512


@dataclass(frozen=True)
class JudgeInput:
    """One judge call's inputs (the gate-passing candidate).

    ``scad_source`` is the design call's output (the refusal text for
    adversarial cases). ``stl_path`` is the case's rendered STL (the
    judge does not load it — the gates already validated the geometry;
    it is carried for the report). ``model_id`` is the resolved
    vision-model id (configured via the catalogue, never hardcoded);
    ``request_factory`` is the injected request path.
    """

    case: Any
    repo_root: Path
    scad_source: str
    stl_path: str | None
    model_id: str
    request_factory: RequestFactory


@dataclass(frozen=True)
class JudgeVerdict:
    """The judge's structured verdict.

    ``passed`` is the pass/fail; ``reason`` is the model's one-line
    explanation (capped at :data:`MAX_REASON_CHARS`); ``failure_class``
    is set on a fail to the outcome class the verdict maps to
    (``graceful_refusal`` / ``clearance_applied`` for adversarial
    cases, ``unclassified_syntax_error`` for a malformed judge answer).
    """

    passed: bool
    reason: str
    failure_class: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            "failure_class": self.failure_class,
        }


def _image_part(repo_root: Path, rel_path: str) -> dict[str, Any] | None:
    """One ``image_url`` content part for an on-disk image (base64).

    ``None`` when the file is absent or not an image (the judge then
    evaluates on the source + text only — the renders are a reference,
    not a requirement for a verdict).
    """
    full = repo_root / rel_path
    if not full.is_file():
        return None
    if full.suffix.lower() not in (".png", ".jpg", ".jpeg", ".webp", ".gif"):
        return None
    encoded = base64.b64encode(full.read_bytes()).decode("ascii")
    mime = full.suffix.lower().lstrip(".")
    return {
        "type": "image_url",
        "image_url": {"url": f"data:{mime};base64,{encoded}"},
    }


def _judge_messages(
    judge_input: JudgeInput,
) -> tuple[str, list[dict[str, Any]]]:
    """The judge's ``(system, messages)`` pair.

    The system prompt is :data:`JUDGE_PROMPT`; the user turn is the
    case's request + OpenSCAD source + one part per rendered view /
    reference photo (base64 ``image_url`` when the file is an on-disk
    image, a text reference otherwise).
    """
    case = judge_input.case
    parts: list[dict[str, Any]] = [
        {
            "type": "text",
            "text": (
                f"Request: {case.request}\n\n"
                f"OpenSCAD source:\n{judge_input.scad_source}"
            ),
        }
    ]
    if case.reference_photo:
        part = _image_part(judge_input.repo_root, case.reference_photo)
        parts.append(
            part
            if part is not None
            else {
                "type": "text",
                "text": f"Reference source (provided as text): {case.reference_photo}",
            }
        )
    for view in case.rendered_views:
        part = _image_part(judge_input.repo_root, view)
        parts.append(
            part
            if part is not None
            else {
                "type": "text",
                "text": f"Rendered view (provided as text): {view}",
            }
        )
    return JUDGE_PROMPT, [{"role": "user", "content": parts}]


def _verdict_from_content(content: str, case: Any) -> JudgeVerdict:
    """Parse the model's reply into a :class:`JudgeVerdict`.

    The judge expects ``{"pass": bool, "reason": str}`` JSON. A reply
    that is not parseable (prose, an empty string, a wrong shape)
    fails closed: ``passed=False`` with
    ``failure_class='unclassified_syntax_error'`` — a malformed judge
    answer is not a pass.

    For adversarial cases the verdict's ``failure_class`` is the
    outcome class the model signalled (``graceful_refusal`` /
    ``clearance_applied``) when the reply names one; otherwise the
    case's expected outcome (a fail that is still one of the two
    outcome classes is tagged with the expected class, so the report
    can attribute it to the adversarial scoring).
    """
    import json

    try:
        obj = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return JudgeVerdict(
            passed=False,
            reason=f"judge reply not parseable JSON: {content[:128]!r}",
            failure_class="unclassified_syntax_error",
        )
    if not isinstance(obj, dict) or "pass" not in obj:
        return JudgeVerdict(
            passed=False,
            reason=f"judge reply missing 'pass': {content[:128]!r}",
            failure_class="unclassified_syntax_error",
        )
    passed = bool(obj["pass"])
    reason = str(obj.get("reason", ""))[:MAX_REASON_CHARS]

    failure_class: str | None = None
    if not passed:
        spec = getattr(case, "adversarial", None)
        if spec is not None:
            failure_class = spec.expected_outcome
        else:
            failure_class = "geometrically_wrong"

    return JudgeVerdict(passed=passed, reason=reason, failure_class=failure_class)


async def judge_render(judge_input: JudgeInput) -> JudgeVerdict:
    """One vision-judge call over the gate-passing candidate.

    Assembles the judge's messages (request + OpenSCAD source + renders
    / marked-region PNGs as base64 ``image_url`` parts), makes the
    injected LLM call, and parses the reply into a
    :class:`JudgeVerdict`. The model is the one the caller resolved via
    the catalogue (``judge_input.model_id``) — never hardcoded here.
    The key stays inside ``judge_input.request_factory``.

    A non-dict response body, a missing ``choices`` list, or a non-dict
    message fails closed (``passed=False``,
    ``failure_class='unclassified_syntax_error'``) — a malformed
    response is not a pass.
    """
    system, messages = _judge_messages(judge_input)
    try:
        response = await judge_input.request_factory(
            {
                "model": judge_input.model_id,
                "messages": [{"role": "system", "content": system}, *messages],
            }
        )
        data = response.json()
    except (AttributeError, ValueError) as e:
        return JudgeVerdict(
            passed=False,
            reason=f"judge call failed: {e}",
            failure_class="unclassified_syntax_error",
        )
    if not isinstance(data, dict):
        return JudgeVerdict(
            passed=False,
            reason="judge response body is not a JSON object",
            failure_class="unclassified_syntax_error",
        )
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        return JudgeVerdict(
            passed=False,
            reason="judge response has no choices",
            failure_class="unclassified_syntax_error",
        )
    message = choices[0].get("message") if isinstance(choices[0], dict) else None
    if not isinstance(message, dict):
        return JudgeVerdict(
            passed=False,
            reason="judge response message is not an object",
            failure_class="unclassified_syntax_error",
        )
    content = message.get("content")
    if not isinstance(content, str):
        content = ""
    return _verdict_from_content(content, judge_input.case)
