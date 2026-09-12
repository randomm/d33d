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

import inspect
import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from d33d import versions as versions_mod

# ---------------------------------------------------------------------------
# Design-source upload bound (same discipline as the photo-upload cap).
# ---------------------------------------------------------------------------

MAX_SCAD_SOURCE_BYTES = 1 * 1024 * 1024  # 1 MB — a parametric .scad is KBs.


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

    def __init__(self, params, name, message) -> None:
        self.params = params
        self.name = name
        self.message = message


class DesignLoopHandle:
    """Minimal duck-type for ``app.state.run_design_loop`` results.

    The injected loop returns an object with ``.status`` (``"pass"`` |
    ``"exhausted"``) and ``.best`` (the best candidate). Tests inject a
    stub with the same shape (see the finalize contract tests).
    """

    def __init__(self, status: str, best: Any) -> None:
        self.status = status
        self.best = best


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
        (``diff_count`` = params that changed vs the parent; 0 for the first
        version)."""
        svc = _service(request)
        _project_or_404(svc, project_id)
        out: list[dict[str, Any]] = []
        prev: dict[str, Any] | None = None
        for v in svc.list_versions(project_id):
            entry = svc._version_public(v)
            if prev is None:
                entry["diff_count"] = 0
            else:
                added, removed, changed = versions_mod.diff_params(
                    prev["params"], v["params"]
                )
                entry["diff_count"] = len(added) + len(removed) + len(changed)
            out.append(entry)
            prev = v
        return out

    # -- compare (the prioritized surface) --------------------------------------
    # NOTE: registered BEFORE the numeric ``{version_id}`` routes so
    # Starlette's registration-order matching sees "compare" as a literal
    # (it would otherwise be parsed as a version id → 422).

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
        out = []
        for row in svc.conn.list_projects():
            latest = svc.latest_version(row["id"])
            card = {
                "id": row["id"],
                "name": row["name"],
                "tags": row["tags"],
                "notes": row["notes"],
                "current_version": row.get("current_version"),
                "last_activity": row.get("last_activity"),
                "thumbnail": (latest or {}).get("thumbnail"),
            }
            # Git invisibility: mask the repo path like the projects
            # router does (the library card shape doesn't need it, but if a
            # future field adds it, it must be masked).
            out.append(card)
        return out

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
        committed — the source is versioned content, not a cache)."""
        svc = _service(request)
        row = _project_or_404(svc, project_id)

        if "application/json" not in request.headers.get("content-type", ""):
            raise HTTPException(status_code=400, detail="expected JSON body")
        content = await _read_bounded_source(request)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            raise HTTPException(status_code=400, detail="invalid JSON body")
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, str):
            raise HTTPException(status_code=422, detail="'source' must be a string")

        path = _design_source_path(row)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(source, encoding="utf-8")
        try:
            from d33d.projects import commit_all

            commit_all(Path(row["git_repo_path"]), "design source update")
        except RuntimeError as e:
            raise HTTPException(status_code=500, detail=f"git commit failed: {e}")
        return {"stored": str(path), "length": len(source)}

    # -- FINALIZE (the version-creation boundary) ------------------------------

    @router.post("/api/projects/{project_id}/finalize", status_code=201)
    async def finalize(request: Request, project_id: int) -> dict[str, Any]:
        """Run the injected design loop; a ``pass`` result versions the best
        candidate's parameters; any other status is 422 (no spurious
        version). ``params`` defaults to the current version's snapshot."""
        svc = _service(request)
        _project_or_404(svc, project_id)
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

        result = run_loop()
        if inspect.isawaitable(result):
            result = await result
        if result.status != "pass":
            raise HTTPException(
                status_code=422,
                detail=f"design loop did not pass validation: {result.status}",
            )

        # On a pass, the version's params are the best candidate's named
        # parameters (the loop's named-param gate verified them) — the full
        # snapshot of the accepted design. The body's params only seed
        # up front; the loop's result is authoritative.
        named = getattr(result.best, "params", None)
        if isinstance(named, dict) and named:
            params = dict(named)
        else:
            # No named parameters on the candidate (or empty) — fall back
            # to the seed (body params or latest-version snapshot); an
            # empty fallback means the loop's pass produced no usable
            # parameter set (a contract violation by the loop).
            if not params:
                raise HTTPException(
                    status_code=502,
                    detail="design loop passed but produced no parameter set",
                )

        try:
            v = await svc.create_version(
                project_id,
                params,
                name=body.name,
                message=body.message or "design finalize",
            )
        except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
            _raise_mapped(e)
        return svc._version_public(v)

    return router


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _raise_mapped(e: Exception) -> None:
    """Map the service's exception taxonomy to HTTP status codes."""
    if isinstance(e, LookupError):
        raise HTTPException(status_code=404, detail=str(e))
    if isinstance(e, ValueError):
        raise HTTPException(status_code=422, detail=str(e))
    if isinstance(e, versions_mod.VersionConflictError):
        raise HTTPException(status_code=409, detail=str(e))
    raise e


async def _call(fn, *args: Any) -> Any:
    """Call a service method, mapping its exception taxonomy to HTTP."""
    try:
        result = fn(*args)
        if hasattr(result, "__await__"):
            result = await result
        return result
    except (LookupError, ValueError, versions_mod.VersionConflictError) as e:
        _raise_mapped(e)
        raise  # unreachable — _raise_mapped always raises


async def _read_bounded_source(request: Request) -> str:
    """Read the request body bounded (same discipline as the catalogue PUT)."""
    declared = request.headers.get("content-length")
    try:
        declared_len = int(declared) if declared is not None else None
    except ValueError:
        declared_len = None
    if declared_len is not None and declared_len > MAX_SCAD_SOURCE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit",
        )
    parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > MAX_SCAD_SOURCE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"body exceeds {MAX_SCAD_SOURCE_BYTES} byte limit",
            )
        parts.append(chunk)
    return b"".join(parts).decode("utf-8")


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
    return FinalizeBody(params=params, name=name, message=message)


__all__ = [
    "MAX_SCAD_SOURCE_BYTES",
    "create_versions_router",
]
