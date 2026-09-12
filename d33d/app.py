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
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from d33d import db
from d33d.config import ModelCatalogueLoader, hot_reload
from d33d.config.catalogue import (
    Catalogue,
    CatalogueError,
    ResolutionError,
    load_catalogue,
)
from d33d.config.resolve import resolve_model
from d33d.projects import create_projects_router
from d33d.security import credentials as cred
from d33d.streaming import create_streaming_router

#: Stub SPA page served at ``/`` until issue #6 ships the real build.
#: Static HTML — not a React build, per the ticket.
#: Hard cap on the ``PUT /api/config/models`` body. A models.yaml catalogue
#: is a few KB in practice; anything bigger is not a legitimate edit and is
#: rejected before it can consume unbounded memory.
MAX_CATALOGUE_BODY_BYTES = 1024 * 1024  # 1 MB

#: ``${ENV_VAR}`` key reference (reused from the catalogue module — the
#: PUT boundary only accepts this form for provider keys).
_ENV_VAR_KEY_RE = re.compile(r"^\$\{[A-Za-z_][A-Za-z0-9_]*\}$")


async def _read_bounded_body(request: Request, cap: int) -> bytes | JSONResponse:
    """Read the request body, bounded to ``cap`` bytes.

    Returns the full body on success, or a 413 ``JSONResponse`` to return
    to the client as-is. Never buffers more than ``cap``+1 bytes: an
    oversized declared Content-Length is rejected after draining, and an
    oversized streamed body aborts mid-stream.
    """
    declared = request.headers.get("content-length")
    try:
        declared_len = int(declared) if declared is not None else 0
    except ValueError:
        declared_len = 0

    if declared_len > cap:
        # Reject early; still drain what the client sends so the
        # connection stays usable.
        await request.body()
        return JSONResponse(
            status_code=413,
            content={"error": f"body exceeds {cap} byte limit"},
        )

    body_parts: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > cap:
            await request.body()  # drain the remainder
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
        new_cat, _ = hot_reload(loader)
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
    # The SSE endpoint (streaming.py) reads this dict at request time; it
    # maps ``project_id -> AsyncIterator[(event, data)]`` and starts empty
    # (no active streams until a future ticket wires the design loop).
    app.state.event_sources: dict[int, AsyncIterator[tuple[str, dict[str, Any]]]] = {}

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


__all__ = [
    "MAX_CATALOGUE_BODY_BYTES",
    "STUB_HTML",
    "create_app",
]
