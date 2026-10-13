"""Issue #437: the fill-and-recut offer only ever offers Yes for a concrete
measured hole (named, positional, or pointed at in the viewer).

The QA v36 plate: bounds 0..120 x 0..80 (mm), holes at (8, 72), (112, 8)
and (112, 72), all Ø4. The report carries the REAL bounds (not a legacy
report), so no mesh load runs.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from httpx import ASGITransport, AsyncClient

from d33d import fill_recut
from d33d.fill_recut import FILL_RECUT_DECLINE_REPLY, FRILL_POINT_FIRST_REPLY
from d33d.fill_recut_region import fill_recut_region_edit
from d33d.part_http import part_public
import d33d.chat_loop as chat_loop_mod
import d33d.design_loop_events as dle_mod
from tests.legacy_bounds_fixtures import _set_legacy_part

PLATE_REPORT = {
    "hole_count": 3,
    "holes": [
        {"center": [8.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [112.0, 8.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
        {"center": [112.0, 72.0, 3.0], "axis": [0.0, 0.0, 1.0], "diameter_mm": 4.0},
    ],
    "bbox_file_units": [120.0, 80.0, 6.0],
    "bbox_bounds_file_units": [[0.0, 0.0, 0.0], [120.0, 80.0, 6.0]],
}

POINT_AT_38 = "Point at the hole on the part and I'll fill it and cut a Ø38.0 mm one there."


def _run_async(app: Any, coro_factory) -> Any:
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


async def _chat(
    app: Any, client: AsyncClient, pid: int, message: str, history: list[str] | None = None
):
    """POST one chat turn and drive its event source to the done frame."""
    body: dict[str, Any] = {"message": message}
    if history is not None:
        body["chat_history"] = history
    r = await client.post(f"/api/projects/{pid}/chat", json=body)
    assert r.status_code == 202, r.text
    source = app.state.event_sources.get(pid)
    assert source is not None, "no event source registered"
    frames = []
    # Drain to the end: the in-flight flag is released when the stream
    # finishes, so a break here would leave the next turn 409'd.
    async for event, data in source:
        frames.append((event, data))
    # The SSE endpoint releases the in-flight flag when it closes the
    # stream; the test drives the source directly, so it releases it here.
    app.state.design_loop_inflight.discard(pid)
    done = [d for e, d in frames if e == "done"]
    assert done, frames
    return done[0]


def _new_plate_project(app: Any, client: AsyncClient):
    async def _create():
        r = await client.post("/api/projects", json={"name": "Plate437"})
        pid = r.json()["id"]
        _set_legacy_part(app, pid, PLATE_REPORT)
        return pid

    return _create()


def _set_plate_without_scale(app: Any, pid: int) -> None:
    """The QA plate's measured hole list on a row with NO usable part_scale
    (``part_scale`` NULL): the holes are in unknown units, so none resolve
    to mm (issue #437, review finding 1)."""
    conn = app.state.conn
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status='settled', part_scale=NULL, "
        "part_report=? WHERE id=?",
        (json.dumps(PLATE_REPORT), pid),
    )
    conn.commit()


def _new_unscaled_plate_project(app: Any, client: AsyncClient):
    async def _create():
        r = await client.post("/api/projects", json={"name": "Plate437b"})
        pid = r.json()["id"]
        _set_plate_without_scale(app, pid)
        return pid

    return _create()


def test_measured_list_without_scale_never_offers_yes_chat(app_with_projects, monkeypatch) -> None:
    """Review finding 1: a part with a KNOWN measured hole list but no usable
    scale has no resolved hole — the chat turn is a point-at (no Yes, no
    offer), and a bare "yes" is the point-first reply, never a loop."""
    loop_calls = _capture_loop(monkeypatch)

    async def _call(client):
        pid = await _new_unscaled_plate_project(app_with_projects, client)
        fresh = await _chat(app_with_projects, client, pid, "make the top right hole 6 mm")
        offer = app_with_projects.state.versions.get_pending_offer(pid)
        after_yes = await _chat(app_with_projects, client, pid, "yes")
        return fresh, offer, after_yes

    fresh, offer, after_yes = _run_async(app_with_projects, _call)
    assert "Point at the hole" in fresh["message"], fresh
    assert not fresh.get("fill_recut_offer"), fresh
    assert offer is None, offer
    assert after_yes["message"] == FRILL_POINT_FIRST_REPLY, after_yes
    assert not loop_calls, f"the design loop ran: {loop_calls}"


def test_measured_list_without_scale_never_offers_yes_region(app_with_projects) -> None:
    """Review finding 1, viewer pick: the pick on a measured hole of an
    unscaled part names no resolved hole → point-at, no offer."""

    async def _call(client):
        pid = await _new_unscaled_plate_project(app_with_projects, client)
        row = app_with_projects.state.conn.get_project(pid)
        part = {**part_public(row), "scale": None}
        result = fill_recut_region_edit(
            app_with_projects,
            pid,
            "make the big hole 6 mm",
            face_normal=(0.0, 1.0, 0.0),
            row=row,
            part=part,
            hit_point_mm=(112.0, 72.0, 3.0),
        )
        return result, app_with_projects.state.versions.get_pending_offer(pid)

    result, offer = _run_async(app_with_projects, _call)
    assert result is not None and result["outcome"] == "point_at", result
    assert offer is None, offer


def _capture_loop(monkeypatch) -> list[dict[str, Any]]:
    """Replace the design loop with a recorder; returns the kwargs list."""
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(chat_loop_mod, "run_design_loop_with_events", _fake_loop)
    return loop_calls


def test_declined_request_never_reaches_a_later_design_loop(app_with_projects, monkeypatch) -> None:
    """Review finding 3: the declined "38 mm" request (and its decline) is
    in the SPA's history. A later design request must not carry it into the
    loop's history or request text."""
    loop_calls = _capture_loop(monkeypatch)

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        await _chat(app_with_projects, client, pid, "make the top left hole 38 mm")
        await _chat(app_with_projects, client, pid, "no")
        await _chat(
            app_with_projects,
            client,
            pid,
            "make it 5 mm taller",
            ["make the top left hole 38 mm", "no"],
        )

    _run_async(app_with_projects, _call)
    assert len(loop_calls) == 1, loop_calls
    history = loop_calls[0]["chat_history"]
    assert not any("38" in t for t in history), history
    assert "38" not in loop_calls[0]["request_text"], loop_calls[0]


def test_accepted_offer_loop_history_omits_the_declined_request(app_with_projects, monkeypatch) -> None:
    """Review finding 3, accept path: the accepted positional offer runs the
    loop with the measured numbers, and neither the loop's history nor its
    request text carries the declined 38 mm request."""
    loop_calls = _capture_loop(monkeypatch)
    history = ["make the top left hole 38 mm", "no", "make the top right hole 6 mm"]

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        await _chat(app_with_projects, client, pid, "make the top left hole 38 mm")
        await _chat(app_with_projects, client, pid, "no")
        await _chat(app_with_projects, client, pid, "make the top right hole 6 mm", history[:2])
        await _chat(app_with_projects, client, pid, "yes", history)

    _run_async(app_with_projects, _call)
    assert len(loop_calls) == 1, loop_calls
    assert not any("38" in t for t in loop_calls[0]["chat_history"]), loop_calls[0]
    assert "38" not in loop_calls[0]["request_text"], loop_calls[0]
    assert "(112, 72)" in loop_calls[0]["request_text"], loop_calls[0]


def test_point_at_text_reply_has_no_yes_and_no_pending_offer(app_with_projects) -> None:
    """An ambiguous named hole ("make the hole 38 mm" — all three holes are
    Ø4) gets the point-at reply: no Yes flag, no pending offer."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        done = await _chat(app_with_projects, client, pid, "make the hole 38 mm")
        return done, app_with_projects.state.versions.get_pending_offer(pid)

    done, offer = _run_async(app_with_projects, _call)
    assert done["message"] == POINT_AT_38, done
    assert not done.get("fill_recut_offer"), done
    assert offer is None, offer


def test_qa_replay_yes_after_point_at_gives_point_first_no_loop(app_with_projects) -> None:
    """The QA replay's first half: the point-at turn, then a bare "yes" —
    the point-first reply, no offer, no loop (no centre-less instruction)."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        await _chat(app_with_projects, client, pid, "make the hole 38 mm")
        done = await _chat(app_with_projects, client, pid, "yes")
        return done, app_with_projects.state.versions.get_pending_offer(pid)

    done, offer = _run_async(app_with_projects, _call)
    assert done["message"] == FRILL_POINT_FIRST_REPLY, done
    assert not done.get("fill_recut_offer"), done
    assert offer is None, offer


def test_qa_replay_decline_then_positional_then_yes_carries_the_hole(app_with_projects) -> None:
    """The QA replay, end to end: a declined request is dropped for good,
    "make the top right hole 6 mm" offers Yes with the (112, 72) hole, and
    the accepted instruction carries that centre and Ø6.0 — never the
    declined 38, never a centre-less instruction."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        fresh = await _chat(app_with_projects, client, pid, "make the top left hole 38 mm")
        declined = await _chat(app_with_projects, client, pid, "no")
        after_decline = app_with_projects.state.versions.get_pending_offer(pid)
        redo = await _chat(app_with_projects, client, pid, "make the top right hole 6 mm")
        return fresh, declined, after_decline, redo, app_with_projects.state.versions.get_pending_offer(pid)

    fresh, declined, after_decline, redo, offer = _run_async(app_with_projects, _call)
    assert fresh.get("fill_recut_offer") is True, fresh
    assert declined["message"] == FILL_RECUT_DECLINE_REPLY, declined
    assert not declined.get("fill_recut_offer"), declined
    assert after_decline is None, after_decline
    assert redo.get("fill_recut_offer") is True, redo
    assert "Ø6.0 mm" in redo["message"], redo
    assert offer is not None
    assert offer["center"] == [112.0, 72.0], offer
    assert offer["size"] == 6.0, offer
    assert offer["diameter_mm"] == 4.0, offer

    instruction = fill_recut.fill_and_recut_instruction(offer)
    assert "(112, 72)" in instruction, instruction
    assert "Ø6.0 mm" in instruction, instruction
    assert "38" not in instruction, instruction
    assert "center" not in instruction.lower(), instruction


def test_yes_on_live_positional_offer_accepts_with_the_measured_hole(app_with_projects) -> None:
    """Accepting the live positional offer runs the loop with the measured
    hole's numbers (the pure accept decision, no loop started)."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        await _chat(app_with_projects, client, pid, "make the top right hole 6 mm")
        pending = app_with_projects.state.versions.get_pending_offer(pid)
        return fill_recut.fill_recut_turn(app_with_projects, pid, "yes"), pending

    result, pending = _run_async(app_with_projects, _call)
    assert pending is not None, "the top-right offer was not stored"
    assert result is not None and result["outcome"] == "accept", result
    assert "(112, 72)" in result["instruction"], result["instruction"]


def test_corner_one_on_symmetric_plate_is_point_at(app_with_projects) -> None:
    """"the corner one" on the QA plate: every hole is the same distance from
    its nearest corner → point-at, no offer."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        done = await _chat(app_with_projects, client, pid, "make the corner hole 6 mm")
        return done, app_with_projects.state.versions.get_pending_offer(pid)

    done, offer = _run_async(app_with_projects, _call)
    assert "Point at the hole" in done["message"], done
    assert not done.get("fill_recut_offer"), done
    assert offer is None, offer


def test_viewer_pick_offers_the_nearest_measured_hole(app_with_projects) -> None:
    """A viewer pick of a hole offers Yes with the NEAREST measured hole's
    centre and diameter (the hit point is next to (112, 72))."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        row = app_with_projects.state.conn.get_project(pid)
        result = fill_recut_region_edit(
            app_with_projects,
            pid,
            "make the big hole 6 mm",
            face_normal=(0.0, 1.0, 0.0),
            row=row,
            part=part_public(row),
            hit_point_mm=(110.0, 70.0, 3.0),
        )
        return result, app_with_projects.state.versions.get_pending_offer(pid)

    result, offer = _run_async(app_with_projects, _call)
    assert result is not None and result["outcome"] == "fresh_offer", result
    assert offer is not None and offer["kind"] == "fill_recut", offer
    assert offer["center"] == [112.0, 72.0], offer
    assert offer["diameter_mm"] == 4.0, offer
    assert offer["size"] == 6.0, offer


def test_viewer_pick_between_holes_is_point_at(app_with_projects) -> None:
    """A pick equidistant from two holes is a point-at — no Yes, no offer."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        row = app_with_projects.state.conn.get_project(pid)
        result = fill_recut_region_edit(
            app_with_projects,
            pid,
            "make the big hole 6 mm",
            face_normal=(0.0, 1.0, 0.0),
            row=row,
            part=part_public(row),
            hit_point_mm=(60.0, 40.0, 3.0),
        )
        return result, app_with_projects.state.versions.get_pending_offer(pid)

    result, offer = _run_async(app_with_projects, _call)
    assert result is not None and result["outcome"] == "point_at", result
    assert offer is None, offer


def test_non_acceptance_reply_discards_the_declined_request(app_with_projects) -> None:
    """Any non-acceptance reply (not only a clean "no") drops the declined
    request: a later bare "yes" never fills the declined 38 mm hole."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        await _chat(app_with_projects, client, pid, "make the top left hole 38 mm")
        other = await _chat(app_with_projects, client, pid, "hmm, leave that one")
        offer = app_with_projects.state.versions.get_pending_offer(pid)
        yes = await _chat(app_with_projects, client, pid, "yes")
        return other, offer, yes, app_with_projects.state.versions.get_pending_offer(pid)

    other, offer, yes, final = _run_async(app_with_projects, _call)
    assert not other.get("fill_recut_offer"), other
    assert offer is None, offer
    assert yes["message"] == FRILL_POINT_FIRST_REPLY, yes
    assert not yes.get("fill_recut_offer"), yes
    assert final is None, final


def test_positional_hole_request_without_holes_is_the_no_hole_reply(app_with_projects) -> None:
    """A positional phrase on a part with NO measured holes gets the #414
    no-hole reply: no offer, no Yes flag."""

    async def _call(client):
        pid = await _new_plate_project(app_with_projects, client)
        _set_legacy_part(
            app_with_projects,
            pid,
            {
                "hole_count": 0,
                "holes": [],
                "bbox_file_units": [120.0, 80.0, 6.0],
                "bbox_bounds_file_units": [[0.0, 0.0, 0.0], [120.0, 80.0, 6.0]],
            },
        )
        done = await _chat(app_with_projects, client, pid, "make the top right hole 6 mm")
        return done, app_with_projects.state.versions.get_pending_offer(pid)

    done, offer = _run_async(app_with_projects, _call)
    assert "I don't see a hole on the part" in done["message"], done
    assert not done.get("fill_recut_offer"), done
    assert offer is None, offer
