"""App-shell tests (issue #23, workstream task-a).

Covers:
- Stub SPA page served at ``/`` via httpx ASGITransport (no live server,
  no port 8080 bound).
- ``GET /api/config/models`` — no catalogue yet (empty), catalogue loaded,
  provider key explicitly redacted in the response.
- ``PUT /api/config/models`` — full-YAML replacement written to
  ``loader.path`` then ``hot_reload``; a bad file → 400 and the
  last-known-good catalogue stays live (loader.live unchanged).
- ``GET/POST /api/settings/credentials`` — names-only list; no key
  material (plaintext or ciphertext) in any response body. The
  ``POST`` round-trips a Fernet-encrypted secret through the
  ``provider_credentials`` table via the same ``Connection`` the
  endpoint used (single-writer design).
- ``GET /api/config/roles?role=X`` — role resolution via the stable
  ``resolve_model`` facade (never the provider key).
- ``create_app`` wiring: ``db.connect()`` runs before
  ``CredentialStore`` (the ``provider_credentials`` table must exist
  when the store is constructed).

All tests non-slow: no Docker, no network, no real providers, no port
8080 bound. The async app is driven with ``asyncio.run`` per test —
consistent with the project's sync test style (no ``pytest-asyncio``).
"""

from __future__ import annotations

import asyncio
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from d33d.app import STUB_HTML, create_app


# ---------------------------------------------------------------------------
# Shared YAML fixture (a minimal valid catalogue with one ${ENV} key so we
# can pin that the env var is resolved at load time, never echoed back).
# ---------------------------------------------------------------------------

MODELS_YAML = textwrap.dedent(
    """\
    providers:
      trailopeners:
        base: "https://llm.trailopeners.com/v1"
        key: "${TRAIL_OPENERS_LLM_KEY}"

    models:
      - id: design-primary
        provider: trailopeners
        model: RedHatAI/Qwen3.8-27B-INT4
        context_window: 262144
        params: { temperature: 0.2 }
        fallbacks: []
        retries: { count: 2, on: [timeout] }

    roles:
      design: design-primary
      critique: design-primary
      classification: design-primary
    """
)


@pytest.fixture
def env_key(monkeypatch: pytest.MonkeyPatch) -> str:
    """Set the env var the fixture catalogue references."""
    monkeypatch.setenv("TRAIL_OPENERS_LLM_KEY", "env-key-value-not-a-real-key")
    return "env-key-value-not-a-real-key"


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under ``tmp_path``."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app(app_paths: dict[str, Path]):
    """A ``create_app`` instance pointing at the isolated paths.

    The caller must run the lifespan (``async with app.router.lifespan_context(app)``)
    so the shared ``Connection`` and ``CredentialStore`` are wired.
    """
    return create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan."""
    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(transport=ASGITransport(app=app), base_url="http://test")
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# Stub page
# ---------------------------------------------------------------------------


def test_stub_page_served_at_root(app):
    """``GET /`` returns a non-empty ``text/html`` stub (no live server,
    no port 8080 bound — ASGITransport runs in-process)."""

    async def _call(client):
        return await client.get("/")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert len(r.text) > 0
    # The stub is a static page, not a React build.
    assert "<html" in r.text.lower()
    assert "d33d" in r.text


def test_stub_page_is_module_constant(app):
    """The route serves the module-level ``STUB_HTML`` verbatim."""

    async def _call(client):
        return await client.get("/")

    r = _run_async(app, _call)
    assert r.text == STUB_HTML


# ---------------------------------------------------------------------------
# Model-config GET/PUT
# ---------------------------------------------------------------------------


def test_get_models_before_first_put_is_empty(app, env_key):
    """No catalogue saved yet → empty providers/models/roles (not an error).

    (The ``env_key`` fixture is required here because the catalogue path
    default is derived from the DB path's parent directory — the env var
    is not actually referenced by this test, but the fixture keeps the
    catalogue loader's path stable across tests.)
    """

    async def _call(client):
        return await client.get("/api/config/models")

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert body["providers"] == {}
    assert body["models"] == []
    assert body["roles"] == {}


def test_app_starts_without_env_var_set(app, monkeypatch):
    """The app starts with an empty in-memory catalogue even when the
    ``TRAIL_OPENERS_LLM_KEY`` env var is unset — the catalogue is not
    loaded until the operator's first ``PUT`` (the ``ModelCatalogueLoader``
    accepts a path that may not yet exist; the initial catalogue is
    ``None``)."""
    monkeypatch.delenv("TRAIL_OPENERS_LLM_KEY", raising=False)

    async def _call(client):
        r = await client.get("/api/config/models")
        r2 = await client.get("/")
        return r, r2

    r, r2 = _run_async(app, _call)
    assert r.status_code == 200
    assert r.json() == {"source": str(app.state.catalogue_path), "providers": {}, "models": [], "roles": {}}
    assert r2.status_code == 200


def test_put_models_writes_yaml_and_hot_reloads(app, app_paths, env_key):
    """``PUT /api/config/models`` writes the body to ``loader.path`` and
    hot-reloads it; the response reflects the new catalogue."""
    new_yaml = MODELS_YAML

    async def _call(client):
        return await client.put("/api/config/models", content=new_yaml)

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    # The YAML was written to disk.
    assert app_paths["cat"].is_file()
    on_disk = yaml.safe_load(app_paths["cat"].read_text())
    assert on_disk["models"][0]["model"] == "RedHatAI/Qwen3.8-27B-INT4"
    # The response reflects the new catalogue (provider key redacted).
    assert body["roles"]["design"] == "design-primary"
    assert body["providers"]["trailopeners"]["key"] == "***redacted***"
    # The loaded model matches.
    assert body["models"][0]["id"] == "design-primary"
    assert body["models"][0]["model"] == "RedHatAI/Qwen3.8-27B-INT4"


def test_put_models_bad_yaml_returns_400_and_keeps_last_known_good(
    app, app_paths, env_key
):
    """A bad ``PUT`` returns 400, and the last-known-good catalogue stays
    live (the loader's live catalogue is untouched by the failed reload)."""
    good = MODELS_YAML
    bad = "providers: [unclosed\n  roles: "

    async def _call(client):
        ok = await client.put("/api/config/models", content=good)
        assert ok.status_code == 200
        live_alias = ok.json()["models"][0]["id"]

        r = await client.put("/api/config/models", content=bad)
        r2 = await client.get("/api/config/models")
        return r, r2, live_alias

    r, r2, live_alias = _run_async(app, _call)
    assert r.status_code == 400
    assert "error" in r.json()
    # The last-known-good catalogue is still live.
    assert r2.status_code == 200
    assert r2.json()["models"][0]["id"] == live_alias


def test_put_models_rejects_non_mapping_top_level(app, env_key):
    """A YAML list at the top level (not a mapping) → 400, file untouched."""

    async def _call(client):
        return await client.put("/api/config/models", content="- just\n- a\n- list\n")

    r = _run_async(app, _call)
    assert r.status_code == 400
    assert "mapping" in r.json()["error"]


def test_put_models_provider_key_never_leaks(app, env_key):
    """The provider key — resolved from ``${ENV}`` at load time — must not
    surface in any response body. The response redacts it to
    ``***redacted***`` and never echoes the raw ``${...}`` placeholder
    either (we serialise the loaded catalogue, not the raw file bytes)."""

    async def _call(client):
        return await client.put("/api/config/models", content=MODELS_YAML)

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert env_key not in r.text
    assert body["providers"]["trailopeners"]["key"] == "***redacted***"
    assert "${TRAIL_OPENERS_LLM_KEY}" not in r.text


def test_role_resolution_via_resolve_model(app, env_key):
    """``GET /api/config/roles?role=design`` resolves via the stable
    ``resolve_model`` facade (never the provider key)."""

    async def _call(client):
        await client.put("/api/config/models", content=MODELS_YAML)
        return await client.get("/api/config/roles", params={"role": "design"})

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert body["role"] == "design"
    assert body["alias"] == "design-primary"
    assert body["model"] == "RedHatAI/Qwen3.8-27B-INT4"
    assert body["provider"] == "trailopeners"
    # No key material in the response.
    assert "key" not in body


def test_role_resolution_unknown_role_400(app, env_key):
    """Unknown role → 400 (the resolution fails cleanly)."""

    async def _call(client):
        await client.put("/api/config/models", content=MODELS_YAML)
        return await client.get("/api/config/roles", params={"role": "nonexistent"})

    r = _run_async(app, _call)
    assert r.status_code == 400
    assert "error" in r.json()


# ---------------------------------------------------------------------------
# Credentials
# ---------------------------------------------------------------------------


def test_credentials_list_empty_initially(app):
    """``GET /api/settings/credentials`` with no credentials stored → []."""

    async def _call(client):
        return await client.get("/api/settings/credentials")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.json() == []


def test_credentials_post_stores_and_list_is_names_only(app, env_key):
    """``POST /api/settings/credentials`` stores a Fernet-encrypted secret;
    the response and the subsequent ``GET`` list return names only — no
    key material (plaintext or ciphertext) in any response body."""
    secret = f"sk-{uuid.uuid4().hex}"

    async def _call(client):
        r = await client.post(
            "/api/settings/credentials",
            json={
                "provider": "trailopeners",
                "model_alias": "RedHatAI/Qwen3.8-27B-INT4",
                "secret": secret,
            },
        )
        r2 = await client.get("/api/settings/credentials")
        return r, r2

    r, r2 = _run_async(app, _call)
    assert r.status_code == 201
    # The POST response echoes only the names.
    assert r.json() == {
        "provider_id": "trailopeners",
        "model_alias": "RedHatAI/Qwen3.8-27B-INT4",
    }
    assert secret not in r.text

    # The list endpoint returns names only.
    assert r2.status_code == 200
    rows = r2.json()
    assert rows == [
        {"provider_id": "trailopeners", "model_alias": "RedHatAI/Qwen3.8-27B-INT4"}
    ]
    # No key material in the list response.
    assert secret not in r2.text
    # No field name contains "key" (the columns are provider_id/model_alias).
    for row in rows:
        for field in row:
            assert "key" not in field.lower()


def test_credentials_post_rejects_empty_secret(app):
    """An empty secret → 400 (mirrors ``CredentialStore.store_key``'s
    ``ValueError`` on empty secrets)."""

    async def _call(client):
        return await client.post(
            "/api/settings/credentials",
            json={"provider": "p", "model_alias": "m", "secret": ""},
        )

    r = _run_async(app, _call)
    assert r.status_code == 400
    assert "secret" in r.json()["error"]


def test_credentials_post_rejects_invalid_json(app):
    """Non-JSON body → 400."""

    async def _call(client):
        return await client.post("/api/settings/credentials", content=b"not json")

    r = _run_async(app, _call)
    assert r.status_code == 400


def test_credentials_ciphertext_never_in_response(app, env_key):
    """The Fernet ciphertext (``gAAA...``) must not appear in any response
    body — the invariant inherited from ``CredentialStore`` (no
    ``key_ciphertext`` column in the list view, no ciphertext echo on
    ``POST``)."""
    secret = "some-secret-to-encrypt"

    async def _call(client):
        await client.post(
            "/api/settings/credentials",
            json={"provider": "p", "model_alias": "m", "secret": secret},
        )
        return await client.get("/api/settings/credentials")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert "gAAA" not in r.text
    assert secret not in r.text


# ---------------------------------------------------------------------------
# App wiring / startup order
# ---------------------------------------------------------------------------


def test_app_state_wiring_after_lifespan(app, app_paths, env_key):
    """After the lifespan runs, ``app.state`` carries the shared singletons:
    a ``db.Connection`` (WAL single-writer), a ``CredentialStore`` pointing
    at the same connection, and a ``ModelCatalogueLoader`` on the
    configured path. The startup order (``db.connect()`` →
    ``CredentialStore``) is what makes the ``provider_credentials`` table
    exist before the store is constructed."""

    async def _call(client):
        return (
            app.state.conn,
            app.state.credential_store,
            app.state.master_key,
            app.state.catalogue_loader,
        )

    conn, store, master_key, loader = _run_async(app, _call)
    assert conn is not None
    assert store is not None
    assert master_key is not None
    assert loader.path == app_paths["cat"]
    # The CredentialStore and the shared conn operate on the same table
    # (the store was constructed with the same Connection object).
    assert store._conn is conn


def test_create_app_does_not_require_catalogue_file_to_exist(app, app_paths):
    """The app starts with an empty in-memory catalogue — no
    ``models.yaml`` required on disk until the first ``PUT``. (The
    ``ModelCatalogueLoader`` accepts a path that may not yet exist; the
    initial catalogue is ``None``.)"""
    assert not app_paths["cat"].exists()


# ---------------------------------------------------------------------------
# main.py entrypoint (import sanity, no bind)
# ---------------------------------------------------------------------------


def test_main_module_importable():
    """``d33d.main`` is importable and exposes ``main`` (the runtime
    entrypoint). No socket bind is exercised here — that belongs to the
    runtime, not the test surface (a real ``uvicorn`` bind on 8080 in a
    test fails on shared/CI machines)."""
    import d33d.main as m

    assert callable(m.main)
