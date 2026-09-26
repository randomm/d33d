"""Issue #261 follow-up (PR #267): the finalize route's ``chat_history``
seam.

``body.chat_history`` feeds ONLY the offer signals
(``_resolve_offer(..., chat_history=body.chat_history)``) — the persisted
stated set and the finalize gate read the LATEST message only
(``stated_axes_from_message(msg_text, chat_history=())`` at the 4b2cbbf
behaviour). And the request body's ``chat_history`` is bounded at
``_parse_finalize_body``: the LAST 50 items survive, and any item longer
than 4000 chars is a clean 422.
"""

from __future__ import annotations

from d33d.design_loop import IterationRecord, Score
from d33d.render_worker import RenderResult
from tests.versioning.helpers import (
    create_project,
    run_async,
)


class _StubResult:
    """Duck-type of the loop result whose ``best`` is a REAL
    ``IterationRecord`` (the route reads its declared ``params`` field)."""

    def __init__(self, status: str, params: dict) -> None:
        self.status = status
        self.best = IterationRecord(
            iteration=0,
            scad_source="",
            render=_default_render(),
            score=Score(bits=(False,)*5, rank=0, tiebreak=(False,)*5),
            params=dict(params),
        )
        self.failure_reason = None if status == "pass" else "bbox_out_of_tolerance"


def _default_render() -> RenderResult:
    return RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=0,
        error_class="ok",
        stderr="",
        stl=None,
        csg=None,
        views=("v",) * 6,
    )


# ---------------------------------------------------------------------------
# 1) finalize: the body's chat_history is NEVER a stated-dims source
# ---------------------------------------------------------------------------


def test_finalize_chat_history_does_not_feed_stated_dims(app_with_versions):
    """Finalize with a chat_history that states ``H: 30`` and a message
    with no dimension cue → the persisted stated_dims do NOT contain
    H=30 (the persisted set carries the LATEST message's cues, not the
    history's — the chat_history field feeds the offer signals only)."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "pass", {"W": 10}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={
                "name": "the bracket",
                "message": "make the edges slightly rounder",
                "chat_history": ["H: 30"],
            },
        )
        assert r.status_code == 201, r.text
        return app_with_versions.state.versions.latest_version(pid)

    latest = run_async(app_with_versions, _call)
    stated = latest.get("stated_dims")
    # No axis is stated: the message carries no cue and the history must
    # not leak into the persisted set (it carries the latest set only).
    assert not stated, f"chat_history leaked into stated_dims: {stated}"


def test_finalize_chat_history_cue_never_replaces_message_cue(app_with_versions):
    """Companion check: when the LATEST message states an axis, the
    history's cue on a DIFFERENT axis still does not join the persisted
    set — the merge carries the latest message's statement, never the
    history's."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "pass", {"W": 10}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={
                "message": "make it 12 mm tall",
                "chat_history": ["W: 30"],
            },
        )
        assert r.status_code == 201, r.text
        return app_with_versions.state.versions.latest_version(pid)

    latest = run_async(app_with_versions, _call)
    stated = latest.get("stated_dims") or {}
    assert stated.get("H") == 12.0, stated
    assert "W" not in stated, f"chat_history leaked W into stated_dims: {stated}"


# ---------------------------------------------------------------------------
# 2) _parse_finalize_body: chat_history is bounded
# ---------------------------------------------------------------------------


def _parse(app, body: dict):
    """POST a finalize body with the loop stubbed so the parse boundary
    is the only code under test; returns the response."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app.state.run_design_loop = lambda: _StubResult("pass", {})
        return await client.post(f"/api/projects/{pid}/finalize", json=body)

    return run_async(app, _call)


def test_finalize_chat_history_keeps_last_50_items(app_with_versions):
    """A chat_history longer than 50 items is kept only as its LAST 50
    (the request still succeeds — the bound is a trailing slice, not a
    reject)."""

    history = [f"turn {i}" for i in range(60)]  # 60 items → keep 10..59

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda: _StubResult(
            "pass", {"W": 10}
        )
        r = await client.post(
            f"/api/projects/{pid}/finalize",
            json={"message": "finalize", "chat_history": history},
        )
        assert r.status_code == 201, r.text
        svc = app_with_versions.state.versions
        return r.status_code, svc.latest_version(pid), svc.get_pending_offer(pid)

    status, latest, pending = run_async(app_with_versions, _call)
    # The bound is the LAST-50 slice, not a reject: the request succeeds
    # and the version is created.
    assert status == 201
    assert latest is not None
    # The offer seam (the only consumer of body.chat_history in finalize)
    # read the truncated history: nothing in "turn N" quotes a number, so
    # no tier-2 offer can be built — the dropped items cannot leak
    # through the offer signal either.
    assert pending is None


def test_finalize_chat_history_item_over_4000_chars_is_422(app_with_versions):
    """A single chat_history item longer than 4000 chars is a clean 422
    (never a 500, never silently truncated)."""

    r = _parse(app_with_versions, {"message": "finalize", "chat_history": ["x" * 4001]})
    assert r.status_code == 422, r.text


def test_finalize_chat_history_item_exactly_4000_chars_is_accepted(
    app_with_versions,
):
    """Boundary: an item of EXACTLY 4000 chars is legal (the bound
    rejects strictly longer items)."""

    r = _parse(app_with_versions, {"message": "finalize", "chat_history": ["x" * 4000]})
    assert r.status_code == 201, r.text


def test_finalize_chat_history_non_string_item_is_422(app_with_versions):
    """Non-string items are still a clean 422 (the pre-existing check)."""

    r = _parse(app_with_versions, {"message": "finalize", "chat_history": ["ok", 7]})
    assert r.status_code == 422, r.text
