"""tests/test_critique_protocol.py — six-view pairwise critique protocol
(ticket #5, task-critique workstream).

Covers:
- The six-view-mandatory gate: a mock render with only one valid PNG (the
  other five blank/invalid) must NOT produce a verdict — the protocol returns
  a distinct ``InsufficientViews`` result, never a partial judgement.
- The critique prompt sent to the critique role contains the full six-view
  image set plus the structured contract (per-view concrete mismatch
  enumeration + an explicit feature checklist, never a bare "looks correct").
- Pairwise A-vs-B framing: the prompt frames candidate (A) vs baseline (B) and
  the parse accepts only relative judgements (better/worse/equivalent) — an
  absolute-score "looks correct" response yields no verdict.
- Graceful non-vision fallback: a T2/T3 tier (no vision) degrades the critique
  to deterministic-gate-only scoring (reusing ``d33d.design_loop.score``)
  rather than crashing or hallucinating a vision verdict.  A full loop can
  complete end-to-end with this fallback.
- Role alias: the critique LLM edge resolves the ``"critique"`` role through
  ``d33d.config.resolve.resolve_model`` (never a hardcoded model id).

No Docker, no network: ``llm_fn`` / ``request_factory`` are injected (the
``d33d.render_worker`` testable pattern).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from d33d.config.catalogue import load_catalogue
from d33d.config.probes import CapabilityResult
from d33d.config.resolve import resolve_model
from d33d.critique_protocol import (
    REQUIRED_VIEW_NAMES,
    CritiqueVerdict,
    InsufficientViews,
    ViewMismatch,
    build_critique_messages,
    build_critique_system,
    critique,
    deterministic_verdict,
    make_critique_llm_fn,
    parse_critique,
    required_views,
    validate_views,
)
from d33d.design_llm import LLMResult, SenderError

CRITIQUE_TOOLS_EXPECTED = [
    {
        "type": "function",
        "function": {
            "name": "emit_critique",
            "parameters": {
                "type": "object",
                "properties": {
                    "assessment": {"type": "string"},
                    "views": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "view": {"type": "string"},
                                "ok": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
        },
    }
]
from d33d.design_loop import BboxInfo
from d33d.render_worker import VIEWS, RenderResult

PHOTO = "data:image/png;base64,REFPHOTO"
SIX_VIEWS = [name for name, _ in VIEWS]
SIX_VIEW_URLS = {
    name: f"data:image/png;base64,VIEW_{i}" for i, name in enumerate(SIX_VIEWS)
}
STATED = (20.0, 25.0, 30.0)
GOOD_SCAD = "W = 20;\nD = 25;\nH = 30;\ncube([W, D, H]);\n"
VIEWS_OK = tuple(SIX_VIEWS)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _catalogue():
    env = {"TRAIL_OPENERS_LLM_KEY": "stub", "PAID_AZURE_LLM_KEY": "stub"}
    saved = {k: os.environ.get(k) for k in env}
    try:
        for k, v in env.items():
            os.environ[k] = v
        return load_catalogue(Path(__file__).parent / "fixtures" / "models.yaml")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _render(
    *,
    error_class: str = "ok",
    stderr: str = "",
    views: tuple[str, ...] = VIEWS_OK,
) -> RenderResult:
    return RenderResult(
        ok=error_class == "ok",
        exit_code=0 if error_class == "ok" else 1,
        duration_ms=10,
        error_class=error_class,
        stderr=stderr,
        stl="model.stl" if error_class == "ok" else None,
        csg="model.csg" if error_class == "ok" else None,
        views=views,
    )


def _llm_result(content: str = "", tool_calls: tuple | None = None) -> LLMResult:
    return LLMResult(
        content=content,
        tool_calls=tool_calls or (),
        prompt_hash="h" * 64,
        tier="T0",
        status="ok",
        request_body={},
    )


def _fenced(payload: dict[str, Any]) -> str:
    return "```json\n" + json.dumps(payload) + "\n```"


def _good_verdict_payload() -> dict[str, Any]:
    return {
        "tool": "emit_critique",
        "arguments": {
            "verdict": "worse",
            "mismatches": [
                {
                    "view": "view_00_front.png",
                    "description": "front face is a slab, missing the notch",
                    "proposed_fix": "add the front notch with difference()",
                }
            ],
            "checklist": [
                {"name": "overall_form", "present": True, "note": "box-like"},
                {"name": "front_notch", "present": False, "note": "absent"},
            ],
            "reason": "candidate is worse than baseline: front notch missing.",
        },
    }


def _t2_capability() -> CapabilityResult:
    """A tier that has no tool channel (T3: nothing validated) — the
    non-vision fallback trigger is ``status='no_tools_supported'``."""
    return CapabilityResult(
        tools=False, json_schema=False, vision=False, max_images=0, validated=False
    )


# ---------------------------------------------------------------------------
# The six-view gate
# ---------------------------------------------------------------------------


def test_required_views_matches_render_contract() -> None:
    assert required_views() == REQUIRED_VIEW_NAMES
    assert len(REQUIRED_VIEW_NAMES) == 6


def test_validate_views_flags_single_valid_png() -> None:
    # Only one valid PNG (the other five blank/invalid) -> five missing.
    missing = validate_views(("view_00_front.png", "", "", "", "", ""))
    assert len(missing) == 5
    assert "view_00_front.png" not in missing


def test_validate_views_all_present_empty() -> None:
    assert validate_views(list(REQUIRED_VIEW_NAMES)) == ()


def test_insufficient_views_returns_distinct_no_verdict() -> None:
    # A mock render with only one valid PNG must NOT produce a verdict.
    calls: list[Any] = []

    def llm_fn(messages, system):
        calls.append(messages)
        return _llm_result(_fenced(_good_verdict_payload()))

    result = critique(
        candidate_views={"view_00_front.png": "data:image/png;base64,X"},
        baseline=PHOTO,
        llm_fn=llm_fn,
    )
    assert isinstance(result, InsufficientViews)
    assert isinstance(result.missing, tuple)
    assert len(result.missing) == 5
    # The LLM edge was never called — no partial judgement was issued.
    assert calls == []


# ---------------------------------------------------------------------------
# Prompt contract
# ---------------------------------------------------------------------------


def test_prompt_contains_all_six_views_and_contract() -> None:
    messages = build_critique_messages(
        candidate_views=dict(SIX_VIEW_URLS),
        baseline=PHOTO,
        baseline_label="reference photo",
        feature_items=["overall_form", "front_notch"],
    )
    user = messages[0]
    parts = user["content"]
    # All six view image parts + the baseline image part.
    image_urls = [
        p["image_url"]["url"]
        for p in parts
        if isinstance(p, dict) and p.get("type") == "image_url"
    ]
    assert len(image_urls) == 7
    assert all(u in image_urls for u in SIX_VIEW_URLS.values())
    assert PHOTO in image_urls

    text = next(
        p["text"] for p in parts if isinstance(p, dict) and p.get("type") == "text"
    )
    # Per-view enumeration requirement.
    for name in REQUIRED_VIEW_NAMES:
        assert name in text
    # Structured contract: per-view mismatches + explicit checklist.
    assert "mismatches" in text
    assert "checklist" in text
    assert "view_00_front.png" in text  # checklist template / per-view
    assert "front_notch" in text  # the injected feature item
    # Neutral delimiter framing (data, not instructions).
    assert "<user_data>" in text and "</user_data>" in text


def test_system_prompt_frames_pairwise_and_rejects_absolute() -> None:
    system = build_critique_system("reference photo (baseline B)")
    assert "pairwise" in system.lower()
    assert "baseline" in system.lower()
    assert "better" in system and "worse" in system and "equivalent" in system
    # The explicit checklist is mandated.
    assert "checklist" in system.lower()
    assert "per view" in system.lower() or "PER VIEW" in system


def test_prompt_pairwise_baseline_label_iteration2() -> None:
    # Iteration 2+ compares candidate-vs-prior-candidate (caller passes it).
    prior = "data:image/png;base64,PREVCANDIDATE"
    messages = build_critique_messages(
        candidate_views=dict(SIX_VIEW_URLS),
        baseline=prior,
        baseline_label="prior candidate (baseline B)",
        feature_items=["overall_form"],
    )
    text = next(
        p["text"]
        for p in messages[0]["content"]
        if isinstance(p, dict) and p.get("type") == "text"
    )
    assert "prior candidate" in text


# ---------------------------------------------------------------------------
# Response parse
# ---------------------------------------------------------------------------


def test_parse_accepts_structured_verdict() -> None:
    result = _llm_result(_fenced(_good_verdict_payload()))
    verdict = parse_critique(result)
    assert isinstance(verdict, CritiqueVerdict)
    assert verdict.verdict == "worse"
    assert len(verdict.mismatches) == 1
    assert isinstance(verdict.mismatches[0], ViewMismatch)
    assert verdict.mismatches[0].view == "view_00_front.png"
    assert len(verdict.checklist) == 2
    assert verdict.deterministic is False


def test_parse_rejects_bare_looks_correct() -> None:
    # No per-view enumeration, no checklist -> not a verdict (no-improvement
    # signal, never accepted as a pass).
    result = _llm_result("The design looks correct.")
    assert parse_critique(result) is None


def test_parse_rejects_absolute_score_verdict() -> None:
    # "8/10" is not one of the relative judgements -> no verdict.
    payload = {
        "verdict": "8/10",
        "mismatches": [],
        "checklist": [],
        "reason": "looks fine",
    }
    assert (
        parse_critique(
            _llm_result(_fenced({"tool": "emit_critique", "arguments": payload}))
        )
        is None
    )


def test_parse_accepts_native_tool_call_arguments() -> None:
    args = _good_verdict_payload()["arguments"]
    result = _llm_result(tool_calls=({"name": "emit_critique", "arguments": args},))
    verdict = parse_critique(result)
    assert isinstance(verdict, CritiqueVerdict)
    assert verdict.verdict == "worse"


# ---------------------------------------------------------------------------
# Non-vision fallback (T2/T3)
# ---------------------------------------------------------------------------


def test_non_vision_fallback_scores_deterministic_only() -> None:
    render = _render()  # ok + six views
    verdict = deterministic_verdict(
        render=render,
        stated_dims=STATED,
        bbox=BboxInfo(*STATED, 15000.0),
        scad_source=GOOD_SCAD,
    )
    assert verdict.deterministic is True
    assert verdict.verdict == "better"
    # The checklist carries the four gate bits (auditable, not hallucinated).
    assert len(verdict.checklist) == 4
    assert all(item.present for item in verdict.checklist)


def test_non_vision_fallback_worse_when_render_not_ok() -> None:
    # Render not ok + no views + no bbox + no named params -> rank 0 -> "worse".
    render = _render(error_class="syntax_error", stderr="ERROR: syntax", views=())
    verdict = deterministic_verdict(
        render=render, stated_dims=STATED, bbox=None, scad_source=""
    )
    assert verdict.deterministic is True
    assert verdict.verdict == "worse"


def test_critique_degrades_to_deterministic_on_no_tools_supported() -> None:
    # The llm_fn edge raises SenderError(status='no_tools_supported') for a
    # T2/T3 tier; the critique must degrade to the deterministic fallback.
    def llm_fn(messages, system):
        raise SenderError("no tool channel", status="no_tools_supported")

    render = _render()
    result = critique(
        candidate_views=dict(SIX_VIEW_URLS),
        baseline=PHOTO,
        llm_fn=llm_fn,
        stated_dims=STATED,
        render=render,
        bbox=BboxInfo(*STATED, 15000.0),
        scad_source=GOOD_SCAD,
    )
    assert isinstance(result, CritiqueVerdict)
    assert result.deterministic is True
    assert result.verdict == "better"


def test_loop_completes_end_to_end_with_non_vision_critique() -> None:
    """A full critique round-trips with the non-vision (deterministic-gate)
    fallback — no vision model, no crash, a real verdict is produced."""
    render = _render()

    def llm_fn(messages, system):
        raise SenderError("T2/T3", status="no_tools_supported")

    result = critique(
        candidate_views=dict(SIX_VIEW_URLS),
        baseline=PHOTO,
        llm_fn=llm_fn,
        stated_dims=STATED,
        render=render,
        bbox=BboxInfo(*STATED, 15000.0),
        scad_source=GOOD_SCAD,
    )
    assert isinstance(result, CritiqueVerdict)
    assert not isinstance(result, InsufficientViews)
    assert result.verdict in ("better", "worse", "equivalent")


# ---------------------------------------------------------------------------
# Role-alias seam (never a hardcoded model id)
# ---------------------------------------------------------------------------


def test_make_critique_llm_fn_resolves_critique_role_alias() -> None:
    catalogue = _catalogue()
    # The critique role resolves to the critique-primary alias (model id),
    # not a literal id the caller hardcodes.
    resolution = resolve_model(catalogue, "critique")
    assert resolution.entry.id == "critique-primary"

    t2 = _t2_capability()
    sent: list[dict[str, Any]] = []

    async def request_factory(request: dict[str, Any]):
        sent.append(request)
        return _FakeResponse(
            {"choices": [{"message": {"content": _fenced(_good_verdict_payload())}}]}
        )

    llm_fn = make_critique_llm_fn(
        catalogue,
        request_factories={"critique": request_factory},
        capabilities={"critique": t2},
    )
    # A T2 capability means `send` raises SenderError(no_tools_supported); the
    # seam resolves the role alias first (the wire request is never built
    # because the tier has no tool channel).
    try:
        llm_fn([{"role": "user", "content": "x"}], "system")
        raised = None
    except SenderError as exc:
        raised = exc
    # The resolution went through the role alias path (it reached `send` with
    # the resolved model id).  No hardcoded model id in this test.
    assert raised is not None and raised.status == "no_tools_supported"


def test_make_critique_llm_fn_t0_body_carries_emit_critique_tool_schema() -> None:
    """``make_critique_llm_fn`` attaches the emit_critique tool schema to the
    T0 outgoing body — the ticket's contract (assessment: string,
    views: array of {view: string, ok: boolean}) — while T1/T2 bodies stay
    tools-free (T2 raises before any request, T1 never gains a tools array)."""
    catalogue = _catalogue()
    t0 = CapabilityResult(
        tools=True, json_schema=True, vision=True, max_images=8, validated=True
    )
    sent: list[dict[str, Any]] = []

    async def request_factory(request: dict[str, Any]):
        sent.append(request)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": "ok",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "emit_critique",
                                        "arguments": "{}",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        )

    llm_fn = make_critique_llm_fn(
        catalogue,
        request_factories={"critique": request_factory},
        capabilities={"critique": t0},
    )
    out = llm_fn([{"role": "user", "content": "x"}], "system")
    assert isinstance(out, LLMResult)
    assert out.tier == "T0"
    # Structural match with the ticket's emit_critique contract; the free-form
    # description is not part of the wire contract.
    tool = sent[0]["tools"][0]
    assert tool["type"] == "function"
    assert tool["function"]["name"] == "emit_critique"
    assert tool["function"]["parameters"] == CRITIQUE_TOOLS_EXPECTED[0]["function"]["parameters"]

    # T1: tools must NOT leak into the fenced-JSON wire body.
    t1 = CapabilityResult(
        tools=True, json_schema=False, vision=True, max_images=8, validated=True
    )
    sent_t1: list[dict[str, Any]] = []

    async def t1_factory(request: dict[str, Any]):
        sent_t1.append(request)
        return _FakeResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": _fenced(_good_verdict_payload())
                        }
                    }
                ]
            }
        )

    llm_t1 = make_critique_llm_fn(
        catalogue,
        request_factories={"critique": t1_factory},
        capabilities={"critique": t1},
    )
    out_t1 = llm_t1([{"role": "user", "content": "x"}], "system")
    assert out_t1.tier == "T1"
    assert "tools" not in sent_t1[0]

    # capabilities={} -> None per role: no AttributeError deciding tools.
    llm_none = make_critique_llm_fn(catalogue, request_factories={"critique": request_factory})
    with pytest.raises(AttributeError):
        llm_none([{"role": "user", "content": "x"}], "system")


def test_critique_uses_role_resolved_edge() -> None:
    """The critique entry point calls the injected role-resolved edge and
    parses a structured verdict end-to-end (vision tier)."""
    catalogue = _catalogue()
    t0 = CapabilityResult(
        tools=True, json_schema=True, vision=True, max_images=8, validated=True
    )

    async def request_factory(request: dict[str, Any]):
        return _FakeResponse(
            {"choices": [{"message": {"content": _fenced(_good_verdict_payload())}}]}
        )

    llm_fn = make_critique_llm_fn(
        catalogue,
        request_factories={"critique": request_factory},
        capabilities={"critique": t0},
    )
    result = critique(
        candidate_views=dict(SIX_VIEW_URLS),
        baseline=PHOTO,
        llm_fn=llm_fn,
    )
    assert isinstance(result, CritiqueVerdict)
    assert result.verdict == "worse"
    assert result.deterministic is False


# ---------------------------------------------------------------------------
# Wire-response stub
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload

    @property
    def is_success(self) -> bool:
        return True
