"""Issue #250 (task-a): the assumed-value offer — selection precedence,
the confirm_first/confirm_sentence reply contract, the number-guarded
sentence, the acceptance flow, and the persistence of the offer state.

All fast (stub loops, no LLM, no Docker).
"""

from __future__ import annotations

from typing import Any

from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)


def _reopen_conn(app):
    """Reopen the app's DB connection if it was closed by a previous
    test's lifespan teardown (the same pattern as the carried-forward
    test above)."""
    closed = False
    try:
        app.state.conn.raw.execute("SELECT 1")
    except Exception:
        closed = True
    if app.state.conn is None or closed:
        import d33d.db as _db
        from d33d import versions as _versions_mod

        fresh = _db.connect(app.state.db_path)
        _versions_mod.migrate(fresh)
        app.state.conn = fresh
        app.state.versions = _versions_mod.VersionService(fresh)



# ---------------------------------------------------------------------------
# Offer selection precedence (pure — d33d.confirm_offer)
# ---------------------------------------------------------------------------


def _meta_with_axis(name: str) -> dict[str, Any]:
    return {name: {"label": "Width", "unit": "mm", "axis": "W"}}


def test_selection_declared_axis_param_wins_over_confirm_first():
    """Rule 1: a pass with an assumed param carrying a model-declared
    axis (issue #248) → the offer names that axis param FIRST, even when
    the model's ``confirm_first`` flags a different param."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"width": 60.0, "wall_thickness": 3.0}
    meta = _meta_with_axis("width")
    name = select_offer_candidate(params, meta, None, set(), "wall_thickness")
    assert name == "width"  # the declared-axis param, not confirm_first


def test_selection_confirm_first_when_no_axis_params():
    """Rule 2: no axis params, but ``confirm_first`` names a valid assumed
    numeric param of this version → that param is the offer."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "fillet_radius": 1.5}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    name = select_offer_candidate(params, meta, None, set(), "wall_thickness")
    assert name == "wall_thickness"


def test_selection_invalid_confirm_first_yields_no_offer():
    """Rule 2 validation: a ``confirm_first`` that names a nonexistent,
    non-numeric, zero, or non-assumed param is rejected — the offer is
    ABSENT (never a silent second guess)."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "note": "left", "zero": 0.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    for bad in ("ghost", "note", "zero", None, ""):
        assert select_offer_candidate(params, meta, None, set(), bad) is None


def test_selection_never_re_offers_confirmed_param():
    """A param already in ``confirmed_params`` (the user confirmed it on
    this version) is never offered again: a flag naming it is rejected
    (no silent fallback), while a flag naming another eligible param
    still works."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"wall_thickness": 3.0, "fillet_radius": 1.5}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    confirmed = {"wall_thickness": 3.0}
    assert (
        select_offer_candidate(params, meta, confirmed, set(), "wall_thickness")
        is None
    )
    assert (
        select_offer_candidate(params, meta, confirmed, set(), "fillet_radius")
        == "fillet_radius"
    )


def test_selection_never_re_offers_user_changed_param():
    """A param the user changed via the design loop (it is in the
    changed set — a value move or a new param) is never offered: those
    values are the user's own moves, not assumptions to confirm."""
    from d33d.confirm_offer import select_offer_candidate

    assert (
        select_offer_candidate(
            {"wall_thickness": 2.0, "fillet": 1.0},
            None,
            None,
            {"wall_thickness"},
            "wall_thickness",
        )
        is None
    )
    # A param the user did NOT change is still offered when flagged:
    assert (
        select_offer_candidate(
            {"wall_thickness": 2.0, "fillet": 1.0},
            None,
            None,
            {"wall_thickness"},
            "fillet",
        )
        == "fillet"
    )


def test_selection_no_eligible_param_yields_no_offer():
    """String-only / zero / bool params: no numeric assumed candidate →
    no offer (never a fake one), regardless of the flag."""
    from d33d.confirm_offer import select_offer_candidate

    params = {"note": "left", "flag": True, "zero": 0.0}
    assert select_offer_candidate(params, None, None, set(), "note") is None
    assert select_offer_candidate(params, None, None, set(), None) is None


# ---------------------------------------------------------------------------
# The sentence: model text is number-guarded, else the template
# ---------------------------------------------------------------------------


def test_sentence_model_text_accepted_when_guard_passes():
    """The model's ``confirm_sentence`` is used verbatim when it names
    the chosen value AND every number in it appears in the block."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    sentence = offer_sentence(
        entry,
        "I assumed 3 mm Wall thickness. That is sturdy for a shelf spacer. Want it thinner?",
        [entry],
    )
    assert sentence == (
        "I assumed 3 mm Wall thickness. That is sturdy for a shelf spacer. Want it thinner?"
    )


def test_sentence_invented_number_falls_to_template():
    """A model sentence with a number that is NOT in the block (an
    invented value — "I assumed 4 mm" when the param is 3) fails the
    guard → the deterministic template is used."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    sentence = offer_sentence(entry, "I assumed 4 mm walls. Want it thinner?", [entry])
    assert sentence == "I assumed 3 for Wall thickness. Want it different?"


def test_sentence_wrong_value_falls_to_template():
    """A sentence that names the RIGHT param but the WRONG value fails
    the value-name check (even if the number exists in the block under
    another param) → the template."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0, "spacer_width": 20.0}
    meta = {
        "wall_thickness": {"label": "Wall thickness", "unit": "mm"},
        "spacer_width": {"label": "Spacer width", "unit": "mm"},
    }
    entry = offer_entry(params, meta, "wall_thickness")
    block = [
        offer_entry(params, meta, "wall_thickness"),
        offer_entry(params, meta, "spacer_width"),
    ]
    sentence = offer_sentence(entry, "I assumed 20 mm walls. Want it thinner?", block)
    assert sentence == "I assumed 3 for Wall thickness. Want it different?"


def test_sentence_none_falls_to_template_with_identifier_fallback():
    """No model sentence → the deterministic template (label per #248,
    raw-identifier fallback when no label — the mono case)."""
    from d33d.confirm_offer import offer_entry, offer_sentence

    params = {"wall_thickness": 3.0}
    entry = offer_entry(params, None, "wall_thickness")  # no metadata
    sentence = offer_sentence(entry, None, [entry])
    assert sentence == "I assumed 3 for wall_thickness. Want it different?"


def test_ack_sentence_shape():
    """The acknowledgement is the deterministic template:
    "Got it — {label} stays {value}." (label per #248, identifier
    fallback)."""
    from d33d.confirm_offer import ack_sentence, offer_entry

    params = {"wall_thickness": 3.0}
    meta = {"wall_thickness": {"label": "Wall thickness", "unit": "mm"}}
    entry = offer_entry(params, meta, "wall_thickness")
    assert ack_sentence(entry) == "Got it — Wall thickness stays 3."
    entry2 = offer_entry({"bore": 8.0}, None, "bore")
    assert ack_sentence(entry2) == "Got it — bore stays 8."


# ---------------------------------------------------------------------------
# Acceptance: clean affirmation + live offer (heuristic reuse)
# ---------------------------------------------------------------------------


def test_acceptance_matrix_through_heuristic():
    """The composite acceptance predicate: a clean affirmation (the
    issue #249 ``_is_clean_affirmation`` heuristic, reused) AND a live
    pending offer on the current latest version. "yes but make it 2 mm"
    is NOT an acceptance (a change request routes to the design loop); a
    stale offer (a newer version exists) lapses."""
    from d33d.confirm_offer import is_pending_offer_acceptance

    offer = {"version_id": 7, "param": "wall_thickness"}
    latest = {"id": 7}
    for message in ("yes", "Yes.", "ok", "confirmed", "yep"):
        assert is_pending_offer_acceptance(message, offer, latest), message
    for message in (
        "yes but make it 2 mm",
        "is it 3 mm?",
        "no",
        "nope",
        "why 3 mm?",
    ):
        assert not is_pending_offer_acceptance(message, offer, latest), message
    # Stale offer (a newer version was created meanwhile) → lapses:
    assert not is_pending_offer_acceptance("yes", offer, {"id": 8})
    # No pending offer → normal routing:
    assert not is_pending_offer_acceptance("yes", None, latest)


# ---------------------------------------------------------------------------
# Through the /chat route: the done frame's offer field, the server-side
# pending-offer state, the acceptance flow, and the carry-forward
# ---------------------------------------------------------------------------


class _OfferStubResult:
    """A pass result whose ``best`` is a real ``IterationRecord``
    carrying the declared ``confirm_first`` / ``confirm_sentence``
    fields (the adapter reads them as declared fields, never
    duck-typed)."""

    def __init__(
        self,
        params: dict[str, Any],
        meta: dict[str, Any] | None = None,
        confirm_first: str | None = None,
        confirm_sentence: str | None = None,
    ) -> None:
        from d33d.design_loop import IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "pass"
        self.failure_reason = None
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 10; cube([W, W, W]);",
            render=_default_render(),
            score=Score(bits=(True, True, True, True), rank=4, tiebreak=(True,)*4),
            params=dict(params),
            param_meta=dict(meta) if meta else {},
            confirm_first=confirm_first,
            confirm_sentence=confirm_sentence,
        )


class _ExhaustedStub:
    """An exhausted result (no version, no offer — the pending offer is
    cleared on the exhausted path)."""

    def __init__(self, params: dict[str, Any]) -> None:
        from d33d.design_loop import IterationRecord, Score
        from tests.versioning.test_design_loop_finalize import _default_render

        self.status = "exhausted"
        self.failure_reason = "bbox_out_of_tolerance"
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 10; cube([W, W, W]);",
            render=_default_render(),
            score=Score(bits=(False,)*4, rank=0, tiebreak=(False,)*4),
            params=dict(params),
        )


def _drive_chat(app, client, pid, body):
    """POST ``body`` to /chat and drive the event source to its terminal
    frame (the same pattern the issue #54 chat tests use). Returns a
    coroutine — ``await`` it from inside an async ``_call`` closure.
    Returns ``(response, frames)``."""

    async def _drive():
        r = await client.post(f"/api/projects/{pid}/chat", json=body)
        source = app.state.event_sources.get(pid)
        frames = []
        assert source is not None, "event source not registered before 202"
        async for event, data in source:
            frames.append((event, data))
            if event in ("done", "error"):
                break
        return r, frames

    return _drive()


def test_chat_pass_with_assumed_axis_param_offers_it(app_with_versions):
    """A passing pass whose new version carries an assumed param with a
    model-declared axis → the done frame's ADDITIVE ``confirm_offer`` /
    ``confirm_sentence`` fields name that axis param first (the template
    sentence — no model sentence supplied), and the pending offer is
    stored server-side on the project row (never client-derived)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"width": 60.0, "wall_thickness": 3.0},
            meta={"width": {"label": "Width", "unit": "mm", "axis": "W"}},
            confirm_first="wall_thickness",  # the axis param must win
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        latest = app_with_versions.state.versions.latest_version(pid)
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, latest, row_offer

    status, frames, latest, row_offer = run_async(app_with_versions, _call)
    assert status == 202, status
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    # The offer rides the done frame as ADDITIVE ``confirm_*`` fields
    # (the SPA's App.tsx routes them to a separate plain assistant
    # message after the pass card — issue #250's task-b contract).
    assert done[-1].get("confirm_offer") == "width", done
    assert done[-1].get("confirm_sentence") == (
        "I assumed 60 for Width. Want it different?"
    ), done
    # Server-side pending-offer state: the offer's version is the NEW
    # version, the param is the axis param.
    assert row_offer == {"version_id": latest["id"], "param": "width"}


def test_chat_pass_confirm_first_valid_offers_that_param(app_with_versions):
    """No axis params, but the model's ``confirm_first`` names a valid
    assumed numeric param of the new version → that param is the offer
    (the model's sentence is used when it passes the number guard)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
            confirm_first="wall_thickness",
            confirm_sentence="I assumed 3 mm Wall thickness. Want it thinner?",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done
    assert done[-1].get("confirm_sentence") == (
        "I assumed 3 mm Wall thickness. Want it thinner?"
    ), done


def test_chat_pass_invalid_confirm_first_offers_nothing(app_with_versions):
    """A ``confirm_first`` naming a nonexistent param is rejected and —
    with no declared-axis param either — the pass carries NO offer
    (never a silent second guess) and the pending offer is cleared."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="ghost_param",  # not a param of this version
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, row_offer

    status, frames, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert "confirm_offer" not in done[-1], done
    assert "confirm_sentence" not in done[-1], done
    assert row_offer is None


def test_chat_pass_model_sentence_with_invented_number_uses_template(app_with_versions):
    """The model's ``confirm_sentence`` invents a number ("4 mm" when the
    param is 3) → the issue #249 number guard fails it → the
    deterministic template is used instead."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
            confirm_sentence="I assumed 4 mm walls. Want it thinner?",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done
    assert done[-1].get("confirm_sentence") == (
        "I assumed 3 for wall_thickness. Want it different?"
    ), done


def test_chat_exhausted_emits_no_offer_and_clears_state(app_with_versions):
    """An exhausted pass emits NO offer (the error frame carries no
    ``offer`` field) and CLEARS any pending offer (a failed run cannot
    keep a live offer)."""

    async def _loop(app, **kwargs):
        return _ExhaustedStub({"wall_thickness": 3.0})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "make a part"})
        row_offer = app_with_versions.state.versions.get_pending_offer(pid)
        return r.status_code, frames, row_offer

    status, frames, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    event_names = [e for e, _d in frames]
    assert event_names[-1] == "error", frames
    for _e, d in frames:
        assert "confirm_offer" not in d, frames
        assert "confirm_sentence" not in d, frames
    assert row_offer is None


def test_chat_second_pass_with_new_param_offers_it(app_with_versions):
    """A follow-up pass that keeps the wall thickness (unchanged → not in
    the changed set) and adds a new assumed param: the new param is a
    fresh assumption and IS offered (the changed-set rule excludes only
    value-changed params, never new ones)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="fillet_radius",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        return r.status_code, frames

    status, frames = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "fillet_radius", done
    assert done[-1].get("confirm_sentence") == (
        "I assumed 1.5 for fillet_radius. Want it different?"
    ), done


def test_chat_yes_after_offer_confirms_param_no_new_version(app_with_versions):
    """DECISIVE: a "yes" after an offer (server-side pending-offer state
    from a prior passing pass) → the param is recorded as user-confirmed
    on the version (``confirmed_params``), the design-state block renders
    it ``stated`` (rule (b)), the acknowledgement is a ``kind:
    "answer"`` plain-message done frame, NO design run happens, and NO
    new version is created."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        # A prior passing pass: a version with an assumed wall thickness
        # + the offer the adapter would have set (persisted server-side,
        # the way the passing pass's adapter writes it).
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        loop_called = {"n": 0}

        async def _loop(app, **kwargs):
            loop_called["n"] += 1
            return _OfferStubResult({"wall_thickness": 3.0})

        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        row = svc.latest_version(pid)
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        ds = await client.get(f"/api/projects/{pid}/design-state")
        return (
            r.status_code,
            frames,
            row,
            len(timeline),
            timeline[0]["id"],
            ds.json(),
            loop_called["n"],
            svc.get_pending_offer(pid),
        )

    status, frames, row, version_count, latest_id, ds_body, loop_n, row_offer = (
        run_async(app_with_versions, _call)
    )
    assert status == 202, status
    # NO design run: the acceptance pre-route short-circuits before it.
    assert loop_n == 0, "the design loop must NOT run on an accepted offer"
    # The reply is a kind "answer" plain-message done frame (the
    # acknowledgement), not a design-loop done frame.
    done = [d for e, d in frames if e == "done"]
    assert len(done) == 1, frames
    assert done[0].get("kind") == "answer", done
    assert "confirm_offer" not in done[0]
    # The acknowledgement rides the done frame with the ADDITIVE
    # ``confirm_ack`` fields (the SPA renders the value in the mono
    # face — a measurement must never hide inside a sentence).
    assert done[0].get("confirm_ack") is True, done
    assert done[0].get("confirm_ack_label") == "Wall thickness", done
    assert done[0].get("confirm_ack_value") == "3", done
    # The ack uses the deterministic template with the #248 label.
    assert done[0]["message"] == "Got it — Wall thickness stays 3.", done
    # The version is user-confirmed (the ONLY writer is this flow).
    assert row["confirmed_params"] == {"wall_thickness": 3.0}, row
    # The design-state block renders it stated (rule (b)).
    by_name = {e["name"]: e for e in ds_body}
    assert by_name["wall_thickness"]["provenance"] == "stated", by_name
    # NO new version: the version count is unchanged and the latest id is
    # the pre-existing version.
    assert version_count == 1, "acceptance must not create a version"
    # The offer is consumed (cleared — it never re-fires on a later yes).
    assert row_offer is None


def test_chat_yes_but_make_it_2mm_is_not_an_acceptance(app_with_versions):
    """"yes but make it 2 mm" is NOT an acceptance (the change request
    routes to the design loop — the pending offer stays as-is for a
    future clean yes, and the design run happens)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 2.0},
            confirm_first="wall_thickness",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes but make it 2 mm"}
        )
        timeline = (await client.get(f"/api/projects/{pid}/versions")).json()
        row = svc.latest_version(pid)
        old = svc.get_version(pid, v["id"])
        return r.status_code, frames, len(timeline), row, old, svc.get_pending_offer(pid)

    status, frames, version_count, row, old, row_offer = run_async(
        app_with_versions, _call
    )
    assert status == 202
    # The design loop ran (a new version exists — the change went through
    # the normal route).
    assert version_count == 2, f"expected a new version, got {version_count}"
    # The old version is NOT confirmed (no confirmation happened — the
    # message was a change request, not an acceptance).
    assert old["confirmed_params"] is None
    # The new version carries a FRESH offer for its own param value
    # (the new version's wall_thickness=2.0 is assumed — the old
    # version's offer was superseded by the new version; the carry
    # forward carried nothing because v1 had no confirmed set).
    # The new version's wall_thickness (2.0, the user's change) was the
    # model's confirm_first flag, but the changed set excludes a
    # user-requested value from being the OFFER's target — a changed
    # value is the user's own move, not an assumption to confirm. With
    # no other eligible param, no offer is emitted.
    # (The acceptance test above pins the confirmation path; the
    # "never re-offer a CONFIRMED param" case is pinned at the pure level
    # in test_selection_never_re_offers_confirmed_param.)
    assert row_offer is None, f"changed param was re-offered: {row_offer}"


def test_chat_yes_after_offer_on_unchanged_param_confirms(app_with_versions):
    """A follow-up pass that keeps the wall thickness (UNCHANGED — not in
    the changed set) and the offer still live from the previous version
    is superseded by the new version's offer (the offer's version must be
    the LATEST — a "yes" to the old version's offer lapses)."""

    async def _loop(app, **kwargs):
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v1 = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v1["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        v2 = svc.latest_version(pid)
        return r.status_code, frames, v2, svc.get_pending_offer(pid)

    status, frames, v2, row_offer = run_async(app_with_versions, _call)
    assert status == 202
    # The old offer (on v1) is superseded by the new version's offer:
    assert row_offer is not None and row_offer["version_id"] == v2["id"]
    # The new pass's done frame carries a FRESH offer (additive
    # ``confirm_*`` fields) for its own version.
    done = [d for e, d in frames if e == "done"]
    assert done[-1].get("confirm_offer") == "wall_thickness", done


def test_chat_yes_without_pending_offer_routes_normally(app_with_versions):
    """A bare "yes" with NO pending offer state falls through to the
    normal routing (the design loop) — the acceptance requires the
    server-side offer, never a client-side guess."""

    async def _loop(app, **kwargs):
        return _OfferStubResult({"wall_thickness": 3.0})

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        loop_called = {"n": 0}

        async def _loop2(app, **kwargs):
            loop_called["n"] += 1
            return _OfferStubResult({"wall_thickness": 3.0})

        app_with_versions.state.run_design_loop = _loop2
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        return r.status_code, frames, loop_called["n"]

    status, frames, loop_n = run_async(app_with_versions, _call)
    assert status == 202
    assert loop_n == 1, "a bare yes with no pending offer must route to the loop"
    # No kind "answer" frame from an offer (the loop's done frame has no
    # kind field — the pass created a version normally).
    done = [d for e, d in frames if e == "done"]
    assert "kind" not in done[-1], done


def test_offer_state_survives_reopen_round_trip(app_with_versions):
    """Persistence: the pending offer (param P on version V) and
    ``confirmed_params`` survive a client-session boundary (a fresh
    client over the same app/DB — the "close the browser, come back"
    simulation): the acceptance check reads them from SERVER state,
    never from the client's chat."""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        # A "yes" in a FRESH client session (the server state is the
        # only source of the offer).
        from httpx import ASGITransport, AsyncClient

        fresh = AsyncClient(
            transport=ASGITransport(app=app_with_versions), base_url="http://t2"
        )
        async with fresh:
            r = await fresh.post(f"/api/projects/{pid}/chat", json={"message": "yes"})
            source = app_with_versions.state.event_sources.get(pid)
            frames = []
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
            back = svc.latest_version(pid)
            offer = svc.get_pending_offer(pid)
            return r.status_code, frames, back, offer

    status, frames, back, offer = run_async(app_with_versions, _call)
    assert status == 202
    done = [d for e, d in frames if e == "done"]
    assert done[0].get("kind") == "answer", done
    assert done[0]["message"] == "Got it — Wall thickness stays 3."
    assert done[0].get("confirm_ack") is True, done
    assert done[0].get("confirm_ack_label") == "Wall thickness", done
    assert done[0].get("confirm_ack_value") == "3", done
    # The confirmation persisted on the version row.
    assert back["confirmed_params"] == {"wall_thickness": 3.0}
    # The offer is consumed.
    assert offer is None


def test_carried_forward_confirmed_param_not_re_offered(app_with_versions):
    """A param confirmed on the previous version (identical value on the
    new one) is NEVER re-offered on the new version: a flag naming the
    carried param is rejected (a confirmation the user made on the
    previous version is confirmed evidence, not an assumption to
    re-confirm — it excludes the param from the new version's offer
    selection), and the new version's ``confirmed_params`` row stays
    NULL (its own set, empty until the user accepts THIS version's
    offer — no carry-forward write)."""

    async def _loop(app, **kwargs):
        # A second pass that keeps wall_thickness at 3 (carried forward)
        # and introduces a new assumed param the model flags:
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="wall_thickness",  # the CARRIED param — rejected
        )

    async def _call(client):
        # The shared connection is closed after the previous test's
        # lifespan teardown; reopen it for this case (the lifespan's
        # ``if state.conn is None`` guard then skips the reconnect, so
        # the reopened connection stays in use — the same pattern as
        # ``test_chat_stage1_matrix_routes_imperatives_to_loop``).
        closed = False
        try:
            app_with_versions.state.conn.raw.execute("SELECT 1")
        except Exception:
            closed = True
        if app_with_versions.state.conn is None or closed:
            import d33d.db as _db
            from d33d import versions as _versions_mod

            fresh = _db.connect(app_with_versions.state.db_path)
            _versions_mod.migrate(fresh)
            app_with_versions.state.conn = fresh
            app_with_versions.state.versions = _versions_mod.VersionService(fresh)
        proj = await create_project(client)
        pid = proj["id"]
        svc = app_with_versions.state.versions
        v1 = await svc.create_version(
            pid, {"wall_thickness": 3.0},
            param_meta={"wall_thickness": {"label": "Wall thickness", "unit": "mm"}},
        )
        svc.set_pending_offer(pid, {"version_id": v1["id"], "param": "wall_thickness"})
        # The user accepts (wall_thickness confirmed on v1).
        app_with_versions.state.run_design_loop = _loop
        r, frames = await _drive_chat(
            app_with_versions, client, pid, {"message": "yes"}
        )
        # The answer path's one-frame source releases its in-flight claim
        # when drained (its ``finally`` — the generator's post-yield code
        # runs on exhaustion); the SSE endpoint does the same in
        # production. A follow-up pass needs the claim free.
        inflight = app_with_versions.state.design_loop_inflight
        if inflight is not None:
            inflight.discard(pid)
        # A second pass creates v2 (the adapter's offer resolution runs
        # against v2 with the carried confirmed set).
        r2, frames2 = await _drive_chat(
            app_with_versions, client, pid, {"message": "add a fillet"}
        )
        v2 = svc.latest_version(pid)
        assert v2["id"] != v1["id"]
        v1_row = svc.get_version(pid, v1["id"])
        return frames, frames2, v2, v1_row, svc.get_pending_offer(pid)

    frames, frames2, v2, v1_row, row_offer = run_async(app_with_versions, _call)
    # v1 confirmed (read inside the lifespan — the connection is live).
    assert v1_row["confirmed_params"] == {"wall_thickness": 3.0}, v1_row
    # v2's confirmed set is its OWN (NULL — the accepted-offer flow is
    # the ONLY writer, and the user has not accepted v2's offer yet);
    # the carried param is excluded from the new version's offer by the
    # previous version's stashed confirmed set (no carry-forward write
    # onto the new row).
    assert v2["confirmed_params"] is None, v2
    # The flag named the CARRIED param (wall_thickness=3, unchanged): a
    # confirmation the user made on the previous version is confirmed
    # evidence, not an assumption to re-confirm — a confirmed param is
    # excluded from the offer (no other eligible param is flagged, so
    # the offer is absent).
    done = [d for e, d in frames2 if e == "done"]
    assert "confirm_offer" not in done[-1], done
    assert row_offer is None


# ---------------------------------------------------------------------------
# Item 1: cross-project race — the pre-pass confirmed set is LOCAL, not
# stashed in ``app.state``
# ---------------------------------------------------------------------------


def test_interleaved_passes_use_own_projects_confirmed_set(app_with_versions):
    """TWO projects' passes interleaved on the shared app: each pass's
    offer selection uses ITS OWN project's pre-pass confirmed set, never
    the other project's. Before the fix, ``app.state._offer_prev_confirmed``
    was a single global attribute — a pass on project B that started after
    project A's pass would read project A's confirmed set (a param B never
    confirmed would be excluded from B's offer, or a confirmed param would
    be re-offered)."""

    async def _loop_a(app, **kwargs):
        # Project A: pass 1 creates a version with wall_thickness assumed.
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _loop_b(app, **kwargs):
        # Project B: pass 1 creates a version with wall_thickness assumed.
        return _OfferStubResult(
            {"wall_thickness": 3.0},
            confirm_first="wall_thickness",
        )

    async def _loop_a2(app, **kwargs):
        # Project A: pass 2 keeps wall_thickness (carried forward) and adds
        # a new param the model flags.
        return _OfferStubResult(
            {"wall_thickness": 3.0, "fillet_radius": 1.5},
            confirm_first="wall_thickness",  # the CARRIED param — must be rejected
        )

    async def _loop_b2(app, **kwargs):
        # Project B: pass 2 keeps wall_thickness (carried forward) and adds
        # a new param the model flags.
        return _OfferStubResult(
            {"wall_thickness": 3.0, "groove_depth": 2.0},
            confirm_first="wall_thickness",  # the CARRIED param — must be rejected
        )

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions

        # Project A: pass 1 → v1_a (wall_thickness assumed, offer set).
        proj_a = await create_project(client, "project A")
        pid_a = proj_a["id"]
        app_with_versions.state.run_design_loop = _loop_a
        r_a1, _ = await _drive_chat(app_with_versions, client, pid_a, {"message": "make a part"})
        assert r_a1.status_code == 202

        # Project A: user accepts the offer (wall_thickness confirmed on v1_a).
        inflight = app_with_versions.state.design_loop_inflight
        if inflight is not None:
            inflight.discard(pid_a)
        r_a1yes, _ = await _drive_chat(app_with_versions, client, pid_a, {"message": "yes"})
        assert r_a1yes.status_code == 202

        # Project B: pass 1 → v1_b (wall_thickness assumed, offer set).
        # This runs AFTER project A's acceptance — if the pre-pass set were
        # stashed in app.state, project B's adapter would read project A's
        # confirmed set (the cross-project race).
        proj_b = await create_project(client, "project B")
        pid_b = proj_b["id"]
        app_with_versions.state.run_design_loop = _loop_b
        r_b1, _ = await _drive_chat(app_with_versions, client, pid_b, {"message": "make a part"})
        assert r_b1.status_code == 202

        # Project B: user accepts the offer (wall_thickness confirmed on v1_b).
        if inflight is not None:
            inflight.discard(pid_b)
        r_b1yes, _ = await _drive_chat(app_with_versions, client, pid_b, {"message": "yes"})
        assert r_b1yes.status_code == 202

        # Project A: pass 2 → v2_a. The adapter must read v1_a's confirmed
        # set (wall_thickness confirmed) — NOT project B's set (which also
        # confirms wall_thickness, but via project B's own acceptance). The
        # flag names the CARRIED param → rejected → no offer.
        if inflight is not None:
            inflight.discard(pid_a)
        app_with_versions.state.run_design_loop = _loop_a2
        r_a2, frames_a2 = await _drive_chat(app_with_versions, client, pid_a, {"message": "add a fillet"})
        assert r_a2.status_code == 202
        v2_a = svc.get_version(pid_a, (await client.get(f"/api/projects/{pid_a}/versions")).json()[-1]["id"])

        # Project B: pass 2 → v2_b. Same expectation — the adapter must
        # read v1_b's confirmed set, not project A's.
        if inflight is not None:
            inflight.discard(pid_b)
        app_with_versions.state.run_design_loop = _loop_b2
        r_b2, frames_b2 = await _drive_chat(app_with_versions, client, pid_b, {"message": "add a groove"})
        assert r_b2.status_code == 202
        v2_b = svc.get_version(pid_b, (await client.get(f"/api/projects/{pid_b}/versions")).json()[-1]["id"])

        done_a2 = [d for e, d in frames_a2 if e == "done"]
        done_b2 = [d for e, d in frames_b2 if e == "done"]
        return (
            v2_a, v2_b,
            done_a2[-1] if done_a2 else {},
            done_b2[-1] if done_b2 else {},
            svc.get_pending_offer(pid_a),
            svc.get_pending_offer(pid_b),
        )

    v2_a, v2_b, done_a2, done_b2, offer_a, offer_b = run_async(
        app_with_versions, _call
    )
    # Both projects: v2's confirmed set is its own (NULL — no carry-forward
    # write). The carried param (wall_thickness, confirmed on v1) is
    # EXCLUDED from the offer by the pre-pass confirmed set → the flag
    # naming it is rejected → no offer (a new param is introduced but not
    # flagged, so no fallback).
    assert v2_a["confirmed_params"] is None, v2_a
    assert v2_b["confirmed_params"] is None, v2_b
    assert "confirm_offer" not in done_a2, done_a2
    assert "confirm_offer" not in done_b2, done_b2
    assert offer_a is None
    assert offer_b is None


# ---------------------------------------------------------------------------
# Item 3: malformed confirmed_params row + set_pending_offer shape guard
# ---------------------------------------------------------------------------


def test_record_confirmation_malformed_confirmed_params_starts_from_empty(app_with_versions):
    """A version row whose ``confirmed_params`` is NOT a dict (corrupted
    JSON or hand-edited row): ``record_confirmation`` logs a WARNING and
    starts from ``{}`` — never a 500, never a crash."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "malformed test")
        pid = proj["id"]
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        vid = v["id"]
        # Corrupt the confirmed_params column: store a JSON list (valid
        # JSON, not a dict — simulates a corrupted row that was written
        # by a buggy writer). get_version's json.loads parses it to a
        # list; record_confirmation must handle the non-dict gracefully.
        svc.conn.raw.execute(
            "UPDATE versions SET confirmed_params = ? WHERE id = ?",
            ("[1, 2, 3]", vid),
        )
        svc.conn.commit()
        # record_confirmation must NOT raise — it starts from {}.
        svc.record_confirmation(pid, vid, "wall_thickness", 3.0)
        row = svc.get_version(pid, vid)
        return row

    row = run_async(app_with_versions, _call)
    # The write overwrote the malformed value with a well-formed set.
    assert row["confirmed_params"] == {"wall_thickness": 3.0}, row


def test_set_pending_offer_shape_guard_rejects_bad_input(app_with_versions):
    """``set_pending_offer`` with a malformed offer dict raises ValueError
    (never silently persists a bad row that would degrade to no offer on
    read anyway — the raise is the early, loud failure)."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "shape guard test")
        pid = proj["id"]
        results = {}
        # version_id is a bool (not a real int).
        try:
            svc.set_pending_offer(pid, {"version_id": True, "param": "x"})
            results["bool_version"] = "no raise"
        except ValueError as e:
            results["bool_version"] = str(e)
        # version_id is a string.
        try:
            svc.set_pending_offer(pid, {"version_id": "1", "param": "x"})
            results["str_version"] = "no raise"
        except ValueError as e:
            results["str_version"] = str(e)
        # param is empty.
        try:
            svc.set_pending_offer(pid, {"version_id": 1, "param": ""})
            results["empty_param"] = "no raise"
        except ValueError as e:
            results["empty_param"] = str(e)
        # param is not a string.
        try:
            svc.set_pending_offer(pid, {"version_id": 1, "param": 42})
            results["non_str_param"] = "no raise"
        except ValueError as e:
            results["non_str_param"] = str(e)
        # version_id missing.
        try:
            svc.set_pending_offer(pid, {"param": "x"})
            results["missing_version"] = "no raise"
        except ValueError as e:
            results["missing_version"] = str(e)
        # A well-formed offer still works (the guard doesn't reject valid
        # input).
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        results["valid"] = svc.get_pending_offer(pid)
        # None (clear) still works.
        svc.set_pending_offer(pid, None)
        results["clear"] = svc.get_pending_offer(pid)
        return results

    results = run_async(app_with_versions, _call)
    # All bad inputs raised ValueError.
    assert "no raise" not in results["bool_version"]
    assert "no raise" not in results["str_version"]
    assert "no raise" not in results["empty_param"]
    assert "no raise" not in results["non_str_param"]
    assert "no raise" not in results["missing_version"]
    # Valid offer was persisted.
    assert results["valid"] is not None
    assert results["valid"]["param"] == "wall_thickness"
    # Clear worked.
    assert results["clear"] is None


# ---------------------------------------------------------------------------
# Item 5: ack label is always non-empty
# ---------------------------------------------------------------------------


def test_ack_label_falls_back_to_param_name(app_with_versions):
    """The ack's ``confirm_ack_label`` is ALWAYS non-empty: when the
    param has no user-facing label (``param_meta`` is absent or the label
    key is missing), the label falls back to the param's identifier —
    never an empty string."""

    async def _call(client):
        _reopen_conn(app_with_versions)
        svc = app_with_versions.state.versions
        proj = await create_project(client, "ack label test")
        pid = proj["id"]
        # A version with NO param_meta (no label — the identifier fallback
        # must fire).
        v = await svc.create_version(pid, {"wall_thickness": 3.0})
        # No param_meta → the design-state entry has no "label" key →
        # the ack label falls back to the param name.
        svc.set_pending_offer(pid, {"version_id": v["id"], "param": "wall_thickness"})
        app_with_versions.state.run_design_loop = None
        r, frames = await _drive_chat(app_with_versions, client, pid, {"message": "yes"})
        done = [d for e, d in frames if e == "done"]
        return r.status_code, done

    status, done = run_async(app_with_versions, _call)
    assert status == 202, status
    assert len(done) == 1, done
    # The ack label is the param's identifier (non-empty, never "").
    assert done[0].get("confirm_ack_label") == "wall_thickness", done
    assert done[0]["confirm_ack_label"] != ""
    # The ack value is still correct.
    assert done[0].get("confirm_ack_value") == "3", done
    # The message uses the identifier as the label.
    assert done[0]["message"] == "Got it — wall_thickness stays 3.", done
