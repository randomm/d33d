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

import shutil
import subprocess
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, field_validator

from d33d import db as db_mod
from d33d.design_loop_events import (
    axes_to_gate_triple,
    photo_data_uri,
    run_design_loop_with_events,
)
from d33d.dimension_protocol import stated_axes_from_message
from d33d.question_answer import route_chat_message

# ---------------------------------------------------------------------------
# Upload bounds (committed by the issue spec)
# ---------------------------------------------------------------------------

MAX_UPLOAD_BYTES = 20 * 1024 * 1024  # 20 MB
_READ_CHUNK_BYTES = 1024 * 1024  # 1 MiB — bounded read chunk size
ALLOWED_CONTENT_TYPES = {"image/png", "image/jpeg"}

_GIT_USER_EMAIL = "d33d@local"
_GIT_USER_NAME = "d33d"

# Commit-message messages are stored in the per-project git repo's history,
# which downstream consumers (git log parsing, shell tooling, template
# interpolation) treat as data. Sanitize the message text with a strict
# safe-character filter so user-supplied filenames can never inject newlines
# or shell metacharacters into the commit history.
_MAX_COMMIT_MESSAGE_LEN = 200


def _sanitize_commit_message(text: str) -> str:
    """Reduce ``text`` to a single line of safe alnum+``._-`` characters.

    Mirrors the filename-sanitization filter (defensively stricter than the
    caller needs): any character outside the safe set — including newlines,
    shell metacharacters, and other punctuation — is dropped, and the result
    is capped at ``_MAX_COMMIT_MESSAGE_LEN`` characters.
    """
    safe = "".join(c for c in text if c.isalnum() or c in "._-")
    return safe[:_MAX_COMMIT_MESSAGE_LEN]


# ---------------------------------------------------------------------------
# Git helpers (local only, no network)
# ---------------------------------------------------------------------------


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``repo_dir``. Raises on non-zero exit."""
    cmd = ["git", "-C", str(repo_dir), *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result


def init_git_repo(repo_dir: Path) -> None:
    """``git init`` + set local identity. Idempotent (skips if .git exists)."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _git(repo_dir, "init", "-q")
        _git(repo_dir, "config", "user.email", _GIT_USER_EMAIL)
        _git(repo_dir, "config", "user.name", _GIT_USER_NAME)


def commit_all(repo_dir: Path, message: str) -> None:
    """Stage everything and commit. No-op if nothing to commit."""
    _git(repo_dir, "add", "-A")
    # Check if there is anything to commit
    status = _git(repo_dir, "status", "--porcelain")
    if not status.stdout.strip():
        return
    _git(repo_dir, "commit", "-q", "-m", message)


def remove_repo(repo_dir: Path) -> None:
    """Remove the repo directory entirely. No-op if missing."""
    if repo_dir.is_dir():
        shutil.rmtree(repo_dir)


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
                raise ValueError("stated_dims elements must be numbers")
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

    versions = app.state.versions
    offer = versions.get_pending_offer(project_id)
    if offer is None:
        return None
    latest = versions.latest_version(project_id)
    if not is_pending_offer_acceptance(message, offer, latest):
        return None
    name = offer["param"]
    entry = offer_entry(dict(latest["params"]), latest["param_meta"], name)
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


async def _answered_frames(
    answer: str,
    project_id: int | None = None,
    app: Any = None,
    confirm_ack: dict[str, str] | None = None,
):
    """The answer-path SSE stream (issue #249): ONE terminal ``done``
    frame whose ``message`` is the answer text and which carries the
    additive ``kind: "answer"`` discriminator (design-loop done frames
    carry no ``kind`` at all — existing frames are byte-identical).

    ``confirm_ack`` (issue #250, the accepted-offer flow only) adds the
    acknowledgement's ``label``/``value`` as ADDITIVE ``confirm_ack_*``
    fields on the same done frame — the SPA's ``App.tsx`` renders the
    value in the mono face (a measurement must never hide inside a
    sentence). The question-answer path passes ``None`` (no ``confirm_*``
    keys, byte-identical).

    No token frames, no version-created progress frame: the answer text
    is delivered exclusively in the done frame's ``message`` (the
    operator's decision — token frames feed the model-source view, and
    the SPA renders an ``kind: "answer"`` done frame's ``message``
    verbatim as a plain assistant chat message)."""
    done_data: dict[str, Any] = {"message": answer, "kind": "answer"}
    if confirm_ack is not None:
        done_data["confirm_ack"] = True
        done_data["confirm_ack_label"] = confirm_ack["label"]
        done_data["confirm_ack_value"] = confirm_ack["value"]
    yield ("done", done_data)
    # The in-flight flag is released by the STREAM's ``finally``
    # (``d33d.streaming._stream_events`` — the single release point for
    # every event source, on every exit path: the SSE endpoint drains in
    # production; tests that drive the source directly exhaust the same
    # generator via the SSE endpoint's ``_stream_events``). There is
    # deliberately no post-yield discard here: a release would have to
    # happen in generator ``finally`` code (not after the last ``yield`` —
    # that runs only if the consumer exhausts the generator), and
    # ``_stream_events``'s ``finally`` already covers every drain path.
    # A bare ``async for`` over the raw source (bypassing the SSE
    # endpoint) leaves the flag set by design — the contract is that the
    # stream endpoint is the sole driver of event sources (its
    # ``finally`` is the single release point).


def _public_project_row(row: dict[str, Any]) -> dict[str, Any]:
    """A project row with the raw git-repo path removed (git invisibility
    — the on-disk path names a git repo and is never exposed in an API
    response)."""
    out = dict(row)
    out.pop("git_repo_path", None)
    return out


def create_projects_router() -> APIRouter:
    """Build and return the projects router.

    The router reads the shared ``db.Connection`` from ``request.app.state.conn``
    at request time (set by the lifespan in ``d33d.app.create_app``).
    """
    router = APIRouter(prefix="/api/projects", tags=["projects"])

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
        # call (up to 10 s) between the 409 check and the old
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
        except Exception:  # noqa: BLE001 — degrade to the normal route, never 500
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
            from d33d.confirm_offer import format_param_value as _fmt_pv

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
                    # same bool/number/other rule, one implementation).
                    "value": _fmt_pv(ack_entry["value"]),
                },
            )
            return {"status": "accepted"}

        # Resolve the loop's stated dimensions (ticket #91; issue #247's
        # per-axis decision) — the SPA never sends ``stated_dims`` (it
        # posts only ``message`` + ``chat_history``): the loop receives
        # the CURRENT run's per-axis confirmed set (the body's explicit
        # ``stated_dims`` axes when a client sends one, else the
        # protocol's per-axis extraction of the message). NO persisted
        # fallback: a follow-up message with no explicit dimension cue
        # confirms nothing and the gate ABSTAINS (``Score.bbox_abstained``)
        # — it must not enforce an axis confirmed on an earlier turn
        # against a candidate the user just asked to change. A PARTIAL
        # confirmed set is a zero-filled (W, D, H) triple — unconfirmed
        # axes render as ``not specified`` in the prompt and abstain
        # per-axis in the bbox gate; ``None`` (abstain entirely) when the
        # current turn confirmed no axis. This route never reads W/D/H
        # param keys and never reads a persisted version row for the gate.
        chat_history = tuple(body.chat_history or ())

        # Issue #249 — the pre-route (BEFORE the design loop): if the
        # message is a question AND the project's design state can answer
        # it, the answer is emitted on the chat stream as a single
        # terminal done frame (``kind: "answer"`` — the additive
        # discriminator; no token frames, no version-created frame, no
        # version). Everything else — including anything ambiguous — goes
        # to the design loop EXACTLY as today. The route is narrow on
        # purpose (one stage-1 question detector, one cheap stage-2 LLM
        # call with a deterministic number guard, a 10 s hard timeout):
        # the common case ("make it taller") costs nothing.
        try:
            answer_route = await route_chat_message(
                body.message,
                app.state.versions.latest_version(project_id),
                answer_edge=getattr(app.state, "answer_question", None),
            )
        except Exception:
            # The pre-route must never take the project down with it:
            # release the claim and degrade to the design loop (exactly
            # as an un-wired answer edge would).
            inflight.discard(project_id)
            raise
        if answer_route is not None:
            # The event source is registered — the flag stays set
            # (streaming.py's ``finally`` clears it when the stream is
            # drained; no event source means no stream to drain it).
            app.state.event_sources[project_id] = _answered_frames(
                answer_route["answer"]
            )
            return {"status": "accepted"}

        # The per-axis stated evidence — the gate's current-message source
        # AND what the loop pass persists on the new version row (issue
        # #246): the body's explicit ``stated_dims`` axes when a client
        # sends one (the protocol's highest-priority source), else the
        # protocol's per-axis extraction of the user's own words
        # (``stated_axes_from_message`` reuses the same ``_extract_stated``
        # pipeline — partial statements count for the axes they state).
        # A statement that names no axis is ``{}`` → the version row
        # persists NULL (abstain, never a fabricated axis row).
        per_axis_stated: dict[str, float] = {}
        if body.stated_dims is not None:
            _w, _d, _h = body.stated_dims
            if _w > 0:  # ``> 0`` (never truthiness): 0 is the unconfirmed marker
                per_axis_stated["W"] = float(_w)
            if _d > 0:
                per_axis_stated["D"] = float(_d)
            if _h > 0:
                per_axis_stated["H"] = float(_h)
        else:
            per_axis_stated = stated_axes_from_message(body.message, chat_history)

        stated = axes_to_gate_triple(per_axis_stated)

        # Photo: read the project's stored photo NOW (synchronously, before
        # the 202 response) — the background task runs via asyncio and the
        # DB may be closed by the time the loop starts (a deleted project
        # or a closed connection). The photo is captured here as a data URI
        # (MIME from the extension; missing file → the fixed 1x1
        # transparent-PNG constant).
        photo = photo_data_uri(row.get("source_photo_path"))

        # Register the event source synchronously BEFORE the 202 response
        # (else the client stream terminates on "no active stream" — see
        # d33d/streaming.py's contract). The adapter is a plain async
        # generator (not a coroutine): ``event_sources`` maps
        # project_id -> AsyncIterator of (event, data) tuples.
        #
        # The SSE endpoint (GET /api/stream/{project_id}) is the SOLE
        # driver of this generator — a single async generator cannot be
        # driven by two concurrent ``async for`` consumers (CPython raises
        # ``RuntimeError: anext(): asynchronous generator is already
        # running`` on the second consumer's first ``__anext__``). The
        # inflight flag is set here (synchronously, before the 202
        # response) and cleared in the SSE endpoint's ``finally`` when the
        # generator is exhausted (or an SSE client disconnects).
        try:
            events = run_design_loop_with_events(
                app,
                project_id,
                user_message=body.message,
                stated_dims=stated,
                stated_axes=per_axis_stated,
                chat_history=chat_history,
                photo=photo,
                request_text=body.message,
            )
        except Exception:
            # Design-loop setup failed BEFORE an event source was
            # registered: release the claim so the project is not stuck
            # (the flag was claimed before the pre-route — see above).
            inflight.discard(project_id)
            raise
        app.state.event_sources[project_id] = events
        # The flag stays set — the SSE endpoint's ``finally`` clears it
        # on ALL exit paths (generator exhausted, client disconnect,
        # exception).

        return {"status": "accepted"}

    @router.post("/{project_id}/photos", status_code=201)
    async def upload_photo(request: Request, project_id: int) -> dict[str, Any]:
        """Multipart photo upload (png/jpeg, ≤ 20 MB).

        Accepts a single file field (``file``) in the multipart body.
        The content type is validated against the allowed set, the file is
        written to the per-project git repo's ``photos/`` directory, and the
        ``source_photo_path`` DB column is updated.
        """
        conn: db_mod.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        # Parse the multipart body to extract the file
        content_type_header = request.headers.get("content-type", "")
        if "multipart/form-data" not in content_type_header:
            raise HTTPException(
                status_code=400,
                detail="expected multipart/form-data body",
            )

        try:
            form = await request.form()
        except (LookupError, ValueError, OSError) as e:
            raise HTTPException(status_code=400, detail=f"multipart parse error: {e}")

        file = form.get("file")
        if file is None:
            raise HTTPException(status_code=400, detail="missing 'file' field")

        # Validate content type
        file_content_type = getattr(file, "content_type", None) or ""
        if file_content_type not in ALLOWED_CONTENT_TYPES:
            raise HTTPException(
                status_code=400,
                detail=f"content type {file_content_type!r} not allowed (must be image/png or image/jpeg)",
            )

        # Read the file bytes in bounded chunks; abort with 413 as soon as
        # the running total exceeds the cap (never buffers the whole
        # upload first — an unbounded read would defeat the limit).
        buf = bytearray()
        while True:
            chunk = await file.read(_READ_CHUNK_BYTES)
            if not chunk:
                break
            buf.extend(chunk)
            if len(buf) > MAX_UPLOAD_BYTES:
                buf.clear()
                raise HTTPException(
                    status_code=413,
                    detail=f"file exceeds {MAX_UPLOAD_BYTES} byte limit",
                )
        content = bytes(buf)

        # Determine a safe filename
        original_name = getattr(file, "filename", None) or "photo"
        safe_name = Path(original_name).name  # strip path components
        safe_name = (
            "".join(c for c in safe_name if c.isalnum() or c in "._-") or "photo"
        )
        # Commit-message text is sanitized separately (stricter, capped) so
        # the repo's commit history can never carry newlines or shell
        # metacharacters derived from the user-supplied filename.
        commit_subject = _sanitize_commit_message(original_name) or "photo"
        # Ensure extension matches the declared type
        if file_content_type == "image/png":
            if "." in safe_name:
                safe_name = safe_name.rsplit(".", 1)[0] + ".png"
            else:
                safe_name = safe_name + ".png"
        else:
            if "." in safe_name:
                safe_name = safe_name.rsplit(".", 1)[0] + ".jpg"
            else:
                safe_name = safe_name + ".jpg"

        # Write to the repo's photos/ dir
        repo_path = Path(row["git_repo_path"])
        photos_dir = repo_path / "photos"
        photos_dir.mkdir(parents=True, exist_ok=True)
        dest = photos_dir / safe_name
        dest.write_bytes(content)
        size = len(content)

        # Commit the photo to the git repo, under the shared version-write
        # lock (d33d.versions.VersionService._with_project_lock) so EVERY
        # git write to this repo — design-source PUT, version create,
        # set-as-main, and this photo upload — is serialized; concurrent
        # committers would otherwise collide on ``.git/index.lock``.
        svc = getattr(request.app.state, "versions", None)
        try:
            if svc is not None:
                await svc._with_project_lock(project_id, lambda: commit_all(
                    repo_path, f"photo: {commit_subject}"
                ))
            else:  # pragma: no cover - the app lifespan always wires it
                commit_all(repo_path, f"photo: {commit_subject}")
        except RuntimeError as e:
            # Clean up the file but keep the repo consistent
            dest.unlink(missing_ok=True)
            raise HTTPException(status_code=500, detail=f"git commit failed: {e}")

        # Persist the path in the DB
        stored_path = str(dest)
        conn.update_project(project_id, source_photo_path=stored_path)

        return {
            "id": project_id,
            "source_photo_path": stored_path,
            "size": size,
        }

    return router


__all__ = [
    "ALLOWED_CONTENT_TYPES",
    "MAX_UPLOAD_BYTES",
    "create_projects_router",
]
