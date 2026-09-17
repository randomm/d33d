"""Version HTTP routes (issue #8).

The backend half of the five surfaces, registered on the FastAPI app from
``d33d.app.create_app``:

- ``GET   /api/projects/{id}/versions``                  — timeline (with
                                                            diff badges)
- ``POST  /api/projects/{id}/versions``                  — create (manual)
- ``GET   /api/projects/{id}/versions/{version_id}``     — single version
- ``PATCH /api/projects/{id}/versions/{version_id}``     — rename / pin /
                                                            archive / thumbnail
- ``POST  /api/projects/{id}/versions/{version_id}/restore``       —
                                                            non-destructive
                                                            restore (forward
                                                            commit)
- ``POST  /api/projects/{id}/versions/{version_id}/set-as-main``
- ``POST  /api/projects/{id}/versions/{version_id}/branch-from``
- ``GET   /api/projects/{id}/versions/compare?a=&b=``    — two-version
                                                            compare (diff
                                                            table +
                                                            shared-rotation
                                                            contract)
- ``GET   /api/projects/{id}/gallery``                   — pinned variant
                                                            cards (``?archived=1``)
- ``GET   /api/library``                                  — project library
                                                            grid (search
                                                            over names/tags/
                                                            notes is
                                                            client-side)

Git is never shown: response bodies carry no commit hash, branch name, or raw
git output — a contract test asserts the invariant.

Design-loop FINALIZE (the accepted-change boundary — a version is created
exactly when the design loop passes validation, never on clarify/propose/
patch/critique):

- ``GET  /api/projects/{id}/design-source``    — the current OpenSCAD source
- ``POST /api/projects/{id}/design-source``    — bounded source upload
                                                  (committed to the repo)
- ``POST /api/projects/{id}/finalize``         — runs the injected design
                                                  loop (``app.state.
                                                  run_design_loop``); a
                                                  ``pass`` result versions
                                                  the best candidate's
                                                  parameters; any other
                                                  status is 422 (never a
                                                  spurious version)

The design loop is injected via ``app.state.run_design_loop`` (the same
dependency-injection seam as ``build_registry_glb``) so tests never need a
live LLM, Docker render, or catalogue.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from d33d import versions as versions_mod
from d33d.design_state import state_block_from_params

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Design-source upload bound (same discipline as the photo-upload cap).
# ---------------------------------------------------------------------------

MAX_SCAD_SOURCE_BYTES = 1 * 1024 * 1024  # 1 MB — a parametric .scad is KBs.

#: Bounded body drain timeout — a stalled client must not hold the
#: version-write lock (held across write + commit) indefinitely.
_DRAIN_TIMEOUT_SECONDS = 30


def _design_source_path(project: dict[str, Any]) -> Path:
    """The per-project OpenSCAD source path inside the git repo (versioned)."""
    return Path(project["git_repo_path"]) / "design.scad"


# ---------------------------------------------------------------------------
# Request bodies
# ---------------------------------------------------------------------------


class VersionCreateRequest(BaseModel):
    """Body of ``POST .../versions``.

    ``params`` is the COMPLETE parameter set (full snapshot, never a
    delta) — the OpenSCAD Customizer presets insight. Values are scalars
    (number | string | bool); the key set is owned by the design loop's
    named-parameter block (this ticket diffs, it never invents keys).
    """

    params: dict[str, Any]
    name: str | None = None
    message: str = Field(default="", max_length=1000)


class VersionPatchRequest(BaseModel):
    """Body of ``PATCH .../versions/{id}`` — any subset of the fields."""

    name: str | None = None
    pinned: bool | None = None
    archived: bool | None = None
    thumbnail: str | None = None


class FinalizeBody:
    """Parsed FINALIZE body (thin holder — the JSON boundary is validated
    in ``_parse_finalize_body`` so non-dict bodies map to clean 400/422s).
    """

    def __init__(
        self,
        params: dict[str, Any] | None,
        name: str | None,
        message: str,
        photo: str | None = None,
        request: str | None = None,
        stated_dims: tuple[float, float, float] | None = None,
    ) -> None:
        self.params = params
        self.name = name
        self.message = message
        self.photo = photo
        self.request = request
        self.stated_dims = stated_dims


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_versions_router() -> APIRouter:
    """Build the versions router.

    Reads the shared ``VersionService`` from ``request.app.state`` (set by
    the lifespan in ``d33d.app.create_app``).
    """
    router = APIRouter(tags=["versions"])

    def _service(request: Request) -> versions_mod.VersionService:
        svc: versions_mod.VersionService | None = request.app.state.versions
        assert svc is not None  # set by lifespan
        return svc

    def _project_or_404(svc: versions_mod.VersionService, project_id: int):
        row = svc.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")
        return row

    def _version_or_404(
        svc: versions_mod.VersionService, project_id: int, version_id: int
    ):
        v = svc.get_version(project_id, version_id)
        if v is None:
            raise HTTPException(status_code=404, detail="version not found")
        return v

    # -- timeline ------------------------------------------------------------

    @router.get("/api/projects/{project_id}/versions")
    async def list_versions(request: Request, project_id: int) -> list[dict[str, Any]]:
        """Version timeline: oldest first, each entry with name, triggering
        message excerpt, timestamp, thumbnail, parent link, and a diff badge
        (``diff_count`` = params that changed vs the version's OWN parent —
        never the list-position predecessor, so a restored version's badge
        is correct even though its parent is not the row before it; 0 for a
        version without a parent)."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        by_id: dict[int, dict[str, Any]] = {
            v["id"]: v for v in svc.list_versions(project_id)
        }
        out: list[dict[str, Any]] = []
        for v in by_id.values():
            entry = svc._version_public(v)
            parent = by_id.get(v["parent"]) if v["parent"] is not None else None
            if parent is None:
                entry["diff_count"] = 0
            else:
                added, removed, changed = versions_mod.diff_params(
                    parent["params"], v["params"]
                )
                entry["diff_count"] = len(added) + len(removed) + len(changed)
            out.append(entry)
        return out

    # -- compare (the prioritized surface) --------------------------------------
    # ``compare`` is a literal segment in a distinct route pattern; it cannot
    # collide with the ``{version_id}`` segment — no ordering dependency.

    @router.get("/api/projects/{project_id}/versions/compare")
    async def compare(
        request: Request,
        project_id: int,
        a: int = Query(...),
        b: int = Query(...),
    ) -> dict[str, Any]:
        """Two-version compare: both full param sets, the computed diff
        table (added/removed/changed), and the shared-rotation contract
        (identical units/axis convention → the two client viewports share
        one rotation state). No geometry payload."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        _version_or_404(svc, project_id, a)
        _version_or_404(svc, project_id, b)
        return svc.get_compare(project_id, a, b)

    # -- single version -------------------------------------------------------

    @router.get("/api/projects/{project_id}/versions/{version_id}")
    async def get_version(
        request: Request, project_id: int, version_id: int
    ) -> dict[str, Any]:
        svc = _service(request)
        _project_or_404(svc, project_id)
        v = _version_or_404(svc, project_id, version_id)
        return svc._version_public(v)

    # -- create (manual) ------------------------------------------------------

    @router.post("/api/projects/{project_id}/versions", status_code=201)
    async def create_version(
        request: Request, project_id: int, body: VersionCreateRequest
    ) -> dict[str, Any]:
        svc = _service(request)
        _project_or_404(svc, project_id)
        try:
            v = await svc.create_version(
                project_id, body.params, name=body.name, message=body.message
            )
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)
        return svc._version_public(v)

    # -- patch (rename / pin / archive / thumbnail) ---------------------------

    @router.patch("/api/projects/{project_id}/versions/{version_id}")
    async def patch_version(
        request: Request, project_id: int, version_id: int, body: VersionPatchRequest
    ) -> dict[str, Any]:
        svc = _service(request)
        _project_or_404(svc, project_id)
        _version_or_404(svc, project_id, version_id)
        if body.name is not None:
            await _call(svc.rename_version, project_id, version_id, body.name)
        if body.pinned is not None:
            await _call(svc.set_pinned, project_id, version_id, body.pinned)
        if body.archived is not None:
            await _call(svc.set_archived, project_id, version_id, body.archived)
        if body.thumbnail is not None:
            svc.conn.raw.execute(
                "UPDATE versions SET thumbnail = ? WHERE id = ?",
                (body.thumbnail, version_id),
            )
            svc.conn.commit()
        v = svc.get_version(project_id, version_id)
        assert v is not None
        return svc._version_public(v)

    # -- restore (non-destructive) --------------------------------------------

    @router.post(
        "/api/projects/{project_id}/versions/{version_id}/restore", status_code=201
    )
    async def restore_version(
        request: Request, project_id: int, version_id: int
    ) -> dict[str, Any]:
        """Non-destructive restore: a NEW forward version with the target's
        full snapshot (parent = current latest). Restoring the current
        latest is a 409 (no-op — dedupe, no spurious entry)."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        _version_or_404(svc, project_id, version_id)
        try:
            v = await svc.restore_version(project_id, version_id)
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)
        return svc._version_public(v)

    # -- set as main -----------------------------------------------------------

    @router.post("/api/projects/{project_id}/versions/{version_id}/set-as-main")
    async def set_as_main(
        request: Request, project_id: int, version_id: int
    ) -> dict[str, Any]:
        """Re-point ``current_version`` in place AND write a no-change marker
        commit so the git history records the switch without rewriting."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        _version_or_404(svc, project_id, version_id)
        try:
            row = await svc.set_as_main(project_id, version_id)
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)
        # Git invisibility: mask the raw repo path in the response.
        out = dict(row)
        out.pop("git_repo_path", None)
        return out

    # -- branch-from (variant card) --------------------------------------------

    @router.post("/api/projects/{project_id}/versions/{version_id}/branch-from")
    async def branch_from(
        request: Request, project_id: int, version_id: int
    ) -> dict[str, Any]:
        """Forks are variant cards, not a git graph: creates a NEW PROJECT
        (own git repo) whose first version is seeded from the source
        version's full snapshot. The new repo's history contains no
        source-project commit."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        _version_or_404(svc, project_id, version_id)
        try:
            return await svc.branch_from(project_id, version_id)
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)

    # -- gallery (pinned variants) ---------------------------------------------

    @router.get("/api/projects/{project_id}/gallery")
    async def gallery(
        request: Request,
        project_id: int,
        archived: int = Query(0),
    ) -> list[dict[str, Any]]:
        """Pinned variant cards: thumbnail, name, params, and the available
        actions (set-as-main / branch-from / archive). Archived variants
        are hidden from the default listing (``?archived=1`` reveals them);
        unpinning never deletes the version or its commit."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        if archived:
            items = [
                v
                for v in svc.list_versions(project_id)
                if v["pinned"] and v["archived"]
            ]
        else:
            items = svc.list_pinned(project_id)
        cards = []
        for v in items:
            card = svc._version_public(v)
            card["actions"] = ["set-as-main", "branch-from", "archive"]
            cards.append(card)
        return cards

    # -- project library ---------------------------------------------------------

    @router.get("/api/library")
    async def library(request: Request) -> list[dict[str, Any]]:
        """Project library grid: name, last-activity timestamp, and
        thumbnail for each project (search over names/tags/notes is
        client-side over these full rows)."""
        svc = _service(request)
        return svc.library_cards()

    # -- design state (the block the SPA's Brief renders — issue #120) -------

    @router.get("/api/projects/{project_id}/design-state")
    async def get_design_state(request: Request, project_id: int) -> list[dict[str, Any]]:
        """The design-state block for the project's LATEST version (issue
        #120, consumer 2 — the GET the SPA reads to render the Brief).

        Returns a JSON array of entries: ``name``, ``label``, ``value``
        (nullable — ``unknown`` serialises as ``null``), ``unit`` (``mm``
        for numeric params, ``null`` for non-numeric), and ``provenance``
        (``stated``/``unknown`` at this commit; ``stated_value`` rides
        alongside ONLY for ``disagrees`` entries, which no production data
        source can produce yet — see ``d33d.design_state``). A project
        with no version yet returns an EMPTY array with 200 (never a 404,
        never null — turn one is the commonest case).

        This calls the SAME ``state_block_from_params`` the live prompt
        builder (``d33d.design_loop``) calls — the shared callable, not a
        parallel implementation — so the prompt and the Brief cannot
        drift apart. The route reads the persisted params snapshot only:
        it does NOT re-render to obtain a measurement.
        """
        svc = _service(request)
        _project_or_404(svc, project_id)
        latest = svc.latest_version(project_id)
        params = dict(latest["params"]) if latest is not None else None
        return state_block_from_params(params)

    # -- design source (the versioned OpenSCAD text) ---------------------------

    @router.get("/api/projects/{project_id}/design-source")
    async def get_design_source(request: Request, project_id: int) -> dict[str, Any]:
        """The project's current OpenSCAD source (the conversation's resumed
        design state). ``{"source": null}`` when no design exists yet."""
        svc = _service(request)
        row = _project_or_404(svc, project_id)
        path = _design_source_path(row)
        source = path.read_text(encoding="utf-8") if path.is_file() else None
        return {"source": source}

    @router.post("/api/projects/{project_id}/design-source", status_code=200)
    async def put_design_source(request: Request, project_id: int) -> dict[str, Any]:
        """Bounded OpenSCAD source upload (persisted to the git repo and
        committed — the source is versioned content, not a cache).

        The source is installed atomically (temp file + ``os.replace``,
        same discipline as the catalogue write) and — under the shared
        version-write lock, which also serializes the photo upload's git
        commit on the same repo — committed to the repo. On commit
        failure the written file is unlinked, restoring the pre-write
        state, so ``GET /design-source`` can never observe uncommitted
        source."""
        svc = _service(request)
        row = _project_or_404(svc, project_id)

        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(status_code=400, detail="expected JSON body")
        content = await _read_bounded_source(request)
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(status_code=400, detail="invalid JSON body")
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, str):
            raise HTTPException(status_code=422, detail="'source' must be a string")

        path = _design_source_path(row)
        repo_dir = Path(row["git_repo_path"])

        async def _write_and_commit() -> None:
            versions_mod.install_text_file_atomic(path, source)
            try:
                from d33d.projects import commit_all as _commit_all
                _commit_all(repo_dir, "design source update")
            except RuntimeError as e:
                # Undo the atomic install so the working tree is clean —
                # the pre-write state (prior committed source, or nothing)
                # is restored. The raw git output names the repo on disk
                # — never leak it.
                path.unlink(missing_ok=True)
                raise HTTPException(
                    status_code=500, detail="design source commit failed"
                ) from e

        await svc._with_project_lock(project_id, _write_and_commit)
        # Git invisibility: the on-disk path is server-internal; the
        # response names only the repo-relative file, never the absolute
        # path (which names the repo's on-disk location).
        return {"stored": "design.scad", "length": len(source)}

    # -- FINALIZE (the version-creation boundary) ------------------------------

    @router.post("/api/projects/{project_id}/finalize", status_code=201)
    async def finalize(request: Request, project_id: int) -> dict[str, Any]:
        """Run the injected design loop; a ``pass`` result versions the best
        candidate's parameters; any other status is 422 (no spurious
        version). ``params`` defaults to the current version's snapshot."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        # The current design source (issue #105): the project's current
        # version's per-version source, captured BEFORE the loop runs.
        # ``None`` when no version owns a source yet (turn one).
        row = svc.get_project(project_id)
        assert row is not None  # already 404'd above
        from d33d.design_source import current_version_source

        design_source = current_version_source(row, svc)
        run_loop = request.app.state.run_design_loop
        if run_loop is None:
            raise HTTPException(status_code=503, detail="design loop not wired")

        body = await _parse_finalize_body(request)
        if body.params is not None:
            bad = versions_mod.validate_params(body.params)
            if bad:
                raise HTTPException(
                    status_code=422,
                    detail=f"params must be scalars; bad keys: {bad}",
                )
            params = dict(body.params)
        else:
            # No explicit params: seed from the loop's best candidate
            # (validated after the pass check below) or the latest
            # version's snapshot (a mid-project finalize without a body).
            latest = svc.latest_version(project_id)
            params = dict(latest["params"]) if latest is not None else {}

        try:
            if _loop_takes_app(run_loop):
                result = run_loop(
                    app=request.app,
                    **_finalize_loop_kwargs(request, project_id, body),
                )
            else:
                result = run_loop()
            if inspect.isawaitable(result):
                result = await result
        except (OSError, RuntimeError, TypeError, ValueError) as e:
            # Bounded infra-error set around the injected loop: a raised
            # loop must be a structured 502, not a bare unclassified 500
            # (the same discipline as create_module_registry). TypeError
            # covers a real loop called with an unexpected kwargs
            # contract (e.g. a missing required design-loop argument on
            # an unconfigured project) — it is an infra failure, not a
            # user error.
            logger.exception(
                "design loop failed for project_id=%s (type=%s)",
                project_id,
                type(e).__name__,
            )
            raise HTTPException(
                status_code=502, detail="design loop failed: internal error"
            ) from e
        if result.status != "pass":
            raise HTTPException(
                status_code=422,
                detail=f"design loop did not pass validation: {result.status}",
            )

        # The loop's result is authoritative: on a pass the version's
        # params are the best candidate's ``params`` — a DECLARED
        # ``IterationRecord`` field (issue #93), populated from the
        # defines map the render was made with. The body's params only
        # seed up front; the loop's result overwrites them here.
        #
        # ISSUE #93 SEMANTIC FLIP (stated explicitly): this used to be a
        # duck-typed read of an attribute the real ``IterationRecord``
        # did not declare, so it was ``None`` for every real loop result
        # and the code below ALWAYS fell through to the seed — a fresh-
        # project pass (no body params, no prior version) therefore 502'd.
        # The declared field makes the read succeed: the best candidate's
        # params win, and a fresh-project pass that used to 502 may now
        # return 201 with the candidate's own params.
        named = result.best.params
        if not isinstance(named, dict):
            # Contract violation: the loop's pass produced no usable
            # parameter set. There is no fallback for the finalize seam
            # (the loop is authoritative here, unlike the chat adapter).
            raise HTTPException(
                status_code=502,
                detail="design loop passed but produced no parameter set",
            )
        params = dict(named)

        # The version OWNS its geometry (issue #105): the passing best
        # candidate's source is persisted as ``versions/{id}/design.scad``
        # (written + committed inside create_version, same commit as the
        # params snapshot). A stub without a declared field falls back to
        # the candidate's ``scad_source`` attribute; a candidate with an
        # empty source persists params only (no spurious empty source file).
        best_record = getattr(result, "best", None)
        candidate_source = getattr(best_record, "scad_source", None)
        if not isinstance(candidate_source, str):
            candidate_source = None
        try:
            v = await svc.create_version(
                project_id,
                params,
                name=body.name,
                message=body.message or "design finalize",
                scad_source=(candidate_source or None),
            )
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)
        return svc._version_public(v)

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _finalize_loop_kwargs(
    request: Request, project_id: int, body: FinalizeBody, on_progress: Any = None
) -> dict[str, Any]:
    """The full design-loop kwargs contract for the FINALIZE seam.

    The production loop (``d33d.app._build_production_design_loop``) forwards
    everything except its own hook kwargs (``model`` / ``prompt_version`` /
    ``request``) to ``d33d.design_loop.run_design_loop``, which REQUIRES
    ``photo``, ``stated_dims``, ``render_fn`` and ``llm_fn`` — a zero-kwarg
    call would be a ``TypeError`` that escapes the route's bounded exception
    set, and the hook's ``request`` (``FailureEvent.request`` is
    ``min_length=1``) would be empty so an exhausted loop would never be
    archived. This helper builds that contract from the app state and the
    project row:

    - ``photo`` — the body's photo, else the project's stored
      ``source_photo_path`` (the uploaded reference photo); ``None`` when
      neither exists (text-only finalize — the hook's photo is optional).
    - ``stated_dims`` — the body's dims, else the named W/D/H parameters
      from the latest version's snapshot (0.0 for any unset axis — the
      dimension gate then measures, never fabricates).
    - ``render_fn`` — the production render worker (``d33d.render_worker.
      render_for_design_loop``). ``llm_fn`` — a no-op async edge: the
      production closure builds its OWN ``llm_fn`` from the live catalogue
      and does not consume this kwarg; the key is present so the seam's
      full contract (``photo``, ``stated_dims``, ``render_fn``, ``llm_fn``)
      is satisfied for any future seam variant that does pass it through.
    - ``model`` / ``prompt_version`` / ``request`` — the hook's kwargs
      (popped by the hook before the real loop runs): the resolved
      design-role model id (empty when no catalogue is loaded — honest,
      never a crash), the canonical hash of the design-role prompt (the
      join key that makes "prompt v7 fails case 12 which v5 passed"
      readable), and the user's request text (guaranteed non-empty — the
      failures.jsonl line is un-archivable without it).
    """
    from d33d.config.catalogue import CatalogueError, ResolutionError
    from d33d.prompt_hash import canonical_hash
    from d33d.render_worker import project_renders_dir, render_for_design_loop

    app = request.app
    db_path = getattr(app.state, "db_path", None)
    data_dir = Path(db_path).parent if db_path is not None else Path(".")

    def _render_fn(scad_source: str, defines: dict[str, str]) -> Any:
        # Project-scoped persistence (issue #72): bind the per-project
        # renders_dir so the worker's post-harvest step actually fires in
        # production (the bare ``render_for_design_loop`` reference would
        # leave ``renders_dir`` unset and fall back to the global default).
        return render_for_design_loop(
            scad_source,
            defines,
            renders_dir=project_renders_dir(data_dir, project_id),
            on_progress=on_progress,
        )

    async def _noop_llm_fn(*args: Any, **kwargs: Any) -> Any:
        raise ValueError(
            "finalize route llm_fn must never be called "
            "(the production closure builds its own llm_fn)"
        )

    row = app.state.versions.get_project(project_id)
    assert row is not None  # already 404'd above

    photo = body.photo or row.get("source_photo_path")
    latest = app.state.versions.latest_version(project_id)
    stated_dims = body.stated_dims
    if stated_dims is None:
        p = latest["params"] if latest is not None else {}
        stated_dims = (
            float(p.get("W", 0.0)),
            float(p.get("D", 0.0)),
            float(p.get("H", 0.0)),
        )
    # The design-state block's data source (issue #120): the latest
    # version's full params snapshot, passed INTO the loop (the loop's
    # prompt builder renders it). The GET the SPA reads
    # (``GET /api/projects/{id}/design-state``) calls the SAME shared
    # ``state_block_from_params`` on the same snapshot. ``None`` when no
    # version exists yet (the block renders with zero entries — an honest
    # empty state, never a fabricated dimension).
    state_params: dict[str, Any] | None = (
        dict(latest["params"]) if latest is not None else None
    )

    # The current design source (issue #105): the project's current
    # version's per-version source, captured BEFORE the loop runs (the
    # loop pass creates a version that must NOT carry its own output
    # forward — the carried source is what the model saw as the prior
    # design). ``None`` when no version owns a source yet (turn one).
    from d33d.design_source import current_version_source

    design_source = current_version_source(row, app.state.versions)

    # ``request`` must be non-empty: the hook builds a FailureEvent from
    # it (``min_length=1``) and an empty string would silently drop the
    # failures.jsonl line for an exhausted loop.
    request_text = body.request or body.message
    if not request_text:
        request_text = f"finalize project {project_id}"

    model = ""
    cat = getattr(app.state, "catalogue", None)
    if cat is not None:
        try:
            from d33d.config.resolve import resolve_model

            model = resolve_model(cat, "design").entry.model
        except (CatalogueError, ResolutionError, LookupError):
            model = ""
    prompt_version = canonical_hash(role="design", messages=[])

    return {
        "photo": photo,
        "chat_history": (),
        "stated_dims": stated_dims,
        "render_fn": _render_fn,
        "llm_fn": _noop_llm_fn,
        "model": model,
        "prompt_version": prompt_version,
        "request": request_text,
        "state_params": state_params,
        "design_source": design_source,
    }


def _loop_takes_app(run_loop: Any) -> bool:
    """The injected design-loop seam's signature check.

    True when the seam's callable takes an ``app`` keyword argument (the
    production ``_build_production_design_loop`` closure does); False
    otherwise (test stubs that take no args, or stubs that take a
    different signature — the route calls them with no args for
    backward-compat). The check is a static ``inspect.signature`` read
    over the seam's ``__call__`` / function wrapper, not a per-call
    dynamic behavior change.
    """
    import inspect as _inspect

    try:
        sig = _inspect.signature(run_loop)
    except (TypeError, ValueError):
        return False
    return "app" in sig.parameters


def _raise_mapped(e: Exception) -> None:
    """Map the service's exception taxonomy to HTTP status codes.

    Raises ``HTTPException`` for every known type (and re-raises unknown
    types) — every path raises, so a caller never returns from here with a
    mapped error left unhandled.
    """
    if isinstance(e, LookupError):
        raise HTTPException(status_code=404, detail=str(e))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=422, detail=str(e))
    if isinstance(e, versions_mod.VersionConflictError):
        raise HTTPException(status_code=409, detail=str(e))
    raise e


async def _call(fn, *args: Any) -> Any:
    """Call a service method, mapping its exception taxonomy to HTTP.

    ``fn`` is a sync service method (all current callers are sync); the
    ``hasattr(result, "__await__")`` check is kept for forward-compat
    with async service methods but is never exercised today.
    """
    try:
        result = fn(*args)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
        # _raise_mapped raises on every path (a mapped HTTPException, or the
        # original exception re-raised) — nothing falls through.
        _raise_mapped(e)


async def _read_bounded_source(request: Request) -> str:
    """Read the request body bounded (same discipline as the catalogue PUT)."""
    declared = request.headers.get("content-length")
    try:
        declared_len = int(declared) if declared is not None else None
    except ValueError:
        declared_len = None
    if declared_len is None:
        declared_len = MAX_SCAD_SOURCE_BYTES
    if declared_len > MAX_SCAD_SOURCE_BYTES:
        # Reject early; still drain what the client sends — chunk by
        # chunk, never via ``await request.body()`` (which would buffer
        # the entire remainder in memory) — so the connection stays
        # usable without an unbounded buffer.
        try:
            await asyncio.wait_for(_drain_stream(request), timeout=_DRAIN_TIMEOUT_SECONDS)
        except TimeoutError as e:
            raise HTTPException(
                status_code=413,
                detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit (drain timed out)",
            ) from e
        raise HTTPException(
            status_code=413,
            detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit",
        )
    parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_SCAD_SOURCE_BYTES:
            # Bounded drain of the remainder (see the header-reject path
            # above): read-and-discard, never full-body buffering.
            try:
                await asyncio.wait_for(_drain_stream(request), timeout=_DRAIN_TIMEOUT_SECONDS)
            except TimeoutError as e:
                raise HTTPException(
                    status_code=413,
                    detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit (drain timed out)",
                ) from e
            raise HTTPException(
                status_code=413,
                detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit",
            )
        parts.append(chunk)
    return b"".join(parts).decode("utf-8")


async def _drain_stream(request: Request) -> None:
    """Read-and-discard the request stream (never buffer it)."""
    async for _ in request.stream():
        pass


async def _parse_finalize_body(request: Request):
    """Parse (or default) the FINALIZE body."""
    try:
        content = await _read_bounded_source(request)
        data = json.loads(content) if content.strip() else {}
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise HTTPException(status_code=400, detail="invalid JSON body")
    if not isinstance(data, dict):
        raise HTTPException(status_code=400, detail="body must be a JSON object")
    params = data.get("params")
    if params is not None and not isinstance(params, dict):
        raise HTTPException(status_code=422, detail="'params' must be a JSON object")
    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise HTTPException(status_code=422, detail="'name' must be a string")
    message = data.get("message", "")
    if not isinstance(message, str):
        raise HTTPException(status_code=422, detail="'message' must be a string")
    photo = data.get("photo")
    if photo is not None and not isinstance(photo, str):
        raise HTTPException(status_code=422, detail="'photo' must be a string")
    request = data.get("request")
    if request is not None and not isinstance(request, str):
        raise HTTPException(status_code=422, detail="'request' must be a string")
    stated_dims = data.get("stated_dims")
    if stated_dims is not None:
        if not isinstance(stated_dims, (list, tuple)) or len(stated_dims) != 3:
            raise HTTPException(
                status_code=422, detail="'stated_dims' must be a 3-element array"
            )
        try:
            stated_dims = tuple(float(d) for d in stated_dims)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=422, detail="'stated_dims' must be numeric"
            ) from None
    return FinalizeBody(
        params=params,
        name=name,
        message=message,
        photo=photo,
        request=request,
        stated_dims=stated_dims,
    )


__all__ = [
    "MAX_SCAD_SOURCE_BYTES",
    "create_versions_router",
]
