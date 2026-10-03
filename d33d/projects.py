"""Project CRUD router + per-project git repo + photo upload (issue #23, workstream task-b).

Endpoints (registered on the FastAPI app from ``d33d.app.create_app``):

- ``POST   /api/projects``               — create a project row + git-init the repo
- ``GET    /api/projects``               — list all projects
- ``GET    /api/projects/{id}``          — get a single project
- ``PATCH  /api/projects/{id}``          — update name / tags / notes
- ``DELETE /api/projects/{id}``          — delete the row AND the on-disk git dir
- ``POST   /api/projects/{id}/photos``   — multipart upload (png/jpeg, ≤ 20 MB)

The git repo is the content spine: it is initialised at ``git_repo_path`` on
project creation and committed to on each photo upload. The DB row only
carries the path.

Git operations are local-only (``git init``, ``git config``, ``git add``,
``git commit``). No remote, no network. The identity is set per-repo
(``user.email`` / ``user.name``) so tests work on machines without a global
git identity.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from d33d import db as db_mod
from d33d import fill_recut
from d33d.chat_frames import answered_frames as _answered_frames
from d33d.chat_loop import run_design_loop as chat_loop_run_design_loop
from d33d.design_frames import PHOTO_MISSING_NOTICE, SAVED_DESIGN_MISSING_REPLY
from d33d.design_loop_events import photo_storage_signal
from d33d.photo_upload import photo_upload_route
from d33d.project_git import (
    init_git_repo,
    remove_repo,
    repo_present,
)

logger = logging.getLogger(__name__)

# The git primitives live in ``d33d.project_git`` (shared by the
# photo-upload and design-source routes without the projects → … → projects
# import cycle); this module re-exports the two design-state notice strings
# (``PHOTO_MISSING_NOTICE`` / ``SAVED_DESIGN_MISSING_REPLY``, from
# ``d33d.design_frames``) under their historical names.

# ---------------------------------------------------------------------------
# Pydantic models for request bodies
# ---------------------------------------------------------------------------


class ProjectCreate(BaseModel):
    name: str
    tags: list[str] | None = None
    notes: str = ""


class ProjectUpdate(BaseModel):
    name: str | None = None
    tags: list[str] | None = None
    notes: str | None = None


class ChatRequest(BaseModel):
    """Body of ``POST /api/projects/{id}/chat`` (issue #54).

    ``message`` is the user's chat text (the design-loop request text).
    ``stated_dims`` is an optional (W, D, H) triple in mm. The body's
    ``stated_dims`` REPLACES (not merges with) the message extraction:
    when present, ONLY the body's axes (those ``> 0``) feed the loop's
    gate; when absent (the SPA never sends the field), the server
    resolves dimensions itself via the existing
    ``d33d.dimension_protocol`` per-axis extraction of the user's OWN
    message (``stated_axes_from_message``) — there is no other source.
    The result is the current turn's per-axis confirmed set (ticket
    #91): a PARTIAL confirmation is a zero-filled triple (unconfirmed
    axes abstain per-axis), ``None`` when no axis is confirmed THIS turn
    (the gate ABSTAINS — ``Score.bbox_abstained`` — never a fabricated
    ``(0.0, 0.0, 0.0)`` target). There is deliberately NO fallback to a
    persisted version row's dimensions: the gate enforces only what the
    current turn confirmed (issue #247's operator decision).

    ``chat_history`` is the list of prior user messages (the SPA sends the
    last 10); absent → empty tuple.
    """

    message: str
    stated_dims: list[float] | None = None
    chat_history: list[str] | None = None

    @field_validator("message")
    @classmethod
    def _message_must_be_nonempty(cls, v: str) -> str:
        if not v.strip():
            raise ValueError("message must be non-empty and not whitespace-only")
        return v

    @field_validator("stated_dims")
    @classmethod
    def _stated_dims_must_be_3_finite(cls, v: list[float] | None) -> list[float] | None:
        if v is None:
            return None
        if len(v) != 3:
            raise ValueError("stated_dims must be a 3-element array [W, D, H]")
        import math

        for x in v:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                raise TypeError("stated_dims elements must be numbers")
            x = float(x)
            if not math.isfinite(x):
                raise ValueError("stated_dims elements must be finite numbers")
            if x < 0:
                # Negative is malformed input; 0 stays legal = unconfirmed axis.
                raise ValueError("stated_dims elements must be >= 0")
        return [float(x) for x in v]


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


async def _confirm_offer_route(app: Any, project_id: int, message: str):
    """The issue #250 offer-acceptance decision for ONE chat message.

    Returns ``{"kind": "answer", "answer": <acknowledgement>}`` when ALL
    of the following hold (otherwise ``None`` — the caller routes to the
    existing question pre-route / design loop exactly as today):

    * the project has a PENDING offer (``versions.get_pending_offer`` —
      server-side state written by the design pass's adapter, never
      client-derived);
    * the offer is LIVE: its version is the project's CURRENT LATEST
      version (a newer version supersedes it — a stale offer lapses);
    * the message is a CLEAN AFFIRMATION (``_is_clean_affirmation`` —
      short, no question mark, no negation, no hedge — "yes but make it
      2 mm" is NOT an acceptance);
    * the offered param is still ASSUMED and still carries the offered
      value in the latest version's design-state block (a value the user
      already moved, or a param the state no longer marks assumed, is not
      a live offer).

    On acceptance (the body, run under no lock — SQLite's own write
    serialization is the concurrency boundary, same as the
    ``design_state`` reads elsewhere in this module): the value is
    recorded as user-confirmed on the version row
    (``versions.record_confirmation`` — the ONLY writer of
    ``confirmed_params``) and the pending offer is CLEARED (a consumed
    offer is a consumed offer — it never re-fires on a later "yes").
    """
    from d33d.confirm_offer import (
        ack_sentence,
        is_pending_offer_acceptance,
        offer_entry,
    )
    from d33d.design_state import state_block_for_version

    versions = app.state.versions
    offer = versions.get_pending_offer(project_id)
    if offer is None:
        return None
    latest = versions.latest_version(project_id)
    if not is_pending_offer_acceptance(message, offer, latest):
        return None
    name = offer["param"]
    # The same single-source block the Brief shows (issue #300's operator
    # decision): the acceptance pre-route reads provenance from
    # ``state_block_for_version`` — not the stated/measurement-blind
    # ``state_block_from_params`` the pre-fix path used — so a param the
    # Brief renders ``stated`` (rule (a) axis promotion / rule (b)
    # confirmed), ``measured`` or ``disagrees`` is never confirmed. A
    # "yes" is accepted only when the param row is EXACTLY ``assumed``
    # here; any other provenance lapses the offer (the offer stays
    # untouched — the next pass overwrites the pending-offer state — and
    # the message routes normally). The block carries the row's label /
    # value, so the ack and its ``confirm_ack_*`` fields ride the same
    # single source (the ``meta_unit`` / ``param_axis`` graft is applied
    # inside ``offer_entry`` as before — the value spelling is unchanged).
    block = state_block_for_version(
        dict(latest["params"]),
        latest["bbox"],
        latest["stated_dims"],
        latest["param_meta"],
        latest["confirmed_params"],
    )
    entry = offer_entry(
        dict(latest["params"]), latest["param_meta"], name, block_entries=block
    )
    if entry is None or entry.get("provenance") != "assumed":
        # The param is gone or no longer assumed (confirmed/stated /
        # measured): the offer is stale — leave it be (the next pass
        # overwrites the pending-offer state) and route normally.
        return None
    versions.record_confirmation(project_id, latest["id"], name, entry.get("value"))
    versions.set_pending_offer(project_id, None)
    return {
        "kind": "answer",
        "answer": ack_sentence(entry),
        "entry": entry,
        "param": name,
    }


def _storage_field(row: dict[str, Any]) -> dict[str, Any]:
    """The project's live storage signal (issue #295), computed server-side
    from live file checks — the SPA reads it and never recomputes presence
    in the client.

    ``{"repo_present": bool, "photo_present": true | false | null}`` — the
    field is ALWAYS present on the project GET response, never omitted.
    ``photo_present`` is a three-way value (JSON ``null`` for a project
    that never had a photo, ``false`` for a lost photo, ``true`` when the
    stored file is on disk) — a photo-LESS project must never read as a
    LOST one (no marker, no notice, no WARNING).

    Git invisibility: the raw repo path is consumed here, never carried
    into the response (the caller already strips it).
    """
    # The storage signal (issue #299) is ONE shared definition —
    # ``photo_storage_signal`` in ``d33d.design_loop_events`` — so the
    # upload, embed and storage checks agree by construction. Cheap: a
    # header-only open + verify + size, memoized per (path, mtime, size),
    # so the project list endpoint never full-decodes per row.
    photo_present = photo_storage_signal(row.get("source_photo_path"))
    return {"repo_present": repo_present(row), "photo_present": photo_present}


def _public_project_row(row: dict[str, Any]) -> dict[str, Any]:
    """A project row with the raw git-repo path removed (git invisibility
    — the on-disk path names a git repo and is never exposed in an API
    response), plus the live ``storage`` signal (issue #295)."""
    out = dict(row)
    out.pop("git_repo_path", None)
    out["storage"] = _storage_field(row)
    return out


def create_projects_router() -> APIRouter:
    """Build and return the projects router.

    The router reads the shared ``db.Connection`` from ``request.app.state.conn``
    at request time (set by the lifespan in ``d33d.app.create_app``).
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])
    # The photo-upload route's body lives in ``d33d.photo_upload`` (the
    # router file stays under the 500-line split threshold; the thin
    # wiring call is the only thing this file keeps of it).
    photo_upload_route(router)

    @router.post("", status_code=201)
    async def create_project(request: Request, body: ProjectCreate) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        # Update the DB row (generates git_repo_path, sets activity)
        project_id = conn.create_project(
            name=body.name,
            tags=body.tags or [],
            notes=body.notes,
        )
        row = conn.get_project(project_id)
        assert row is not None
        repo_path = Path(row["git_repo_path"])
        # Initialise the git repo on disk
        try:
            init_git_repo(repo_path)
        except RuntimeError as e:
            # Rollback: delete the DB row since git init failed
            conn.delete_project(project_id)
            raise HTTPException(status_code=500, detail=f"git init failed: {e}")
        # Git invisibility: the raw on-disk repo path is never exposed.
        return _public_project_row(row)

    @router.get("/{project_id}")
    async def get_project(request: Request, project_id: int) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return _public_project_row(row)

    @router.get("")
    async def list_projects(request: Request) -> list[dict[str, Any]]:
        conn: db_mod.Connection = request.app.state.conn
        out = []
        for r in conn.list_projects():
            out.append(_public_project_row(r))
        return out

    @router.patch("/{project_id}")
    async def update_project(
        request: Request, project_id: int, body: ProjectUpdate
    ) -> dict[str, Any]:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        conn.update_project(
            project_id,
            name=body.name,
            tags=body.tags,
            notes=body.notes,
        )
        updated = conn.get_project(project_id)
        assert updated is not None
        return _public_project_row(updated)

    @router.delete("/{project_id}", status_code=204)
    async def delete_project(request: Request, project_id: int) -> None:
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        # Remove the on-disk git repo (the DB layer does NOT do this)
        repo_path = Path(row["git_repo_path"])
        remove_repo(repo_path)
        # Delete the DB row (cascades transcripts via FK)
        conn.delete_project(project_id)

    @router.post("/{project_id}/chat", status_code=202)
    async def post_chat(request: Request, project_id: int, body: ChatRequest) -> dict[str, Any]:
        """Wire a chat message to the design loop (issue #54).

        Validates the project exists (404), checks the per-project in-flight
        flag (409), registers the event source synchronously (so the SSE
        stream does not terminate on "no active stream"), and starts the
        background design-loop task. Returns 202 immediately — the design
        loop runs in a background asyncio task and streams progress/token
        frames via ``GET /api/stream/{project_id}``.

        The in-flight flag (``app.state.design_loop_inflight``) is set
        synchronously before the 202 response and cleared in the
        background task's ``finally`` on ALL exit paths (pass, exhausted,
        exception) — a leaked flag would 409 every subsequent chat for
        that project forever.
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        app = request.app
        inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
        if inflight is None:
            inflight = set()
            app.state.design_loop_inflight = inflight
        if project_id in inflight:
            raise HTTPException(status_code=409, detail="a design loop is already in flight")

        # Claim the in-flight flag IMMEDIATELY (issue #249 review):
        # the pre-route (``route_chat_message``) awaits a stage-2 LLM
        # call (up to the shared per-LLM-call timeout,
        # ``LLM_CALL_TIMEOUT_SECONDS``) between the 409 check and the old
        # ``inflight.add`` — a second concurrent POST in that window saw
        # an un-set flag and passed the 409. The flag is now set before
        # the pre-route; EVERY exit path that does not end in a
        # registered event source releases it (``inflight.discard``), so
        # a pre-route failure or a design-loop setup failure never leaves
        # the project stuck. The two paths that DO register an event
        # source (the answer path here; the design-loop generator below)
        # keep the flag — streaming.py's ``finally`` clears it when the
        # stream is drained.
        inflight.add(project_id)

        # Issue #295 — the missing-source pre-route (BEFORE the offer
        # pre-route, consistent with every other pre-route running after
        # the in-flight claim — the flag is claimed immediately on entry
        # and released here via ``inflight.discard`` on this exit path,
        # so the net effect is "the flag is never observed as held by a
        # design loop"). When the project's saved design is missing
        # (``source_expected`` — the repo gone, or the current version's
        # recorded design.scad lost out-of-band), the reply is the fixed
        # copy.ts text as a plain ``kind: "answer"`` done frame via the
        # existing ``_answered_frames`` pattern: 202, ONE done frame, no
        # design run, no version, no version row, and the flag released
        # by the stream drain (the same contract as the #249/#260
        # no-run replies). A project that never had a version is NOT the
        # missing state (turn one proceeds to the design loop unchanged).
        from d33d.design_source import source_expected

        if source_expected(row, app.state.versions):
            logger.warning(
                "chat for project %s: the saved design is missing on "
                "disk — replying with the missing-source notice, no "
                "design run",
                project_id,
            )
            app.state.event_sources[project_id] = _answered_frames(
                SAVED_DESIGN_MISSING_REPLY
            )
            return {"status": "accepted"}

        # Issue #332 (sub-issue 3) — the unsettled-part + fill-and-recut
        # pre-routes (BEFORE the offer/question pre-routes, after the
        # missing-source check). The module (``d33d.fill_recut``) owns the
        # whole flow — the unsettled guard, the trigger, the boundary
        # copy, and the offer build/accept/clear; this route keeps only
        # the answer-frame plumbing (register the done frame, return the
        # 202 body) and the accepted-offer instruction (appended to the
        # request text further down).
        # The fill-recut pre-route runs after the in-flight claim but BEFORE
        # an event source is registered — the same contract as the #250
        # guard below: ANY failure here releases the flag (the stream's
        # ``finally`` never runs) and re-raises.
        try:
            from d33d.part_http import part_public

            _part = part_public(row) if row.get("part_filename") else None
            _fill_recut_instruction: str | None = None
            if _part is not None and _part.get("unit_status") not in (
                "assumed",
                "settled",
            ):
                logger.warning(
                    "chat for project %s: the part's units are unsettled — "
                    "replying with the settle-first notice, no design run",
                    project_id,
                )
                app.state.event_sources[project_id] = _answered_frames(
                    fill_recut.UNSETTLED_PART_REPLY
                )
                return {"status": "accepted"}
            _fill_recut = fill_recut.fill_recut_turn(app, project_id, body.message)
            if _fill_recut is not None:
                if _fill_recut.get("run_loop"):
                    _fill_recut_instruction = _fill_recut["instruction"]
                    logger.info(
                        "chat for project %s: fill-and-recut offer accepted — "
                        "running the design loop with the fill-and-recut "
                        "instruction (len(message)=%d)",
                        project_id,
                        len(body.message),
                    )
                else:
                    app.state.event_sources[project_id] = _answered_frames(
                        _fill_recut["answer"], fill_recut_offer=True
                    )
                    return {"status": "accepted"}
        except Exception:
            inflight.discard(project_id)
            logger.exception(
                "chat for project %s: the fill-recut pre-route failed — "
                "the in-flight flag is released",
                project_id,
            )
            raise

        # Issue #250 — the offer-acceptance pre-route (BEFORE the
        # question pre-route): if the project has a LIVE pending offer
        # (the previous turn's design pass offered to confirm param P on
        # the current latest version — server-side state, never parsed
        # from the client's chat) AND the message is a clean affirmation
        # (``_is_clean_affirmation`` — "yes" yes; "yes but make it 2 mm"
        # no) AND the offered param still carries the offered value, the
        # acceptance records the value as user-confirmed on that version
        # (the ``confirmed_params`` write — the ONLY writer of that set)
        # and replies with the deterministic acknowledgement as a
        # ``kind: "answer"`` plain-message done frame. NO design run, NO
        # new version. Anything else (a change request, a question, a
        # hedge, a lapsed/stale offer, a value that moved) falls through
        # to the existing routes exactly as today.
        try:
            offer_route = await _confirm_offer_route(app, project_id, body.message)
        except Exception:
            inflight.discard(project_id)
            raise
        if offer_route is not None:
            # The event source is registered — the flag stays set
            # (streaming.py's ``finally`` clears it when the stream is
            # drained). The answer path has no SSE client (the SPA polls
            # the stream), so the source is drained by the test's direct
            # iteration; popping the drained source here would be too
            # early (the SSE endpoint reads it at request time). The flag
            # is released when the source is exhausted — the SAME contract
            # as the question-answer path. The acknowledgement's label /
            # value ride the done frame as ADDITIVE ``confirm_ack_*``
            # fields (the SPA renders the value in the mono face).
            from d33d.confirm_offer import mm_value_str

            ack_entry = offer_route["entry"]
            app.state.event_sources[project_id] = _answered_frames(
                offer_route["answer"],
                project_id,
                app,
                confirm_ack={
                    # The label is ALWAYS non-empty (the wire contract — the
                    # SPA's ``confirm_ack`` render gate keys off it): fall
                    # back to the param's identifier, never an empty
                    # string (the identifier is present by construction —
                    # ``offer_entry`` returns an entry whose ``name`` is
                    # the pending offer's param).
                    "label": (
                        ack_entry.get("label")
                        or ack_entry.get("name")
                        or offer_route["param"]
                    ),
                    # The shared value formatter (``confirm_offer`` — the
                    # same spelling the ack sentence used: mm-formatted
                    # for a genuinely-mm param, bare otherwise — issue
                    # #265).
                    "value": mm_value_str(ack_entry),
                },
            )
            return {"status": "accepted"}

        # The design-loop setup (the per-axis stated-evidence resolution,
        # the photo capture, and the loop's event-source registration) lives
        # in ``d33d.chat_loop`` — the thin entry point keeps this router file
        # under the 500-line split threshold (AGENTS.md).
        chat_history = tuple(body.chat_history or ())
        try:
            return await chat_loop_run_design_loop(
                app,
                project_id,
                row,
                body.message,
                body.stated_dims,
                chat_history,
                _fill_recut_instruction,
            )
        except Exception:
            # The design-loop setup raised BEFORE an event source was
            # registered — the in-flight flag is released FIRST (the
            # stream's ``finally`` never runs, so the flag must be
            # released here; the setup path never releases it), then the
            # accepted fill-recut
            # offer is restored inside its OWN try/except so a restore
            # failure never masks the original setup exception (the
            # ``fill_recut_region._handle_acceptance`` pattern).
            inflight.discard(project_id)
            if _fill_recut is not None and _fill_recut.get("run_loop"):
                try:
                    app.state.versions.set_pending_offer(
                        project_id, _fill_recut.get("accepted_offer")
                    )
                except Exception:
                    logger.exception(
                        "chat for project %s: restoring the accepted "
                        "fill-recut offer after a design-loop setup "
                        "failure failed — the acceptance may be lost",
                        project_id,
                    )
            logger.exception(
                "chat for project %s: design-loop setup failed — the "
                "in-flight flag is released",
                project_id,
            )
            raise

    return router


__all__ = [
    "PHOTO_MISSING_NOTICE",
    "SAVED_DESIGN_MISSING_REPLY",
    "create_projects_router",
]
