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
``d33d/streaming.py``) are a sibling workstream: they register their routers
on the shared ``app.state`` objects this factory creates, and are out of
scope here.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import yaml
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from d33d import db
from d33d.config import ModelCatalogueLoader, hot_reload
from d33d.config.catalogue import Catalogue, CatalogueError, ResolutionError
from d33d.config.resolve import resolve_model
from d33d.security import credentials as cred

#: Stub SPA page served at ``/`` until issue #6 ships the real build.
#: Static HTML — not a React build, per the ticket.
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
    """
    db_path_p = Path(db_path)
    data_dir = db_path_p.parent if db_path_p.parent not in (Path(""), Path(".")) else Path(".")
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

    @app.get("/", response_class=HTMLResponse)
    async def stub_page() -> HTMLResponse:
        """Stub SPA page — static HTML until issue #6's build exists."""
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
        """Full-YAML replacement: write to ``catalogue_path``, then
        ``hot_reload``. A bad file (``CatalogueError``) → 400, and the
        last-known-good catalogue stays live (the loader's live catalogue
        is untouched by the failed reload)."""
        body = await request.body()
        loader: ModelCatalogueLoader = app.state.catalogue_loader
        try:
            # Validate first: parse the body to confirm it is valid YAML
            # with the right shape, then write, then reload. If the parse
            # fails we never touch the file on disk.
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
            # Write atomically: temp file + rename (no partial write).
            loader.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = loader.path.with_suffix(loader.path.suffix + ".tmp")
            tmp.write_bytes(body)
            os.replace(tmp, loader.path)
            new_cat, _ = hot_reload(loader)
            app.state.catalogue = new_cat
            return JSONResponse(content=_serialise_catalogue(new_cat))
        except CatalogueError as e:
            # Bad file → 400, last-known-good stays live (loader.live is
            # unchanged by the failed reload in hot_reload).
            app.state.catalogue = loader.live
            return JSONResponse(
                status_code=400,
                content={"error": f"catalogue error: {e.message}"},
            )
        except OSError as e:
            return JSONResponse(
                status_code=500,
                content={"error": f"could not write catalogue: {e}"},
            )

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
            return JSONResponse(status_code=400, content={"error": f"invalid JSON: {e}"})
        provider = data.get("provider")
        model_alias = data.get("model_alias")
        secret = data.get("secret")
        if not isinstance(provider, str) or not provider:
            return JSONResponse(
                status_code=400, content={"error": "'provider' must be a non-empty string"}
            )
        if not isinstance(model_alias, str) or not model_alias:
            return JSONResponse(
                status_code=400, content={"error": "'model_alias' must be a non-empty string"}
            )
        if not isinstance(secret, str) or not secret:
            return JSONResponse(
                status_code=400, content={"error": "'secret' must be a non-empty string"}
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

    return app


__all__ = [
    "STUB_HTML",
    "create_app",
]
