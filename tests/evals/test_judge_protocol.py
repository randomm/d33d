"""The vision judge returns a structured pass/fail with a reason
(issue #9, workstream task-harness).

Covers (all mocked — no real LLM call, no real Docker):
- a well-formed ``{"pass": true, "reason": ...}`` reply yields
  ``JudgeVerdict(passed=True, reason=...)`` with ``failure_class=None``
- a well-formed ``{"pass": false, ...}`` reply yields ``passed=False``
  with the case's expected outcome class (adversarial) or
  ``geometrically_wrong`` (non-adversarial)
- an unparseable reply (prose, empty, wrong shape) fails closed:
  ``passed=False`` + ``unclassified_syntax_error``
- a malformed transport response (no ``.json()``, non-dict body, no
  choices) fails closed the same way
- the judge sends the model id from the input (configured, never
  hardcoded) and never puts the API key in the message
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from d33d.evals.case_schema import (
    AdversarialSpec,
    GoldenCase,
    PromptPin,
)
from d33d.evals.judge import (
    JUDGE_PROMPT,
    JudgeInput,
    JudgeVerdict,
    judge_render,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _pin() -> PromptPin:
    return PromptPin(
        prompt_version="v1",
        path="evals/prompts/primitive_design_v1.md",
        sha256=Path("evals/prompts/primitive_design_v1.md")
        and _real_sha256(),
    )


def _real_sha256() -> str:
    import hashlib

    return hashlib.sha256(
        (REPO_ROOT / "evals" / "prompts" / "primitive_design_v1.md").read_bytes()
    ).hexdigest()


def _case(kind: str = "primitive") -> GoldenCase:
    return GoldenCase(
        case_id="judge-box",
        kind=kind,  # type: ignore[arg-type]
        prompt=_pin(),
        request="A 20mm box",
        expected_dims=(
            {"x": 20, "y": 20, "z": 20} if kind != "adversarial" else None
        ),
        gate_expectations=["compile"],
        adversarial=(
            AdversarialSpec(
                expected_outcome="graceful_refusal", diagnostic="d"
            )
            if kind == "adversarial"
            else None
        ),
    )


class _FakeResponse:
    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def json(self) -> Any:
        if self._payload is None:
            raise AttributeError("no json")
        return self._payload


def _factory(response: _FakeResponse) -> Any:
    async def factory(request: dict[str, Any]) -> _FakeResponse:
        factory.last_request = request  # type: ignore[attr-defined]
        return response

    return factory  # type: ignore[attr-defined]


async def _judge(case: GoldenCase, response: _FakeResponse) -> tuple[JudgeVerdict, dict[str, Any]]:
    factory = _factory(response)
    ji = JudgeInput(
        case=case,
        repo_root=REPO_ROOT,
        scad_source="cube([20,20,20]);",
        stl_path=None,
        model_id="RedHatAI/Qwen3.8-27B-INT4",
        request_factory=factory,
    )
    verdict = await judge_render(ji)
    return verdict, factory.last_request  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# Well-formed replies
# ---------------------------------------------------------------------------


def test_pass_reply() -> None:
    verdict, _ = _run_sync(
        _judge(_case(), _FakeResponse(_body(True, "matches the photo")))
    )
    assert verdict.passed is True
    assert verdict.reason == "matches the photo"
    assert verdict.failure_class is None


def test_fail_reply_non_adversarial_is_geometrically_wrong() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse(_body(False, "hole too small"))))
    assert verdict.passed is False
    assert verdict.reason == "hole too small"
    assert verdict.failure_class == "geometrically_wrong"


def test_fail_reply_adversarial_carries_expected_outcome() -> None:
    verdict, _ = _run_sync(
        _judge(_case("adversarial"), _FakeResponse(_body(False, "below tolerance")))
    )
    assert verdict.passed is False
    assert verdict.failure_class == "graceful_refusal"


# ---------------------------------------------------------------------------
# Malformed replies fail closed
# ---------------------------------------------------------------------------


def test_unparseable_reply_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse(_body_raw("not json at all"))))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_empty_reply_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse(_body_raw(""))))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_wrong_shape_reply_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse({"ok": True})))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_no_json_method_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse(None)))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_non_dict_body_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse(["a", "list"])))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_no_choices_fails_closed() -> None:
    verdict, _ = _run_sync(_judge(_case(), _FakeResponse({"choices": []})))
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


def test_non_dict_message_fails_closed() -> None:
    verdict, _ = _run_sync(
        _judge(_case(), _FakeResponse({"choices": [{"message": "str"}]}))
    )
    assert verdict.passed is False
    assert verdict.failure_class == "unclassified_syntax_error"


# ---------------------------------------------------------------------------
# Request shape: the configured model id, no key in the message
# ---------------------------------------------------------------------------


def test_request_uses_configured_model_id() -> None:
    _, req = _run_sync(
        _judge(_case(), _FakeResponse(_body(True, "ok")))
    )
    assert req["model"] == "RedHatAI/Qwen3.8-27B-INT4"


def test_request_has_system_and_user_messages() -> None:
    _, req = _run_sync(_judge(_case(), _FakeResponse(_body(True, "ok"))))
    messages = req["messages"]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == JUDGE_PROMPT
    assert messages[1]["role"] == "user"
    # The user turn carries the request text + the OpenSCAD source.
    content = messages[1]["content"]
    assert isinstance(content, list)
    first = content[0]
    assert first["type"] == "text"
    assert "A 20mm box" in first["text"]
    assert "cube([20,20,20]);" in first["text"]


def test_no_api_key_in_message() -> None:
    """The API key stays in the Authorization header (the factory),
    never in a message part."""
    _, req = _run_sync(_judge(_case(), _FakeResponse(_body(True, "ok"))))
    serialized = json.dumps(req, ensure_ascii=False)
    assert "Bearer" not in serialized
    assert "key" not in serialized.lower().replace("monkey", "")


def test_verdict_to_dict_is_json_serialisable() -> None:
    v = JudgeVerdict(passed=True, reason="ok", failure_class=None)
    d = v.to_dict()
    assert d == {"passed": True, "reason": "ok", "failure_class": None}
    json.dumps(d)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _body(pass_: bool, reason: str) -> dict[str, Any]:
    return {
        "choices": [
            {"message": {"role": "assistant", "content": json.dumps({"pass": pass_, "reason": reason})}}
        ]
    }


def _body_raw(raw: str) -> dict[str, Any]:
    return {"choices": [{"message": {"role": "assistant", "content": raw}}]}


def _run_sync(coro: Any) -> Any:
    import asyncio

    return asyncio.run(coro)
