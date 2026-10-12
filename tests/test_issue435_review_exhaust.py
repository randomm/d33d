"""tests/test_issue435_review_exhaust.py — a review fail keeps its reason on exhaustion.

Issue #435 (round 1 adversarial): ``visual_review_failed`` is no longer a
post-check reason, so the exhaustion derivation must still reach it when the
review failure is the best record's repair (CRITICAL) or the last attempt
(ISSUE). A gate reason on the best candidate still wins.
"""

from __future__ import annotations

from typing import Any

from d33d.design_loop import BboxInfo, run_design_loop
from d33d.post_check_reasons import SCREW_CLEARANCE_REASON, VISUAL_REVIEW_FAILED_REASON
from tests.test_design_loop import (
    PHOTO,
    REVIEW_FAIL_REASON,
    STATED,
    _bbox_ok,
    _review_render,
    _run_review_loop,
    _scad_llm,
    _verdict_reply,
)


def test_review_fail_then_gate_fails_exhausts_with_visual_review_failed(tmp_path):
    """Iteration 1 passes every gate and is judged wrong by the review; the
    next two iterations fail the bbox gate. Iteration 1 stays the best (ties
    keep the incumbent), so the run must still report the review reason."""
    calls = {"bbox": 0}

    def bbox_fn(render: Any) -> BboxInfo | None:
        calls["bbox"] += 1
        return _bbox_ok(render) if calls["bbox"] == 1 else None

    result, counts = _run_review_loop(
        tmp_path,
        replies=[_verdict_reply("fail", REVIEW_FAIL_REASON)],
        bbox_fn=bbox_fn,
    )
    assert result.status == "exhausted"
    assert result.iterations_used == 3
    assert counts["critique"] == 1
    assert result.failure_reason == VISUAL_REVIEW_FAILED_REASON


def test_review_fail_on_last_attempt_beats_earlier_post_check_reason(tmp_path):
    """Iteration 1 fails the screw-clearance post-check (all gate bits green,
    so it ties on rank and keeps the incumbent best). Iteration 2, the last
    attempt, passes the post-check and is judged wrong by the review: the
    exhaustion must report the review reason, not the earlier post-check."""
    import dataclasses

    render = _review_render(tmp_path)
    scads = [
        "W = 20;\nD = 25;\nH = 30;\nhole_d = 3;\ncube([W, D, H]);\n",
        "W = 20;\nD = 25;\nH = 30;\nhole_d = 6;\ncube([W, D, H]);\n",
    ]
    counts = {"design": 0, "critique": 0}

    async def llm_fn(role: str, messages: Any, system: Any) -> Any:
        if role != "critique":
            scad = scads[min(counts["design"], len(scads) - 1)]
            counts["design"] += 1
            return _scad_llm(scad)
        counts["critique"] += 1
        return _verdict_reply("fail", REVIEW_FAIL_REASON)

    result = run_design_loop(
        photo=PHOTO,
        stated_dims=STATED,
        render_fn=lambda scad, defines: dataclasses.replace(render),
        llm_fn=llm_fn,
        bbox_fn=_bbox_ok,
        request="a 20 by 25 by 30 mm plate with an M4 screw hole",
        max_iterations=2,
        vision=True,
    )
    assert result.status == "exhausted"
    assert result.iterations[0].repair is not None
    assert result.iterations[0].repair["reason"] == SCREW_CLEARANCE_REASON
    assert counts["critique"] == 1
    assert result.failure_reason == VISUAL_REVIEW_FAILED_REASON
