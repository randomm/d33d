"""Issue #435 time budget: the server safety net and the client stream timeout
are pinned together (server net + 60 s = client)."""

from __future__ import annotations

import re
from pathlib import Path

from d33d import design_loop_events as ev
from d33d.design_loop import DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS

_REPO = Path(__file__).resolve().parents[1]


def _server_net_seconds() -> float:
    # (attempt + review allowance + render allowance) x MAX_ITERATIONS + margin
    return (
        DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS
        + ev.ADAPTER_REVIEW_ALLOWANCE_SECONDS
        + ev.ADAPTER_RENDER_ALLOWANCE_SECONDS
    ) * ev.MAX_ITERATIONS + ev.ADAPTER_DEADLINE_MARGIN_SECONDS


def test_review_allowance_is_one_attempt_budget() -> None:
    assert ev.ADAPTER_REVIEW_ALLOWANCE_SECONDS == DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS


def test_server_safety_net_is_pinned_at_1140_seconds() -> None:
    assert DESIGN_LOOP_ATTEMPT_TIMEOUT_SECONDS == 120
    assert ev.ADAPTER_RENDER_ALLOWANCE_SECONDS == 120
    assert ev.MAX_ITERATIONS == 3
    assert ev.ADAPTER_DEADLINE_MARGIN_SECONDS == 60
    assert _server_net_seconds() == 1140


def test_client_stream_timeout_is_server_net_plus_60_seconds() -> None:
    api_ts = (_REPO / "web" / "src" / "lib" / "api.ts").read_text(encoding="utf-8")
    match = re.search(r"export const STREAM_TOTAL_TIMEOUT_MS = ([\d_]+);", api_ts)
    assert match is not None
    client_ms = int(match.group(1).replace("_", ""))
    assert client_ms == (_server_net_seconds() + 60) * 1000
