"""Issue #449: acknowledgement routing with NO live fill-recut offer.

Table-driven pins of the outcome for every phrase x route (chat, region
edit) x part state. The part states are measured, unknown hole count,
known count without measured holes, and no part. Every case below has
no pending-offer row unless the test says otherwise.

Outcomes: ``point-first`` (FRILL_POINT_FIRST_REPLY), ``no-offer``
(NO_OFFER_AFFIRMATION_REPLY, the #349 notice), ``loop`` (the design loop
runs), or the question route's reply for ``yes?`` on chat (pinned as main
does it).
"""

from __future__ import annotations

from typing import Any

import pytest
from httpx import AsyncClient

import d33d.chat_loop as chat_loop_mod
import d33d.design_loop_events as dle_mod
from d33d.fill_recut import FRILL_POINT_FIRST_REPLY, is_literal_yes
from d33d.hole_select import part_has_measured_holes
from d33d.part_http import part_public
from d33d.question_answer import NO_OFFER_AFFIRMATION_REPLY
from tests.legacy_bounds_fixtures import _set_legacy_part
from tests.test_app import _region_edit_body
from tests.test_issue437_offer_flow import PLATE_REPORT, _run_async

BOUNDS = {
    "bbox_file_units": [120.0, 80.0, 6.0],
    "bbox_bounds_file_units": [[0.0, 0.0, 0.0], [120.0, 80.0, 6.0]],
}
#: part state -> stored report (None: no part at all).
STATES: dict[str, dict[str, Any] | None] = {
    "measured": PLATE_REPORT,
    "unknown": BOUNDS,
    "known_no_measured": {"hole_count": 2, **BOUNDS},
    "no_part": None,
}
UNMEASURED = ["unknown", "known_no_measured", "no_part"]
ROUTES = ["chat", "region"]

LITERAL_YES = ["yes", "yes please", "Yes, do that"]
NOTICE_EVERY_STATE = [
    "yes, do it",
    "ok",
    "sure thing",
    "all good",
    "got it",
    "all right",
    "yes thanks",
    "do it all",
]
LOOP_EVERY_STATE = ["right, all of it", "yes, add a chamfer here", "ok, make it 5 mm taller"]

#: The #250 param row that fails ``is_live_param_offer``: its version is not
#: the project's latest (a project with no versions has no latest at all).
STALE_250 = {"version_id": 999999, "param": "wall_thickness"}
#: A LIVE fill-recut offer (the chat route's accept/decline/supersede rows).
LIVE_FILL = {"kind": "fill_recut", "noun": "hole", "size": 38.0, "axis": [0.0, 0.0, 1.0]}


@pytest.fixture
def loop_calls(monkeypatch) -> list[dict[str, Any]]:
    """Replace the design loop at BOTH seams; returns the kwargs list."""
    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "LOOP", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    return calls


async def _send(app, client: AsyncClient, pid: int, route: str, text: str) -> dict[str, Any] | None:
    """POST one turn on ``route`` and drain its event source to the done frame."""
    if route == "chat":
        r = await client.post(f"/api/projects/{pid}/chat", json={"message": text})
    else:
        r = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body(instruction=text)
        )
    assert r.status_code == 202, r.text
    source = app.state.event_sources.get(pid)
    frames: list[tuple[str, Any]] = []
    if source is not None:
        async for event, data in source:
            frames.append((event, data))
    app.state.design_loop_inflight.discard(pid)
    done = [d for e, d in frames if e == "done"]
    return done[-1] if done else None


def _outcome_run(app, loop_calls, cases):
    """Run ``cases`` = [(state, route, text, pending)]; returns per-case
    (done, loop_count, pending_after) with a fresh project each."""

    async def _call(client):
        results = []
        for i, (state, route, text, pending) in enumerate(cases):
            r = await client.post("/api/projects", json={"name": f"ack449-{i}"})
            pid = r.json()["id"]
            if STATES[state] is not None:
                _set_legacy_part(app, pid, STATES[state])
            if pending is not None:
                app.state.versions.set_pending_offer(pid, dict(pending))
            loop_calls.clear()
            done = await _send(app, client, pid, route, text)
            results.append(
                (done, len(loop_calls), app.state.versions.get_pending_offer(pid))
            )
        return results

    return _run_async(app, _call)


def _assert_point_first(done, loops, offer):
    assert done is not None and done["message"] == FRILL_POINT_FIRST_REPLY, done
    assert loops == 0, "the design loop ran"
    assert offer is None, offer


def _assert_notice(done, loops, offer):
    assert done is not None and done["message"] == NO_OFFER_AFFIRMATION_REPLY, done
    assert loops == 0, "the design loop ran"


def _assert_loop(done, loops):
    assert loops == 1, f"the design loop must run exactly once, ran {loops}"


# ---------------------------------------------------------------------------
# Literal yes: point-first on measured parts, the #349 notice otherwise.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("text", LITERAL_YES)
def test_literal_yes_on_measured_part_is_point_first(app_with_projects, loop_calls, route, text):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [("measured", route, text, None)]
    )
    _assert_point_first(done, loops, offer)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("state", UNMEASURED)
@pytest.mark.parametrize("text", LITERAL_YES)
def test_literal_yes_on_unmeasured_part_gets_notice(app_with_projects, loop_calls, route, state, text):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, route, text, None)]
    )
    _assert_notice(done, loops, offer)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", NOTICE_EVERY_STATE)
def test_other_acknowledgements_get_notice_in_every_state(app_with_projects, loop_calls, route, state, text):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, route, text, None)]
    )
    _assert_notice(done, loops, offer)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("state", list(STATES))
@pytest.mark.parametrize("text", LOOP_EVERY_STATE)
def test_instructions_with_yes_run_the_loop_in_every_state(app_with_projects, loop_calls, route, state, text):
    [(done, loops, _offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, route, text, None)]
    )
    _assert_loop(done, loops)
    assert text in loop_calls[-1]["request_text"], loop_calls[-1]


@pytest.mark.parametrize("state", list(STATES))
def test_yes_question_on_chat_takes_the_normal_route(app_with_projects, loop_calls, state):
    """``yes?`` is not a bare affirmation: chat answers it with the question
    route's reply (no point-first, no notice, no loop) as main does."""
    [(done, loops, _offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, "chat", "yes?", None)]
    )
    assert done is not None and "Nothing is built yet" in done["message"], done
    assert loops == 0, "the design loop ran"


@pytest.mark.parametrize("state", list(STATES))
def test_yes_question_on_region_runs_the_loop(app_with_projects, loop_calls, state):
    """``yes?`` on the edit bar falls through to the design loop, as main does."""
    [(done, loops, _offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, "region", "yes?", None)]
    )
    _assert_loop(done, loops)


def test_literal_yes_on_measured_part_with_unsettled_unit_is_point_first(app_with_projects, loop_calls):
    """Measured wins over unit status: a literal yes on a measured part whose
    units are unsettled gets point-first on both routes, never the loop."""

    def _unsettle(app, pid):
        app.state.conn.raw.execute(
            "UPDATE projects SET part_unit_status='unsettled' WHERE id=?", (pid,)
        )
        app.state.conn.commit()

    async def _call(client):
        out = []
        for route in ROUTES:
            r = await client.post("/api/projects", json={"name": f"unsettled-{route}"})
            pid = r.json()["id"]
            _set_legacy_part(app_with_projects, pid, PLATE_REPORT)
            _unsettle(app_with_projects, pid)
            loop_calls.clear()
            done = await _send(app_with_projects, client, pid, route, "yes")
            out.append((route, done, len(loop_calls), app_with_projects.state.versions.get_pending_offer(pid)))
        return out

    for route, done, loops, offer in _run_async(app_with_projects, _call):
        _assert_point_first(done, loops, offer)


# ---------------------------------------------------------------------------
# Stale #250 rows (fail is_live_param_offer) and the live fill-recut offer.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("text", ["yes", "yes please", "Yes, do that"])
def test_stale_row_literal_yes_on_measured_is_point_first_and_clears_row(
    app_with_projects, loop_calls, route, text
):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [("measured", route, text, STALE_250)]
    )
    _assert_point_first(done, loops, offer)


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("state", UNMEASURED)
def test_stale_row_literal_yes_unmeasured_gets_notice_and_keeps_row(
    app_with_projects, loop_calls, route, state
):
    """The no-offer path treats a stale row as the chat #349 guard does on
    main: the notice fires and the row is left in place (no clear)."""
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, route, "yes", STALE_250)]
    )
    _assert_notice(done, loops, offer)
    assert offer == STALE_250, offer


@pytest.mark.parametrize("route", ROUTES)
@pytest.mark.parametrize("state", UNMEASURED)
def test_stale_row_other_ack_gets_notice_and_keeps_row(app_with_projects, loop_calls, route, state):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [(state, route, "ok", STALE_250)]
    )
    _assert_notice(done, loops, offer)
    assert offer == STALE_250, offer


@pytest.mark.parametrize("route", ROUTES)
def test_stale_row_other_ack_on_measured_gets_notice_and_keeps_row(app_with_projects, loop_calls, route):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [("measured", route, "ok", STALE_250)]
    )
    _assert_notice(done, loops, offer)
    assert offer == STALE_250, offer


@pytest.mark.parametrize("route", ROUTES)
def test_live_fill_recut_yes_accepts_and_runs_the_loop(app_with_projects, loop_calls, route):
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [("measured", route, "yes", LIVE_FILL)]
    )
    _assert_loop(done, loops)
    assert offer is None, offer


@pytest.mark.parametrize(
    ("route", "text", "expect"),
    [
        ("chat", "ok", "loop"),
        ("chat", "do it all", "point-first"),
        ("region", "ok", "loop"),
        ("region", "do it all", "loop"),
    ],
)
def test_live_fill_recut_ok_and_do_it_all_as_main(app_with_projects, loop_calls, route, text, expect):
    """With a LIVE offer routing is unchanged: "ok" accepts (is_clean_yes);
    "do it all" supersedes the offer, and chat then answers point-first
    while the edit bar falls through to the loop — main's recorded result."""
    [(done, loops, offer)] = _outcome_run(
        app_with_projects, loop_calls, [("measured", route, text, LIVE_FILL)]
    )
    if expect == "loop":
        _assert_loop(done, loops)
    else:
        assert done is not None and done["message"] == FRILL_POINT_FIRST_REPLY, done
        assert loops == 0
    assert offer is None, offer


# ---------------------------------------------------------------------------
# Unit and shared-helper pins.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["yes", "Yes", " YES ", "yes please", "Yes, do that", "yes, do that!", "yes please."])
def test_is_literal_yes_accepts_the_three_forms(text):
    assert is_literal_yes(text)


@pytest.mark.parametrize(
    "text",
    ["yes?", "Yes, do that?", "yes, do it", "yes thanks", "yes go ahead", "ok", "yes 5", "", "yes yes"],
)
def test_is_literal_yes_rejects_everything_else(text):
    assert not is_literal_yes(text)


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("measured", True),
        ("unknown", False),
        ("known_no_measured", False),
        ("no_part", False),
    ],
)
def test_shared_measured_helper_per_state(app_with_projects, state, expected):
    """One helper answers "measured" for both routes; pin each state."""

    async def _probe(client):
        r = await client.post("/api/projects", json={"name": f"helper-{state}"})
        pid = r.json()["id"]
        if STATES[state] is not None:
            _set_legacy_part(app_with_projects, pid, STATES[state])
        row = app_with_projects.state.conn.get_project(pid)
        return part_public(row) if row.get("part_filename") else None

    part = _run_async(app_with_projects, _probe)
    assert part_has_measured_holes(part) is expected


def test_measured_but_unscaled_counts_as_measured(app_with_projects, loop_calls):
    """A measured list with no usable scale is still measured: point-first."""

    def _unscale(app, pid):
        app.state.conn.raw.execute("UPDATE projects SET part_scale=NULL WHERE id=?", (pid,))
        app.state.conn.commit()

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "unscaled449"})
        pid = r.json()["id"]
        _set_legacy_part(app_with_projects, pid, PLATE_REPORT)
        _unscale(app_with_projects, pid)
        out = []
        for route in ROUTES:
            loop_calls.clear()
            done = await _send(app_with_projects, client, pid, route, "yes")
            out.append((route, done, len(loop_calls)))
        return out

    for route, done, loops in _run_async(app_with_projects, _call):
        assert done is not None and done["message"] == FRILL_POINT_FIRST_REPLY, (route, done)
        assert loops == 0


# ---------------------------------------------------------------------------
# Issue #449 review: a LIVE #250 row is not point-first on the edit bar.
# ---------------------------------------------------------------------------


def test_region_live_250_row_bare_yes_on_measured_leaves_row_and_runs_loop(
    app_with_projects, loop_calls, monkeypatch
):
    """Region route, measured part, LIVE #250 row, bare "ok": the fill-recut
    decision returns None (as the chat route does for a live #250 row), so no
    point-first reply is sent, the pending row survives, and the request runs
    the loop with its text (main's routing for a #250 row)."""
    live_250 = {"version_id": 7, "param": "wall_thickness"}

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "live250-449"})
        pid = r.json()["id"]
        _set_legacy_part(app_with_projects, pid, PLATE_REPORT)
        versions = app_with_projects.state.versions
        versions.set_pending_offer(pid, dict(live_250))
        monkeypatch.setattr(versions, "latest_version", lambda _pid: {"id": 7, "version_id": 7})
        loop_calls.clear()
        done = await _send(app_with_projects, client, pid, "region", "ok")
        return done, len(loop_calls), versions.get_pending_offer(pid)

    done, loops, offer = _run_async(app_with_projects, _call)
    assert done is None or done["message"] != FRILL_POINT_FIRST_REPLY, done
    assert loops == 1, f"the design loop must run exactly once, ran {loops}"
    assert offer == live_250, offer


def test_region_notice_does_not_leave_project_in_flight(app_with_projects, loop_calls):
    """No pre-discard of the in-flight flag: after a region "ok" no-offer
    notice on an unmeasured part is drained, the project is not left in
    flight, so the next region edit is accepted (not answered busy)."""

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "inflight-449"})
        pid = r.json()["id"]
        _set_legacy_part(app_with_projects, pid, BOUNDS)
        first = await client.post(
            f"/api/projects/{pid}/region-edits", json=_region_edit_body(instruction="ok")
        )
        assert first.status_code == 202, first.text
        # Drain through the SSE endpoint: its ``finally`` is the in-flight
        # release point (draining the raw generator would bypass it).
        drained = await client.get(f"/api/stream/{pid}")
        assert drained.status_code == 200, drained.text
        assert pid not in app_with_projects.state.design_loop_inflight
        second = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="right, all of it"),
        )
        return second.status_code

    status = _run_async(app_with_projects, _call)
    assert status == 202, status

