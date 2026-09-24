"""FastAPI application shell (issue #23, workstream task-a).

This is the HTTP shell for the d33d backend: the app factory, the stub SPA
page served at ``/`` (a stand-in until issue #6's real SPA lands), the
model-config ``GET/PUT /api/config/models`` endpoint (thin editor over the
YAML catalogue — YAML is the source of truth, hot-reloaded on save with
last-known-good on bad YAML), and the credentials endpoints
(``GET/POST /api/settings/credentials``) that wrap :class:`CredentialStore`.

Wiring, not re-implementation: the app consumes the already-merged internals
as-is — ``d33d.db.connect`` (single shared ``Connection``, WAL single-writer),
``d33d.config.catalogue`` (``ModelCatalogueLoader`` / ``hot_reload``) and
``d33d.config.resolve.resolve_model``, and ``d33d.security.credentials``
(``CredentialStore`` with its names-only list boundary). It never re-parses
the catalogue itself, never encrypts keys, and never re-implements the
DB schema.

Security invariants (inherited from the internals, re-asserted at the HTTP
boundary): no raw key material — plaintext, ciphertext, or even the column
name — appears in any response body. ``CredentialStore.get_key`` is
server-internal and is not wired to any route.

Project CRUD, photo upload, and the SSE endpoint (``d33d/projects.py`` /
``d33d/streaming.py``) are sibling workstreams: this factory mounts their
routers (``create_projects_router`` / ``create_streaming_router``) on the
app it builds, and ``app.state.event_sources`` (the dict the SSE endpoint
reads) is initialised empty at build time. The HTTP endpoints themselves
are out of scope for this module.

Also defined here: ``POST /api/projects/{id}/region-edits`` (issue #7,
workstream task-c; wired to the design loop by issue #68) — the
region-scoped edit request route. It validates and accepts a
point-pick payload (optional module identifiers, marked PNG, single
picked point, view id, instruction) and returns 202 Accepted with a
``status: "accepted"`` body — mirroring ``/chat`` — driving the
injected design loop (``d33d/design_loop.py``) in the background. The
version, when the loop passes, arrives only via the SSE stream's
``version-created`` progress frame (never in the 202 body).

Also defined here: ``POST /api/projects/{id}/module-registry`` (issue #7,
workstream task-a) — the named-module registry route that IS the wiring
path ``region-edits``' ``module_ids`` and ``ModelViewer.tsx``'s
``resolvePointPick`` are BUILT to consume. Given ``.scad`` source, it
calls ``d33d.module_registry.build_registry_glb`` (injected via
``app.state.build_registry_glb`` so tests never spawn Docker) and returns
the assembled named GLB as ``model/gltf-binary`` — exactly the shape
``ModelViewer.loadGLB`` parses, with ``mesh.name`` on each node set to
the registry's per-call-site name. The route itself is real plumbing over
a real capability, not another honest stub: the registry module actually
runs N isolated openscad renders and returns real named geometry.

NOT yet wired end-to-end, however: the SPA shell (``web/src/App.tsx``)
has no source of ``scad_source`` to call this route with — the design-
loop-to-SSE-to-model pipeline that would produce OpenSCAD source in the
browser is issue #23's own documented future-ticket deferral (``App.tsx``
mounts ``ModelViewer`` with ``data={null}`` for exactly this reason), so
no frontend code calls ``POST .../module-registry`` yet. That is a
separate, already-tracked gap, not something this route's own
correctness depends on.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import json
import logging
import math
import os
import re
import subprocess
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field, field_validator

from d33d import db, slicer
from d33d import print_validation as _print_validation
from d33d import versions as versions_mod
from d33d.config import ModelCatalogueLoader, hot_reload
from d33d.config.catalogue import (
    Catalogue,
    CatalogueError,
    ResolutionError,
    load_catalogue,
)
from d33d.config.probes import CapabilityCache
from d33d.config.resolve import resolve_model
from d33d.design_loop_events import (
    EMPTY_PHOTO_DATA_URI,
    latest_version_stated_dims,
)
from d33d.evals.failure_capture import default_failures_path
from d33d.module_registry import (
    MAX_CALL_SITES,
    RegistryBuildResult,
    TooManyCallSitesError,
    build_registry_glb,
)
from d33d.projects import create_projects_router
from d33d.render_worker import render_for_design_loop
from d33d.security import credentials as cred
from d33d.streaming import create_streaming_router
from d33d.versions_routes import create_versions_router

logger = logging.getLogger(__name__)

#: Stub SPA page served at ``/`` until issue #6 ships the real build.
#: Static HTML — not a React build, per the ticket.
#: Hard cap on the ``PUT /api/config/models`` body. A models.yaml catalogue
#: is a few KB in practice; anything bigger is not a legitimate edit and is
#: rejected before it can consume unbounded memory.
MAX_CATALOGUE_BODY_BYTES = 1024 * 1024  # 1 MB

#: ``${ENV_VAR}`` key reference (reused from the catalogue module — the
#: PUT boundary only accepts this form for provider keys).
_ENV_VAR_KEY_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")

#: Every run of non-alphanumeric characters, in a project display name —
#: reduced to a single hyphen when building a download filename.
#: Mirrors ``copy.shell.exportFilename``'s ``/[^a-z0-9]+/g`` (web/src/
#: copy.ts) on the lower-cased name.
_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _export_filename(project_name: str, version: str) -> str:
    """The 3MF download filename for a version (issue #163).

    Mirrors ``copy.shell.exportFilename(project, version)`` in web/src/
    copy.ts EXACTLY — the client slugs the same way when it names the
    downloaded Blob, and the design contract pins "Curtain rod bracket" +
    "v4" → ``curtain-rod-bracket-v4.3mf``: lower-case the project name,
    reduce every non-alphanumeric run to a single hyphen, trim leading and
    trailing hyphens, then append ``-<version>.3mf``. A backend test
    reproduces the pinned case against this function so the two
    implementations cannot drift undetected.
    """
    slug = _SLUG_RE.sub("-", project_name.lower()).strip("-")
    return f"{slug}-{version}.3mf"


async def _read_bounded_body(request: Request, cap: int) -> bytes | JSONResponse:
    """Read the request body, bounded to ``cap`` bytes.

    Returns the full body on success, or a 413 ``JSONResponse`` to return
    to the client as-is. Never buffers more than ``cap``+1 bytes: an
    oversized declared Content-Length is rejected after a bounded drain,
    and an oversized streamed body aborts mid-stream. An absent or
    non-numeric Content-Length is defaulted to the cap itself so the
    request is rejected up front rather than relying solely on the
    mid-stream abort (a request with no declared length would otherwise
    fall straight through to the streaming path).
    """
    declared = request.headers.get("content-length")
    try:
        declared_len = int(declared) if declared is not None else None
    except ValueError:
        declared_len = None
    if declared_len is None:
        declared_len = cap

    if declared_len > cap:
        # Reject early; still drain what the client sends — chunk by
        # chunk, never via ``await request.body()`` (which would buffer
        # the entire remainder in memory) — so the connection stays
        # usable without an unbounded buffer.
        async for _ in request.stream():
            pass
        return JSONResponse(
            status_code=413,
            content={"error": f"body exceeds {cap} byte limit"},
        )

    body_parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            # Bounded drain of the remainder (see the header-reject path
            # above): read-and-discard, never full-body buffering.
            async for _ in request.stream():
                pass
            return JSONResponse(
                status_code=413,
                content={"error": f"body exceeds {cap} byte limit"},
            )
        body_parts.append(chunk)
    return b"".join(body_parts)


def _validate_and_install_candidate(
    loader: ModelCatalogueLoader, body: bytes, app: FastAPI
) -> JSONResponse:
    """Write the candidate catalogue to a unique temp file, validate it
    semantically, and — only on success — ``os.replace`` it onto the real
    path and ``hot_reload`` the in-memory catalogue.

    A semantically broken catalogue can never reach disk at
    ``loader.path``; on any failure the temp file is removed and a 400
    (validation) or 500 (I/O) ``JSONResponse`` is returned.
    """
    try:
        loader.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_fd, tmp_name = tempfile.mkstemp(
            dir=str(loader.path.parent), suffix=".models.tmp"
        )
    except OSError as e:
        return JSONResponse(
            status_code=500,
            content={"error": f"could not prepare catalogue write: {e}"},
        )
    tmp_path = Path(tmp_name)
    try:
        os.write(tmp_fd, body)
        os.close(tmp_fd)
        try:
            load_catalogue(tmp_path)
        except CatalogueError as e:
            return JSONResponse(
                status_code=400,
                content={"error": f"catalogue error: {e.message}"},
            )
        # Candidate validated → swap it onto the real path and reload
        # (the reload re-validates the same file and must now succeed).
        os.replace(tmp_path, loader.path)
        tmp_path = None
        try:
            new_cat, _ = hot_reload(loader)
        except CatalogueError as e:
            # Unexpected after a successful replace, but the loader is
            # authoritative: surface as a 500 without touching state.
            return JSONResponse(
                status_code=500,
                content={"error": f"catalogue reload failed: {e.message}"},
            )
        except Exception as e:  # noqa: BLE001  # intentional catch-all (issue #25): the split-state 500 must be explicit for ANY non-CatalogueError
            # Disk/memory split state: ``os.replace`` already swapped the
            # new catalogue onto disk, but the in-memory swap did not
            # happen — ``app.state.catalogue`` still holds the old one
            # until the next successful reload or restart. Do NOT roll
            # back the on-disk write (it is valid); a retry of the same
            # PUT recovers the split state.
            return JSONResponse(
                status_code=500,
                content={
                    "error": (
                        f"catalogue write completed but in-memory reload "
                        f"failed ({e.__class__.__name__}: {e}); disk now "
                        f"holds the new catalogue but app state still holds "
                        f"the old one — retry the PUT or restart to recover"
                    )
                },
            )
        app.state.catalogue = new_cat
        return JSONResponse(content=_serialise_catalogue(new_cat))
    finally:
        if tmp_path is not None and tmp_path.exists():
            tmp_path.unlink(missing_ok=True)


def _find_literal_provider_keys(doc: dict[str, Any]) -> list[str]:
    """Names of providers whose ``key`` is a literal (not ``${ENV_VAR}``).

    HTTP-boundary check only: a LAN-reachable ``PUT /api/config/models``
    must never persist a plaintext API key into ``models.yaml`` — keys are
    stored Fernet-encrypted in ``provider_credentials`` and the catalogue
    references environment variables. The catalogue module's own loader
    still accepts literals when loading a trusted local file.
    """
    bad: list[str] = []
    providers = doc.get("providers")
    if not isinstance(providers, dict):
        return bad
    for name, spec in providers.items():
        key = spec.get("key") if isinstance(spec, dict) else None
        if key is not None and (
            not isinstance(key, str) or not _ENV_VAR_KEY_RE.match(key)
        ):
            bad.append(str(name))
    return bad


#: Vite's build output directory (``web/dist``), relative to the repo
#: root. Mounted at ``/`` when it exists (production/after ``npm run
#: build``); falls back to :data:`STUB_HTML` when it does not (CI, tests,
#: or a fresh checkout before the SPA has been built) so app startup never
#: crashes on a missing directory.
_SPA_DIST_DIR: Path = Path(__file__).resolve().parent.parent / "web" / "dist"

STUB_HTML: str = """<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>d33d — parametric 3D design</title>
  </head>
  <body>
    <h1>d33d</h1>
    <p>Backend online. The SPA lands in issue #6.</p>
  </body>
</html>
"""


def _default_master_key_path(data_dir: Path) -> Path:
    """Path for the Fernet ``MASTER_KEY`` file under ``data_dir``.

    Created ``0o600`` by ``get_or_create_master_key`` — the file is the
    volume-persistence boundary.
    """
    return data_dir / "master.key"


def _default_catalogue_path(data_dir: Path) -> Path:
    """Path for the model-catalogue YAML under ``data_dir``.

    ``data_dir/models.yaml`` is the source of truth the UI is a thin
    editor over (issue #3 spec). The file is created on first write; the
    app starts with an empty in-memory catalogue and serves it until the
    operator saves their first config via ``PUT /api/config/models``.
    """
    return data_dir / "models.yaml"


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Wire the shared DB + credential store on app startup.

    Order matters (per the ticket): ``db.connect()`` first — the
    ``provider_credentials`` table must exist before ``CredentialStore``
    points at the same connection (pointing it at a bare DB without the
    schema raises ``sqlite3.OperationalError``).
    """
    state = app.state
    if state.conn is None:
        state.conn = db.connect(state.db_path)
        versions_mod.migrate(state.conn)
        state.versions = versions_mod.VersionService(state.conn)
    if state.master_key is None:
        state.master_key = cred.get_or_create_master_key(state.master_key_path)
    if state.credential_store is None:
        state.credential_store = cred.CredentialStore(state.conn, state.master_key)
    yield
    if state.conn is not None:
        state.conn.close()


def _serialise_catalogue(cat: Catalogue) -> dict[str, Any]:
    """Serialise a :class:`Catalogue` for the ``GET /api/config/models``
    response. Provider ``key`` is REDACTED explicitly (never the raw
    material, never the ciphertext — the in-memory value is a resolved
    plaintext key that must not surface).
    """
    providers: dict[str, Any] = {
        name: {
            "name": p.name,
            "base": p.base,
            "key": "***redacted***",
            "defaults": dict(p.defaults),
        }
        for name, p in cat.providers.items()
    }
    models = [
        {
            "id": e.id,
            "provider": e.provider,
            "model": e.model,
            "context_window": e.context_window,
            "params": dict(e.params),
            "fallbacks": list(e.fallbacks),
            "retries": dict(e.retries),
        }
        for e in cat.models.values()
    ]
    return {
        "source": str(cat.source),
        "providers": providers,
        "models": models,
        "roles": dict(cat.roles),
    }


# ---------------------------------------------------------------------------
# Named module registry (issue #7, workstream task-a)
# ---------------------------------------------------------------------------


class ModuleRegistryRequest(BaseModel):
    """Body of ``POST /api/projects/{id}/module-registry``.

    ``scad_source`` is the parametric OpenSCAD the design loop produced
    for the project (see ``d33d.design_loop``) — the SAME source the
    single-render contract already renders. This route does not persist
    ``scad_source``; it is a pure function of the given source in, named
    GLB out.

    ``max_length`` bounds the raw source the same way
    ``MAX_CATALOGUE_BODY_BYTES``/``MAX_REGION_EDIT_IMAGE_BYTES`` bound
    the other body-accepting routes in this file — without it, a small
    request body built from a repeated call pattern can still enumerate
    far more than ``MAX_CALL_SITES`` call-sites (checked separately, see
    ``d33d.module_registry.build_registry_glb``), so this is
    defense-in-depth rather than the sole guard against that.
    """

    scad_source: str = Field(min_length=1, max_length=1024 * 1024)


# ---------------------------------------------------------------------------
# Region-scoped edit request (issue #7, workstream task-c; design-loop
# wiring by issue #68)
# ---------------------------------------------------------------------------
#
# The route accepts and validates a region-pick payload (optional
# module identifiers + marked PNG + single picked point + view id) and drives
# the injected design loop (``app.state.run_design_loop``) in the
# background — the same adapter pattern as ``POST /{id}/chat``
# (``d33d/projects.py`` / ``d33d/design_loop_events.py``). The 202 body
# mirrors ``/chat`` (``{project_id, status: "accepted"}``); the version
# arrives only via the SSE stream's ``version-created`` frame.

#: The six orthographic render-worker views a point pick may be made on
#: (matches ``ViewId`` in ``web/src/components/canvas/
#: DimensionCanvas.tsx``).
REGION_EDIT_VIEW_IDS: frozenset[str] = frozenset(
    {"front", "back", "left", "right", "top", "iso"}
)

#: Hard cap on the marked-PNG body carried in a region-edit request. The
#: composited marked image is a single 800x800 (or viewport-sized) PNG,
#: base64-encoded — a few hundred KB in practice. This bounds the request
#: the same way ``MAX_CATALOGUE_BODY_BYTES`` bounds the catalogue PUT.
MAX_REGION_EDIT_IMAGE_BYTES = 5 * 1024 * 1024  # 5 MB (base64-decoded size)

#: Set-of-Mark cap (spec: "small open models confuse more IDs than
#: that"). A ranked module-identifier list longer than this is rejected
#: rather than silently truncated, so an over-long selection is surfaced
#: to the caller instead of quietly losing ranked entries.
MAX_REGION_EDIT_MODULE_IDS = 10


class PointLocation(BaseModel):
    """The single picked point, in the view's CSS-pixel coordinate space.

    Carried for audit/debugging and for the containment gate: the
    authoritative grounding is the marked PNG (the red dot composited at
    exactly this location), so the server does not interpret the
    coordinate space beyond accepting finite pixel values.

    Finiteness is a wire-format requirement: ``x``/``y`` must be finite
    floats (NaN/±inf are rejected — NaN is not even valid JSON), because
    a non-finite coordinate reaching the containment gate would be a
    defect the server cannot diagnose downstream. BOUNDS are deliberately
    NOT validated: the server does not know the client's view dimensions,
    and negative coordinates are legal (a client may use a different
    origin) — validating a range would fabricate a constraint the client
    alone can justify.
    """

    x: float
    y: float

    @field_validator("x", "y")
    @classmethod
    def _coords_must_be_finite(cls, v: float) -> float:
        if not math.isfinite(v):
            raise ValueError("point coordinates must be finite numbers")
        return v


class RegionEditRequest(BaseModel):
    """Body of ``POST /api/projects/{id}/region-edits``.

    Carries everything the scoped-edit regeneration needs: the optional
    module-identifier list the client-side point pick resolved via
    ``ModelViewer.resolvePointPick`` (supplementary context — the pick
    resolves by raycast against the live scene, so an unnamed streamed STL
    legitimately yields an EMPTY list), the composited red-marked PNG the
    vision model sees (the marked-up render of the current model — the
    authoritative grounding), the single picked point (audit/debugging and
    the containment gate), the view id it was drawn on, and the user's
    free-text edit instruction.
    """

    module_ids: list[str] = Field(default_factory=list, max_length=MAX_REGION_EDIT_MODULE_IDS)
    view_id: str
    marked_png_base64: str = Field(min_length=1)
    point: PointLocation
    instruction: str = Field(min_length=1)

    @field_validator("view_id")
    @classmethod
    def _view_id_must_be_known(cls, v: str) -> str:
        if v not in REGION_EDIT_VIEW_IDS:
            raise ValueError(
                f"view_id must be one of {sorted(REGION_EDIT_VIEW_IDS)}, got {v!r}"
            )
        return v

    @field_validator("module_ids")
    @classmethod
    def _module_ids_non_empty_strings(cls, v: list[str]) -> list[str]:
        if any(not isinstance(m, str) or not m for m in v):
            raise ValueError("module_ids must be non-empty strings")
        return v


def create_app(
    db_path: str | Path = ":memory:",
    *,
    master_key_path: str | Path | None = None,
    catalogue_path: str | Path | None = None,
    spa_dist_dir: str | Path | None = None,
) -> FastAPI:
    """Build the FastAPI app.

    Parameters:
      ``db_path``: SQLite path (``":memory:"`` for tests). Single shared
        ``Connection`` — WAL single-writer; never a second pool.
      ``master_key_path``: where the Fernet ``MASTER_KEY`` is persisted.
        Defaults to ``<data_dir>/master.key`` where ``data_dir`` is the
        directory of ``db_path`` (or ``tmp_path``-style parent when
        ``db_path`` is ``":memory:"`` — callers should pass an explicit
        path in that case).
      ``catalogue_path``: the ``models.yaml`` file the app serves/edits.
        Defaults to ``<data_dir>/models.yaml``. Created on first save; the
        app starts with an empty in-memory catalogue.
      ``spa_dist_dir``: directory containing the built SPA (Vite's
        ``web/dist`` output: ``index.html`` + hashed assets). Defaults to
        ``<repo root>/web/dist``. When the directory does not exist (CI,
        tests, or a fresh checkout before ``npm run build`` has run), the
        app falls back to :data:`STUB_HTML` at ``/`` instead of mounting
        static files — startup never crashes on a missing dist dir.
    """
    db_path_p = Path(db_path)
    data_dir = (
        db_path_p.parent if db_path_p.parent not in (Path(""), Path(".")) else Path(".")
    )
    state_master_key_path = (
        Path(master_key_path)
        if master_key_path is not None
        else _default_master_key_path(data_dir)
    )
    state_catalogue_path = (
        Path(catalogue_path)
        if catalogue_path is not None
        else _default_catalogue_path(data_dir)
    )
    state_spa_dist_dir = (
        Path(spa_dist_dir) if spa_dist_dir is not None else _SPA_DIST_DIR
    )

    app = FastAPI(lifespan=_lifespan)

    # Shared singletons — set on ``app.state`` so sibling workstreams
    # (projects.py / streaming.py) can reach the same objects.
    app.state.db_path = db_path
    app.state.master_key_path = state_master_key_path
    app.state.catalogue_path = state_catalogue_path
    app.state.catalogue_loader = ModelCatalogueLoader(state_catalogue_path)
    app.state.catalogue: Catalogue | None = None
    app.state.conn: db.Connection | None = None
    app.state.master_key: bytes | None = None
    app.state.credential_store: cred.CredentialStore | None = None
    # Version service (issue #8) — the single writer of the versions table
    # + per-project version commits. Set by the lifespan; declared here so
    # ``create_app()`` alone leaves the attribute present for routes/tests.
    app.state.versions: versions_mod.VersionService | None = None
    # The SSE endpoint (streaming.py) reads this dict at request time; it
    # maps ``project_id -> AsyncIterator[(event, data)]`` and starts empty
    # (no active streams until a future ticket wires the design loop).
    app.state.event_sources: dict[int, AsyncIterator[tuple[str, dict[str, Any]]]] = {}
    # The named-module registry builder (issue #7) — injected so tests
    # never spawn Docker; production wiring is the real
    # ``d33d.module_registry.build_registry_glb`` (default below).
    app.state.build_registry_glb = build_registry_glb
    # The design-loop runner for FINALIZE (issue #8/#9) — injected (same
    # seam as build_registry_glb) so tests wire a stub loop. Production
    # wires the REAL d33d.design_loop.run_design_loop (resolved per call
    # from the live catalogue + render worker) wrapped in the
    # failures.jsonl hook: an exhausted loop appends one line to
    # ``app.state.failures_jsonl_path`` (issue #9 — production gate
    # failures auto-archived). The hook fires only on an EXHAUSTED loop
    # (a pass appends nothing), and the eval harness's assert path never
    # calls this closure (structural exclusion — see
    # d33d/evals/failure_capture.py). The wrapper is lazily built on first
    # call so an empty catalogue at startup is not a wiring error.
    app.state.run_design_loop = _build_production_design_loop()
    # The stage-2 question-answer call (issue #249): one cheap single LLM
    # completion, resolved through the SAME live catalogue as the design
    # role (the model is configured, never hardcoded). The question
    # pre-route (``d33d.projects.post_chat`` →
    # ``d33d.question_answer.route_chat_message``) awaits this under its
    # own 10 s hard timeout; any failure degrades to the design loop
    # exactly as today. A catalogue without a ``question`` role (a
    # pre-#249 models.yaml) degrades the pre-route to the design loop
    # with zero added failure modes (the role is ADDITIVE — no new
    # catalogue entry is required, and none is hard-coded here).
    # The stage-2 edge is app-scoped (it reads ``app.state`` for the
    # capability cache and the ``request_logs`` connection): the state
    # object is bound at build time (the ``app`` instance is the same
    # object for the app's lifetime — ``app.state`` is its attribute),
    # so the route's ``AnswerEdge`` seam is the 2-arg
    # ``async (question, entries)`` shape the closure already has.
    app.state.answer_question = _build_question_answer_call(
        state_catalogue_path, app
    )
    # The question-role capability probe cache (issue #249 review): keyed
    # by (base_url, model id hash) — the stage-2 probe (2-3 real LLM
    # requests) runs once per model pair, not per question. A models.yaml
    # hot-reload that changes the question role's model or base re-probes
    # exactly once (a new key), like the design role's probe semantics.
    app.state.question_capability_cache = CapabilityCache()
    # The failures.jsonl path the production design-loop hook appends to
    # (issue #9, workstream task-failures). Defaults to the repo-relative
    # ``evals/failures.jsonl``; overridable via env var for tests / runs
    # that want a different sink. The hook fires only on an EXHAUSTED
    # loop (a pass appends nothing), and the eval harness's assert path
    # never calls this hook (structural exclusion — see
    # d33d/evals/failure_capture.py).
    app.state.failures_jsonl_path = Path(
        os.environ.get("D33D_FAILURES_JSONL") or default_failures_path()
    )

    spa_index = state_spa_dist_dir / "index.html"
    serve_spa_build = state_spa_dist_dir.is_dir() and spa_index.is_file()

    if not serve_spa_build:

        @app.get("/", response_class=HTMLResponse)
        async def stub_page() -> HTMLResponse:
            """Stub SPA page — served when ``web/dist`` has not been built
            yet (CI, tests, fresh checkout). Static HTML fallback."""
            return HTMLResponse(content=STUB_HTML)

    @app.get("/api/config/models")
    async def get_models() -> dict[str, Any]:
        """Current model catalogue (providers redacted, roles, models)."""
        cat = app.state.catalogue
        if cat is None:
            return {
                "source": str(app.state.catalogue_path),
                "providers": {},
                "models": [],
                "roles": {},
            }
        return _serialise_catalogue(cat)

    @app.get("/api/config/envelope")
    async def get_envelope() -> dict[str, Any]:
        """Build envelope for the machine (x/y/z in millimetres) plus a
        ``verified`` flag. The route is a third READER of the named
        constants in ``d33d.print_validation`` — never a copy of the
        numbers; the values are (320.0, 320.0, 300.0) only because the
        constant is.

        The keep-out notch (``QIDI_PLUS_5_KEEP_OUT_MM``) is deliberately
        NOT exposed: it is a separate vendor-verified constraint with its
        own gate-7b branch and its own source-invariant test; consumers
        of this route (W12/W14) need only the envelope.
        """
        x, y, z = _print_validation.QIDI_PLUS_5_ENVELOPE_MM
        return {
            "x": x,
            "y": y,
            "z": z,
            "unit": "mm",
            "verified": _print_validation.QIDI_PLUS_5_ENVELOPE_VERIFIED,
        }

    @app.put("/api/config/models")
    async def put_models(request: Request) -> JSONResponse:
        """Full-YAML replacement of ``catalogue_path``.

        Read-bounded body (413 on a too-large request), then validate the
        catalogue — first the untrusted-input boundary checks (YAML syntax,
        mapping shape, env-var-only provider keys), then a full semantic
        load of the candidate. Only after validation succeeds does the
        candidate ``os.replace`` onto the real path and ``hot_reload`` swap
        the in-memory catalogue. A failure at any stage returns 400 and
        leaves the on-disk file and the live catalogue exactly as they were
        (last-known-good, both on disk and in memory).
        """
        result = await _read_bounded_body(request, MAX_CATALOGUE_BODY_BYTES)
        if isinstance(result, JSONResponse):
            return result
        body = result

        try:
            doc = yaml.safe_load(body.decode("utf-8"))
        except (yaml.YAMLError, UnicodeDecodeError) as e:
            return JSONResponse(
                status_code=400,
                content={"error": f"invalid YAML: {e}"},
            )
        if not isinstance(doc, dict):
            return JSONResponse(
                status_code=400,
                content={"error": "top level must be a mapping"},
            )

        # HTTP-boundary key check: a literal (non-`${ENV}`) provider key
        # would persist plaintext key material into models.yaml, bypassing
        # the Fernet-encrypted provider_credentials storage. Env vars only.
        bad_keys = _find_literal_provider_keys(doc)
        if bad_keys:
            return JSONResponse(
                status_code=400,
                content={
                    "error": (
                        f"provider key(s) for {sorted(bad_keys)} must be an "
                        f'environment variable reference (e.g. "${{SOME_VAR}}")'
                    )
                },
            )

        return _validate_and_install_candidate(app.state.catalogue_loader, body, app)

    # Mount the sibling workstream routers (project CRUD + photo upload,
    # SSE streaming) — the production app is complete from create_app()
    # alone; no extra wiring at the entrypoint.
    app.include_router(create_projects_router())
    app.include_router(create_streaming_router())
    app.include_router(create_versions_router())

    @app.get("/api/settings/credentials")
    async def list_credentials() -> list[dict[str, str]]:
        """Names-only list (``provider_id``, ``model_alias``). No key
        material — the invariant inherited from ``CredentialStore``."""
        store = app.state.credential_store
        assert store is not None  # set by lifespan
        return store.list_credentials()

    @app.post("/api/settings/credentials")
    async def store_credential(request: Request) -> JSONResponse:
        """Store a key via ``CredentialStore.store_key`` (Fernet-encrypted
        under ``MASTER_KEY``). Body: ``{"provider": str, "model_alias": str,
        "secret": str}``. The response echoes only the names — never the
        secret or the ciphertext."""
        body = await request.body()
        try:
            data = json.loads(body)
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return JSONResponse(
                status_code=400, content={"error": f"invalid JSON: {e}"}
            )
        provider = data.get("provider")
        model_alias = data.get("model_alias")
        secret = data.get("secret")
        if not isinstance(provider, str) or not provider:
            return JSONResponse(
                status_code=400,
                content={"error": "'provider' must be a non-empty string"},
            )
        if not isinstance(model_alias, str) or not model_alias:
            return JSONResponse(
                status_code=400,
                content={"error": "'model_alias' must be a non-empty string"},
            )
        if not isinstance(secret, str) or not secret:
            return JSONResponse(
                status_code=400,
                content={"error": "'secret' must be a non-empty string"},
            )
        store = app.state.credential_store
        assert store is not None
        try:
            store.store_key(provider, model_alias, secret)
        except ValueError as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return JSONResponse(
            status_code=201,
            content={"provider_id": provider, "model_alias": model_alias},
        )

    @app.get("/api/config/roles")
    async def resolve_role(role: str) -> JSONResponse:
        """Resolve a role to its concrete model + provider via
        ``resolve_model`` (the stable facade over ``catalogue.resolve``).
        The response never includes the provider key."""
        cat = app.state.catalogue
        if cat is None:
            return JSONResponse(
                status_code=404, content={"error": "no catalogue loaded yet"}
            )
        try:
            res = resolve_model(cat, role)
        except (CatalogueError, ResolutionError, KeyError) as e:
            return JSONResponse(status_code=400, content={"error": str(e)})
        return JSONResponse(
            content={
                "role": res.role,
                "alias": res.entry.id,
                "model": res.entry.model,
                "provider": res.entry.provider,
                "via_fallback": res.via_fallback,
            }
        )

    @app.post("/api/projects/{project_id}/module-registry")
    async def create_module_registry(
        request: Request, project_id: int, body: ModuleRegistryRequest
    ) -> Response:
        """Build the named OpenSCAD module registry for ``scad_source``
        and return it as a GLB — the wiring path ``ModelViewer.loadGLB``
        and ``resolvePointPick`` (``web/src/components/viewer/ModelViewer.tsx``) are built to consume, and the source of the
        ``module_ids`` ``POST /api/projects/{id}/region-edits`` accepts.
        No frontend code calls this route yet — the SPA shell has no
        ``scad_source`` to send it until the design-loop-to-SSE pipeline
        (issue #23's own tracked future ticket) lands; this route and its
        orchestration are independently real and tested regardless.

        Delegates to ``app.state.build_registry_glb`` (the real
        ``d33d.module_registry.build_registry_glb`` in production;
        injectable in tests so no Docker is spawned). Every call-site's
        isolated render is a sequential ``subprocess.run`` round-trip
        (create volume -> populate -> openscad -> harvest -> remove
        volume), so ``build_fn`` itself runs on a worker thread via
        ``asyncio.to_thread`` — called directly, it would block THIS
        process's single event loop for the full multi-call-site Docker
        round-trip, starving every other concurrent request (SSE
        streams, unrelated projects' routes, health checks) for the
        entire duration, not just serialising this one endpoint.

        A ``.scad`` with no top-level module call-sites is a valid, empty
        registry — 200 with ``status: "empty"``, never a fabricated GLB
        or a 500. A PARTIAL registry (some call-sites failed their
        isolated render) still returns the GLB body for every module
        that DID succeed; the failed module names are surfaced in the
        ``X-Module-Registry-Failed`` response header (comma-separated) so
        the caller can show which regions are unavailable without
        discarding the rest of the registry.
        """
        conn: db.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        build_fn = request.app.state.build_registry_glb
        try:
            result: RegistryBuildResult = await asyncio.to_thread(
                build_fn, body.scad_source
            )
        except TooManyCallSitesError as e:
            raise HTTPException(
                status_code=413,
                detail=(
                    f"{e.count} call-sites exceeds the {MAX_CALL_SITES} limit "
                    "per registry build"
                ),
            )
        except (OSError, RuntimeError, subprocess.SubprocessError):
            # Bounded infra-error set around the threaded build: Docker
            # daemon unreachable / docker binary missing (OSError, e.g.
            # FileNotFoundError, and subprocess.SubprocessError), or the
            # RuntimeError _export_scene_isolating_bad_meshes raises when
            # even the per-mesh-isolated scene export fails. None of
            # these are a diagnosable validation failure of the request
            # itself, so they must not escape as a bare unclassified 500
            # (bypassing the project's closed ErrorClass discipline) or
            # leak raw exception text to the client.
            logger.exception(
                "module-registry build failed for project_id=%s "
                "(scad_source length=%d)",
                project_id,
                len(body.scad_source),
            )
            return JSONResponse(
                status_code=502,
                content={
                    "error": "module registry build failed",
                    "error_class": "container_error",
                },
            )

        if result.glb_bytes is None:
            return JSONResponse(
                content={
                    "project_id": project_id,
                    "status": "empty",
                    "registry_names": list(result.registry_names),
                }
            )

        headers: dict[str, str] = {}
        if result.failures:
            headers["X-Module-Registry-Failed"] = ",".join(
                f.site.registry_name for f in result.failures
            )
        return Response(
            content=result.glb_bytes,
            media_type="model/gltf-binary",
            headers=headers,
        )

    @app.get("/api/projects/{project_id}/model.3mf")
    async def download_model_3mf(request: Request, project_id: int) -> Response:
        """Serve the validated 3MF for the project's LATEST version (issue #163).

        The SPA's export button (``ApiClient.downloadModel3MF``) GETs this
        path and consumes the bytes as a Blob — a raw binary download with
        ``model/3mf`` content type and a ``Content-Disposition`` filename
        matching ``copy.shell.exportFilename``'s slug contract (the
        design-contract-pinned "Curtain rod bracket" + "v4" →
        ``curtain-rod-bracket-v4.3mf``), so the client-side and server-side
        slugs cannot drift.

        The route is a THIN HTTP wrapper around
        ``d33d.print_validation.validate_stl`` (ticket #3 — the SOLE 3MF
        producer; the render worker never emits 3MF). The STL to validate
        is the one the version's own render produced, resolved through the
        version row's PERSISTED ``render_artifact_dir`` (issue #163:
        renders land under a per-render uuid, so the link is persisted at
        version creation — never mtime-inferred).

        Re-validates on EVERY request (stateless, matching the rest of the
        design — no cache) and SERIALIZES per project under
        ``VersionService._with_project_lock`` (the existing per-project lock
        pattern) so a second request never streams a half-written
        ``model.3mf``.

        Error cases (each a clear non-2xx naming the cause — never an
        empty, partial or truncated 200; the SPA throws on non-ok so a
        4xx/5xx body never becomes a file):

        - 404 — the project does not exist, or the project has NO VERSIONS;
        - 409 — a design loop is in flight for the project (the latest
          version's render may still be in production; re-validating
          mid-write would read a partial STL), or the latest version has NO
          RECORDED RENDER (pre-#163 row: no render directory was persisted
          — a clear error naming that, never a guess at which directory
          might be its own), or the recorded render directory no longer
          exists on disk (the render artifact was deleted); the in-flight
          and stale-render 409s both carry ``error_class: "conflict"``;
        - 502 — validation ran but a gate failed (no 3MF was produced) or
          the 3MF could not be read back; ``error_class`` is the
          :class:`~d33d.print_validation` gate's closed-enum value.
        """
        conn: db.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        app = request.app
        inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
        if inflight is None:
            inflight = set()
            app.state.design_loop_inflight = inflight
        if project_id in inflight:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "validation in progress: a design loop is in "
                    "flight for this project",
                    "error_class": "conflict",
                },
            )

        versions = app.state.versions
        latest = versions.latest_version(project_id)
        if latest is None:
            return JSONResponse(
                status_code=404,
                content={
                    "error": "the project has no versions yet — nothing to "
                    "validate into a 3MF",
                },
            )

        artifact_dir = latest.get("render_artifact_dir")
        if artifact_dir is None:
            return JSONResponse(
                status_code=409,
                content={
                    "error": "no render is recorded for this version — the "
                    "version predates render recording (issue #163); run the "
                    "design loop to produce one",
                    "error_class": "conflict",
                },
            )
        stl_path = Path(artifact_dir) / "model.stl"
        if not stl_path.is_file():
            return JSONResponse(
                status_code=409,
                content={
                    "error": "the render recorded for this version no longer "
                    "exists on disk — the model cannot be validated",
                    "error_class": "conflict",
                },
            )

        # ``stated_mm`` ABSTAINS, it does not compare against zero: the
        # latest version's confirmed W/D/H via ``latest_version_stated_dims``
        # — a fully-positive triple, or ``None`` when any axis is missing
        # from (or <= 0 in) that version's persisted ``stated_dims`` column
        # (issue #247 — the helper no longer reads W/D/H param keys), so
        # ``validate_stl``'s dimension gate skips rather than measuring a
        # perfectly good model against 0.0 and failing it (the #91 bug). A bbox-gate abstention is still ``ok=True``
        # (issue #91); the route carries no metadata claiming otherwise —
        # a 3MF served under an abstention is a valid millimetre artefact.
        stated = latest_version_stated_dims(versions, project_id)

        # The 3MF is written to the render's OWN durable directory (next to
        # its model.stl) — re-validate per request, never a cache. The
        # per-project lock serializes every 3MF write for this project so
        # a concurrent GET never observes a half-written file. The
        # PRODUCTION slicer hook (``slicer.slice_dry_run``) is injected —
        # the default fails closed where no slicer binary exists, which is
        # the correct production behaviour (gate 6 reports the cause).
        def _validate() -> _print_validation.ValidationResult:
            return _print_validation.validate_stl(
                str(stl_path),
                stated_mm=stated,
                output_dir=str(Path(artifact_dir)),
                slice_dry_run_fn=slicer.slice_dry_run,
            )

        result = await versions._with_project_lock(project_id, _validate)
        if not result.ok or not result.export_3mf:
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"validation failed — no 3MF produced: "
                    f"{result.message or 'unknown gate failure'}",
                    "error_class": result.error_class or "unknown",
                },
            )

        out_path = Path(result.export_3mf)
        try:
            data = out_path.read_bytes()
        except OSError as e:
            return JSONResponse(
                status_code=502,
                content={
                    "error": f"the 3MF could not be read back after "
                    f"validation: {e}",
                    "error_class": "export_error",
                },
            )
        if not data:
            # A zero-byte 3MF is a truncated artefact — serving it would
            # hand the client a file that cannot be opened. Fail, never
            # stream empty bytes as a 200.
            return JSONResponse(
                status_code=502,
                content={
                    "error": "validation produced an empty 3MF — no bytes to "
                    "serve",
                    "error_class": "export_error",
                },
            )

        # The filename mirrors copy.shell.exportFilename (web/src/copy.ts):
        # project name lower-cased, every non-alphanumeric RUN reduced to a
        # single hyphen, leading/trailing hyphens trimmed, then the version
        # id suffixed ``vN`` + ``.3mf``. Pinned by a backend test ("Curtain
        # rod bracket" + "v4" → "curtain-rod-bracket-v4.3mf") so the two
        # implementations cannot drift undetected.
        filename = _export_filename(row["name"], f"v{latest['id']}")
        return Response(
            content=data,
            media_type="model/3mf",
            headers={
                "Content-Disposition": f'attachment; filename="{filename}"',
            },
        )

    @app.post("/api/projects/{project_id}/region-edits", status_code=202)
    async def create_region_edit(
        request: Request, project_id: int, body: RegionEditRequest
    ) -> JSONResponse:
        """Accept a region-scoped edit request and drive the design loop.

        Validates the payload (marked PNG, picked point, view id,
        instruction) against a real project, checks the
        per-project in-flight flag (409), registers the design-loop event
        source SYNCHRONOUSLY (so the SSE stream does not terminate on
        "no active stream"), and returns 202 Accepted with a
        ``status: "accepted"`` body — mirroring ``POST /{id}/chat``. The
        new version (if the loop passes) arrives only via the SSE stream's
        ``version-created`` progress frame; an exhausted loop produces no
        version and a terminal error frame.

        The design loop is invoked with:

        - ``photo`` = the marked PNG from the body as a data URI (the
          vision model sees the marked-up render of the current model, not
          the stored reference photo); the fixed transparent-PNG constant
          is the fallback only if the (schema-required) field were ever
          absent.
        - ``stated_dims`` = always ``None`` — a region edit carries NO
          dimension statement, so the gate ABSTAINS entirely, recorded
          distinctly as ``Score.bbox_abstained`` (ticket #91 / issue
          #247). There is deliberately no persisted fallback and no
          client override: the gate enforces only the axes the current
          run's input confirmed, and a region edit confirms nothing.
        - ``chat_history`` = the empty tuple — a region edit is a scoped
          directive, not a chat turn.
        - ``request`` = the instruction prefixed with the ``view_id`` and,
          when the pick resolved named modules, with those ``module_ids``
          (``module_ids`` may be EMPTY — a streamed unnamed STL still
          selects fine, grounded by the marked PNG alone). The composed
          string is always non-empty — the failures.jsonl hook's
          ``FailureEvent.request`` requires it.
        """
        conn: db.Connection = request.app.state.conn
        row = conn.get_project(project_id)
        if row is None:
            raise HTTPException(status_code=404, detail="project not found")

        try:
            image_bytes = base64.b64decode(body.marked_png_base64, validate=True)
        except (binascii.Error, ValueError) as e:
            raise HTTPException(
                status_code=400, detail=f"marked_png_base64 is not valid base64: {e}"
            )
        if len(image_bytes) > MAX_REGION_EDIT_IMAGE_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"marked PNG exceeds {MAX_REGION_EDIT_IMAGE_BYTES} byte limit",
            )

        app = request.app
        inflight: set[int] = getattr(app.state, "design_loop_inflight", None)
        if inflight is None:
            inflight = set()
            app.state.design_loop_inflight = inflight
        if project_id in inflight:
            raise HTTPException(
                status_code=409, detail="a design loop is already in flight"
            )

        # Capture photo + stated_dims SYNCHRONOUSLY before the 202
        # response — the loop runs in the background and the DB may be
        # closed by the time it starts (same contract as post_chat).
        # Photo: the marked PNG from the body (schema-required, so the
        # fallback only fires on a future schema change).
        photo = (
            f"data:image/png;base64,{body.marked_png_base64}"
            if image_bytes
            else EMPTY_PHOTO_DATA_URI
        )
        # Stated dims: a region edit carries NO dimension statement, so
        # the gate ABSTAINS (``None``). Pre-#247 the route passed a zero
        # triple; #247 replaced it with the abstaining ``None`` (the gate
        # enforces only the axes the current run's input confirmed, and a
        # region edit confirms nothing — no persisted fallback, issue
        # #247's operator decision). The gate measures rather than
        # fabricates, and an unmeasurable gate must not hard-fail every
        # candidate.
        stated_dims: tuple[float, float, float] | None = None

        # The composed request text: the instruction prefixed with the view
        # id, and with the resolved module_ids only when the pick resolved
        # named modules (a streamed unnamed STL sends an empty list — the
        # marked point alone is the grounding). The failures.jsonl hook
        # records this; the version message is its 200-char prefix.
        if body.module_ids:
            request_text = (
                f"Region edit on modules {', '.join(body.module_ids)} "
                f"at the marked point (view: {body.view_id}): {body.instruction}"
            )
        else:
            request_text = (
                f"Region edit at the marked point (view: {body.view_id}): "
                f"{body.instruction}"
            )

        from d33d.design_loop_events import run_design_loop_with_events

        events = run_design_loop_with_events(
            app,
            project_id,
            user_message=request_text,
            stated_dims=stated_dims,
            chat_history=(),
            photo=photo,
            request_text=request_text,
        )
        # Register the event source SYNCHRONOUSLY before the 202 response
        # (else the client's GET /api/stream/{id} sees no active source).
        # The SSE endpoint is the sole driver of the generator; the
        # in-flight flag (set here, cleared in the SSE endpoint's finally)
        # prevents a second concurrent drive.
        app.state.event_sources[project_id] = events
        inflight.add(project_id)

        return JSONResponse(
            status_code=202,
            content={"project_id": project_id, "status": "accepted"},
        )

    # Static SPA serving — mounted LAST, at the root path. All ``/api/*``
    # routers are registered above; Starlette's Router matches routes in
    # registration order and returns on the first full match, so this
    # catch-all mount can never shadow an already-registered API route. It
    # only serves paths none of the API routers claimed (verified: see
    # tests/test_spa_static.py). ``html=True`` makes ``StaticFiles`` serve
    # ``index.html`` for ``/`` and any unmatched sub-path, which is the SPA
    # client-side-routing fallback.
    if serve_spa_build:
        app.mount(
            "/",
            StaticFiles(directory=state_spa_dist_dir, html=True),
            name="spa",
        )

    return app


def _http_request_factory(
    base_url: str, api_key: str, timeout: float | None = None
):
    """The production HTTP edge for one provider endpoint.

    ``request(body) -> response`` (httpx-shaped: ``.ok`` / ``.json()``);
    the key stays in the ``Authorization: Bearer`` header, never in a
    request body or message (the key never reaches the browser).

    ``timeout`` (seconds) is the PER-REQUEST bound for the httpx client.
    The design-loop closure uses the default 120 s (a render-loop LLM
    call can legitimately be slow); the stage-2 question-answer closure
    uses a short per-request bound (issue #249's latency decision — the
    10 s operator bound must cover the catalogue load + capability probe
    + the completion itself, not just the completion). ``None`` keeps
    the historical 120 s."""
    import httpx

    effective_timeout = 120.0 if timeout is None else timeout

    async def _factory(body: dict[str, Any]) -> httpx.Response:
        # ``probe_capabilities`` hands over an envelope
        # ``{"method", "url", "headers", "json"}``; the design-loop T0 path
        # hands over the raw JSON payload.  Unwrap the envelope when present
        # so both callers hit the same endpoint.
        if "json" in body and "url" in body:
            url = body["url"]
            payload = body["json"]
            headers = dict(body.get("headers") or {})
            headers.setdefault("Content-Type", "application/json")
        else:
            url = f"{base_url.rstrip('/')}/chat/completions"
            payload = body
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            return await client.post(url, json=payload, headers=headers)

    return _factory


def _build_question_answer_call(
    catalogue_path: Path, app_state: Any
) -> Any:
    """The production stage-2 question-answer edge (issue #249).

    ``app_state`` is bound at build time (``create_app`` passes the
    ``app`` instance — the closure reads ``app.state.question_capability
    _cache`` and ``app.state.conn`` through it): the returned callable is
    ``async (question_text, entries) -> reply_text`` — the ``AnswerEdge``
    seam the route expects, no per-call state lookup.

    Returns the bound ``async (question_text, entries) -> reply_text``
    edge for ``d33d.question_answer.ask_answer_call`` — which enforces the 10 s
    hard timeout (``ANSWER_CALL_TIMEOUT_SECONDS``) and the number guard
    around the raw call. One cheap single-shot completion, resolved
    lazily through the SAME live catalogue as the design role (the model
    is configured, never hardcoded; the catalogue hot-reloads — a
    ``models.yaml`` edited while the app is running is picked up on the
    next call, like the design role).

    The ``question`` role is ADDITIVE: a catalogue without a ``question``
    entry (a pre-#249 models.yaml) degrades the whole pre-route to the
    design loop exactly as today — zero added failure modes, zero added
    latency on the common case. A probe failure degrades the role to
    ``None`` (the ``send`` sender then raises a T2/T3 ``SenderError``,
    caught by ``ask_answer_call`` → design loop).

    **One 10 s enforcement point.** The operator's 10 s bound is enforced
    exactly once — by ``ask_answer_call``'s ``asyncio.wait_for`` around
    this edge. There is NO second ``wait_for`` here (the earlier
    ``_wrapper`` bound is removed: two nested bounds of the same value is
    redundant, and the inner one masked which layer actually fired). The
    per-request httpx ``timeout`` (the factory below) stays — it bounds
    a single hung HTTP request independent of the operator bound.

    **Capability probe cached per (base_url, model).** The probe is 2-3
    real LLM requests; running it on every question would make
    steady-state questions cost 3+ LLM calls. The result is cached on
    ``app.state.question_capability_cache`` (``CapabilityCache``, keyed
    by base_url + model id hash — the same cache the startup probe uses),
    so a steady-state question makes exactly ONE LLM request. A catalogue
    edit changing the question role's model or base is a cache miss and
    re-probes exactly once, like the design role. A cached, degraded
    (T2/T3) capability is NOT re-probed on the next call (the design
    role's same semantics — a re-probe storm would defeat the cache).

    **Observability (request_logs).** Each question-role call is
    logged through ``Connection.log_request`` (role=``question``,
    ``prompt_hash`` = ``canonical_hash(role, messages, system)`` — the
    same key the T0 path in ``d33d.design_llm.send`` computes, so
    model-vs-model diffs join), mirroring ``make_llm_fn``'s plumbing:
    ``llm_fn`` returns the ``LLMResult`` and the caller writes the row.
    ``prompt_tokens`` / ``completion_tokens`` come from the ``LLMResult``
    (zero when the model reports no usage); ``latency_ms`` is measured
    here. A non-OK wire response (``status == "error"``) logs status
    ``error``; a T2/T3 ``SenderError`` is NOT logged (no request went
    out — same as the design role, whose ``send`` raises before any
    HTTP call) and degrades to the design loop.
    """
    from d33d.config.catalogue import ResolutionError
    from d33d.config.resolve import resolve_model
    from d33d.design_llm import SenderError
    from d33d.question_answer import ANSWER_CALL_TIMEOUT_SECONDS, build_answer_prompt

    async def _wrapper(question: str, entries: list[dict[str, Any]]) -> str:
        import time as _time

        from d33d.config.catalogue import load_catalogue
        from d33d.config.probes import probe_capabilities
        from d33d.design_loop import make_llm_fn

        state = app_state.state  # bound at build time (see the docstring above)
        cat = load_catalogue(catalogue_path)
        try:
            res = resolve_model(cat, "question")
        except (ResolutionError, KeyError):
            # The ``question`` role is ADDITIVE: a catalogue without it
            # (a pre-#249 models.yaml) degrades the whole pre-route to
            # the design loop exactly as today — an empty reply is
            # ``answerable: false`` to the guard, which routes to the
            # loop. Zero added failure modes, zero added latency on the
            # common case.
            logger.info(
                "question-answer: no 'question' role in the catalogue — "
                "skipping stage 2 (design loop) (len(question)=%d)",
                len(question),
            )
            return ""
        provider = res.provider  # already the Provider object (resolve() does the dict lookup)
        # The per-request httpx bound (issue #249's latency decision):
        # the stage-2 LLM call is a single cheap completion — a hung
        # request is bounded per-request independent of the operator's
        # 10 s bound (which ``ask_answer_call`` enforces).
        factory = _http_request_factory(
            res.provider.base, provider.key, timeout=ANSWER_CALL_TIMEOUT_SECONDS
        )
        # The capability probe, cached per (base_url, model): steady-state
        # questions make exactly one LLM request (the completion).
        cache: CapabilityCache = state.question_capability_cache
        base_url = res.provider.base
        model_id = res.entry.model
        capability = cache.get(base_url, model_id)
        if capability is None:
            capability = await probe_capabilities(
                base_url=base_url,
                model_id=model_id,
                api_key=provider.key,
                request_factory=factory,
            )
            cache.put(base_url, model_id, capability)
        llm_fn = make_llm_fn(cat, {"question": factory}, {"question": capability})
        prompt = build_answer_prompt(question, entries)
        t0 = _time.monotonic()
        try:
            result = await llm_fn(
                "question", [{"role": "user", "content": prompt}], None
            )
        except SenderError:
            # T2/T3 (no tool channel) or a codec failure — no request
            # went out (T2/T3) or the codec failed after the wire call;
            # degrade to the design loop. (A T2/T3 probe failure degrades
            # ``capability`` and takes this path — logged here once, not
            # re-probed, per the cache semantics above.)
            logger.info(
                "question-answer: stage 2 sender error — routing to the "
                "design loop (len(question)=%d)",
                len(question),
            )
            return ""
        latency_ms = int((_time.monotonic() - t0) * 1000)
        # The request_logs row (the same path other roles use — see the
        # module docstring's observability note): the prompt_hash is the
        # ``LLMResult.prompt_hash`` — the canonical hash of the role's
        # logical request (the path in ``d33d.design_llm.send`` that
        # computed it hashes role + dialect-converted messages + system,
        # so model-vs-model diffs join on it).
        conn = getattr(state, "conn", None)
        if conn is not None:
            usage = getattr(result, "usage", None) or {}
            prompt_tokens = usage.get("prompt_tokens", 0)
            completion_tokens = usage.get("completion_tokens", 0)
            if not isinstance(prompt_tokens, int):
                prompt_tokens = 0
            if not isinstance(completion_tokens, int):
                completion_tokens = 0
            conn.log_request(
                project_id=None,
                model_alias=res.entry.id,
                model_id=res.entry.model,
                provider=res.provider.name,
                role="question",
                status="ok" if result.status != "error" else "error",
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                latency_ms=latency_ms,
                prompt_hash=result.prompt_hash or "",
            )
        return result.content

    return _wrapper


def _build_production_design_loop():
    """The production ``run_design_loop`` closure (issue #9).

    Lazily resolves the design-role model from the live catalogue on each
    call (the model is configured, never hardcoded; the catalogue is the
    source of truth and hot-reloads), probes its capability tier, and runs
    the real ``d33d.design_loop.run_design_loop`` wrapped in the
    failures.jsonl hook: an exhausted loop appends one line to
    ``app.state.failures_jsonl_path``. The hook (``default_run_design_loop_hook``)
    fires ONLY on an exhausted result and ONLY here — the eval harness's
    assert path never calls this closure, so eval-run failures are
    structurally excluded from the file (no ``is_eval`` flag). The hook's
    ``model`` / ``prompt_version`` kwargs are added by this wrapper and
    popped before the real loop runs. A test that needs a stub loop (or no
    hook) overwrites ``app.state.run_design_loop`` after ``create_app``
    returns.

    The closure is ``async`` (the finalize route awaits awaitable loop
    results): the capability probe is awaited natively instead of being
    ``asyncio.run``-nested inside the already-running event loop, which
    ``asyncio.run`` forbids with ``RuntimeError``.
    """
    from d33d.config.catalogue import load_catalogue
    from d33d.config.probes import probe_capabilities
    from d33d.config.resolve import resolve_model
    from d33d.design_loop import make_llm_fn
    from d33d.evals.failure_capture import default_run_design_loop_hook
    from d33d.prompt_hash import canonical_hash

    async def _loop(app_state: Any, **kwargs: Any) -> Any:
        catalogue_path: Path = app_state.catalogue_path
        # Project-scoped persistence (issue #72): bind the data-dir
        # renders path so the worker's post-harvest step actually fires in
        # production (the bare ``render_for_design_loop`` reference would
        # leave ``renders_dir`` unset and fall back to the global default).
        # The production closure is app-scoped (the loop kwargs carry no
        # project id), so the path is per-data-dir; the finalize route,
        # which DOES have a project id, uses the project-scoped variant in
        # :func:`d33d.versions_routes._finalize_loop_kwargs`.
        data_dir = Path(app_state.db_path).parent

        def _render_fn(scad_source: str, defines: dict[str, str]) -> Any:
            return render_for_design_loop(
                scad_source,
                defines,
                renders_dir=data_dir / "renders",
                on_progress=kwargs.get("on_progress"),
            )

        cat = load_catalogue(catalogue_path)
        res = resolve_model(cat, "design")
        provider = cat.providers[next(iter(cat.providers))]
        api_key = provider.key
        factory = _http_request_factory(res.provider.base, api_key)
        capability = await probe_capabilities(
            base_url=res.provider.base,
            model_id=res.entry.model,
            api_key=api_key,
            request_factory=factory,
        )
        # The canonical hash of the design-role prompt (role + messages,
        # as ``d33d.design_llm.send`` computes it for each call) — the
        # join key that makes "prompt v7 fails case 12 which v5 passed"
        # readable.
        prompt_version = canonical_hash(
            role="design", messages=[{"role": "user", "content": ""}]
        )
        llm_fn = make_llm_fn(
            cat,
            {"design": factory, "critique": factory},
            {"design": capability, "critique": capability},
        )
        # The user's CURRENT request text (issue #97: forwarded verbatim to
        # the loop via the hook — the loop renders it as the first line of
        # the design prompt). The historical ``chat_history`` fallback is
        # gone: prior-turn history is NOT the current request, and two
        # competing sources of "the user's message" would let the
        # failures.jsonl archive line log history instead of the request
        # (PM decision #97: the archive ``request`` equals the current
        # user's request, not a prior-turn fallback).
        request = str(kwargs.get("request") or "")
        on_progress = kwargs.get("on_progress")
        # ``bbox_fn`` — per-axis extents from the render's harvested STL
        # (``d33d.design_loop_events.bbox_from_render``). The hook forwards
        # it through ``**kwargs`` to ``run_design_loop_async`` (which pops
        # only its own ``model`` / ``prompt_version`` / ``request`` kwargs
        # before calling the real loop). ``None`` when the caller omits
        # it — the bbox gate then fails and no candidate can score the
        # bbox bit, so a chat loop without ``bbox_fn`` exhausts on
        # ``bbox_out_of_tolerance`` (the finalize seam's historical
        # omission; issue #54 wires the chat route to supply one).
        bbox_fn = kwargs.get("bbox_fn")
        return await default_run_design_loop_hook(path=app_state.failures_jsonl_path)(
            photo=kwargs.get("photo"),
            chat_history=kwargs.get("chat_history") or (),
            stated_dims=kwargs.get("stated_dims")
            if kwargs.get("stated_dims") is not None
            else (0.0, 0.0, 0.0),
            render_fn=_render_fn,
            llm_fn=llm_fn,
            bbox_fn=bbox_fn,
            request=request,
            state_params=kwargs.get("state_params"),
            state_bbox=kwargs.get("state_bbox"),
            state_stated=kwargs.get("state_stated"),
            state_meta=kwargs.get("state_meta"),
            state_confirmed=kwargs.get("state_confirmed"),
            design_source=kwargs.get("design_source"),
            model=res.entry.model,
            prompt_version=prompt_version,
            on_progress=on_progress,
        )

    def _wrapper(app: Any, **kwargs: Any) -> Any:
        return _loop(app.state, **kwargs)

    return _wrapper


__all__ = [
    "MAX_CATALOGUE_BODY_BYTES",
    "MAX_REGION_EDIT_IMAGE_BYTES",
    "MAX_REGION_EDIT_MODULE_IDS",
    "REGION_EDIT_VIEW_IDS",
    "STUB_HTML",
    "ModuleRegistryRequest",
    "RegionEditRequest",
    "create_app",
]
