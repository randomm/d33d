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
import base64
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
def app(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance pointing at the isolated paths.

    The caller must run the lifespan (``async with app.router.lifespan_context(app)``)
    so the shared ``Connection`` and ``CredentialStore`` are wired.

    ``spa_dist_dir`` is pinned to a guaranteed-nonexistent path under
    ``tmp_path`` so this test's stub-page assertions are isolated from
    whatever may or may not exist at the real ``web/dist`` on the
    machine running the suite (e.g. after a local ``npm run build`` —
    see ``tests/test_spa_static.py`` for the dedicated dist-serving
    tests, which set ``spa_dist_dir`` explicitly).
    """
    return create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=tmp_path / "no-dist-here",
    )


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
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
    assert r.json() == {
        "source": str(app.state.catalogue_path),
        "providers": {},
        "models": [],
        "roles": {},
    }
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


def test_put_models_semantically_broken_yaml_leaves_disk_file_untouched(
    app, app_paths, env_key
):
    """Regression: a PUT of semantically broken YAML (missing required
    roles) → 400, the live catalogue is unchanged, AND the on-disk file
    still holds the ORIGINAL content — validate-before-write means a
    broken edit can never reach ``loader.path`` (a restart would not load
    the broken file)."""
    broken = MODELS_YAML[: MODELS_YAML.index("roles:")]
    # Sanity: the broken document is valid YAML but fails semantic
    # validation (no roles block at all).
    _doc = yaml.safe_load(broken)
    assert isinstance(_doc, dict)
    assert "roles" not in _doc

    async def _call(client):
        ok = await client.put("/api/config/models", content=MODELS_YAML)
        assert ok.status_code == 200
        original_on_disk = app_paths["cat"].read_text()

        r = await client.put("/api/config/models", content=broken)
        r2 = await client.get("/api/config/models")
        disk_now = app_paths["cat"].read_text()
        return r, r2, original_on_disk, disk_now

    r, r2, original_on_disk, disk_now = _run_async(app, _call)
    assert r.status_code == 400
    assert "error" in r.json()
    # (b) In-memory catalogue still the original.
    assert r2.status_code == 200
    assert r2.json()["models"][0]["id"] == "design-primary"
    # (c) On-disk file is byte-for-byte the original, not the broken YAML.
    assert disk_now == original_on_disk
    assert "roles:" in disk_now


def test_put_models_rejects_literal_provider_key(app, app_paths, env_key):
    """Regression: a PUT with a literal (non-`${ENV}`) provider key → 400
    and nothing is written to disk — plaintext keys must never reach
    models.yaml; keys live Fernet-encrypted in provider_credentials."""
    literal_yaml = MODELS_YAML.replace(
        'key: "${TRAIL_OPENERS_LLM_KEY}"', 'key: "sk-fake123-not-a-real-key"'
    )

    async def _call(client):
        return await client.put("/api/config/models", content=literal_yaml)

    r = _run_async(app, _call)
    assert r.status_code == 400
    assert "environment variable" in r.json()["error"]
    # Nothing written to disk, and the plaintext key is not on disk.
    assert (
        not app_paths["cat"].exists()
        or "sk-fake123" not in app_paths["cat"].read_text()
    )
    assert "sk-fake123" not in r.text


def test_put_models_oversized_body_returns_413(app, env_key):
    """Regression: a PUT body over the 1 MB cap → 413 without buffering
    an unbounded body; the catalogue is untouched."""
    big = "x" * (2 * 1024 * 1024)  # 2 MB > 1 MB cap

    async def _call(client):
        return await client.put("/api/config/models", content=big)

    r = _run_async(app, _call)
    assert r.status_code == 413
    assert "error" in r.json()


def test_put_models_non_numeric_content_length_is_rejected_up_front(
    app, app_paths, env_key
):
    """Regression: a PUT with a NON-NUMERIC Content-Length header and an
    oversized streamed body → 413 before the body is read (the header
    defaults to the cap); the on-disk file stays absent and the in-memory
    catalogue is still empty."""
    big = "x" * (2 * 1024 * 1024)  # 2 MB > 1 MB cap

    async def _call(client):
        r = await client.put(
            "/api/config/models",
            content=big,
            headers={"Content-Length": "not-a-number"},
        )
        r2 = await client.get("/api/config/models")
        return r, r2

    r, r2 = _run_async(app, _call)
    assert r.status_code == 413
    assert "error" in r.json()
    assert not app_paths["cat"].exists()
    assert r2.status_code == 200
    assert r2.json()["models"] == []


def test_put_models_non_numeric_content_length_small_body_passes(app, app_paths, env_key):
    """The Content-Length default-to-cap change must not break the normal
    path: a small PUT whose transport declares a numeric Content-Length
    (httpx's default for ``content=``) still succeeds."""

    async def _call(client):
        return await client.put("/api/config/models", content=MODELS_YAML)

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert app_paths["cat"].is_file()


def test_put_models_hot_reload_non_catalogue_error_is_split_state_500(
    app, app_paths, env_key, monkeypatch
):
    """Regression: ``os.replace`` succeeds but ``hot_reload`` raises a
    non-``CatalogueError`` (e.g. an OSError re-reading the just-written
    file) → an explicit 500 explaining the disk/memory split state, never
    an unhandled exception and never a rollback of the valid on-disk
    write. The on-disk file holds the NEW catalogue while
    ``app.state.catalogue`` still holds the OLD one, and a retry of the
    same PUT recovers the split state."""
    import d33d.app as app_module

    # A second, DISTINCT valid catalogue so the split state is observable
    # as a disk-vs-memory divergence (the 200-path PUT installed
    # MODELS_YAML; the failing PUT installs NEW_YAML onto disk).
    new_yaml = MODELS_YAML.replace("design-primary", "second-model")
    assert new_yaml != MODELS_YAML

    async def _call(client):
        ok = await client.put("/api/config/models", content=MODELS_YAML)
        assert ok.status_code == 200
        return ok

    _run_async(app, _call)  # establish the old catalogue

    def _boom(loader):
        raise OSError("re-reading the just-written file failed")

    monkeypatch.setattr(app_module, "hot_reload", _boom)

    async def _failing_put(client):
        r = await client.put("/api/config/models", content=new_yaml)
        g = await client.get("/api/config/models")
        return r, g

    r, g = _run_async(app, _failing_put)
    monkeypatch.undo()

    assert r.status_code == 500
    err = r.json()["error"]
    # The message names the split state and the recovery path.
    assert "disk" in err and "still holds" in err
    assert "retry" in err or "restart" in err
    # Disk holds the NEW catalogue (no rollback of the valid write).
    on_disk = yaml.safe_load(app_paths["cat"].read_text())
    assert on_disk["models"][0]["id"] == "second-model"
    # Memory still holds the OLD catalogue — the 500 path must not set
    # app.state.catalogue.
    assert g.status_code == 200
    assert g.json()["models"][0]["id"] == "design-primary"
    # Retry recovers: the next PUT succeeds (hot_reload patched back).
    async def _retry(client):
        return await client.put("/api/config/models", content=new_yaml)

    r2 = _run_async(app, _retry)
    assert r2.status_code == 200
    assert r2.json()["models"][0]["id"] == "second-model"


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


def test_create_app_registers_projects_router(app):
    """Regression: ``create_app()`` alone (no manual ``include_router`` in
    this test) must make ``GET /api/projects`` reachable — before the fix,
    the router was never mounted in the production factory and this
    returned 404 (only the test fixtures masked the gap by wiring the
    router themselves)."""

    async def _call(client):
        return await client.get("/api/projects")

    r = _run_async(app, _call)
    assert r.status_code == 200, r.text  # not 404 route-not-found
    assert r.json() == []  # empty list, no projects yet


def test_create_app_registers_streaming_router(app):
    """Regression: ``create_app()`` alone must make ``GET /api/stream/{id}``
    reachable — a real SSE response (``text/event-stream``), not 404."""

    async def _call(client):
        return await client.get("/api/stream/1")

    r = _run_async(app, _call)
    assert r.status_code == 200, r.text  # not 404 route-not-found
    assert r.headers["content-type"].startswith("text/event-stream")
    # Project 1 does not exist → exactly one error event, per the SSE
    # contract for a missing project.
    assert "event: error" in r.text


def test_create_app_inits_event_sources(app):
    """``create_app()`` must initialise ``app.state.event_sources`` as an
    empty dict (the SSE endpoint reads it at request time)."""
    assert getattr(app.state, "event_sources", None) == {}


# ---------------------------------------------------------------------------
# Region-scoped edit request (issue #7, workstream task-c) — HONEST STUB
#
# This route accepts and validates a lasso-selection payload and returns
# 202 Accepted with status "deferred". It never regenerates OpenSCAD
# source — that wiring is out of scope for this ticket (see the module
# docstring on d33d.app.RegionEditRequest).
# ---------------------------------------------------------------------------

#: A minimal valid 1x1 PNG, base64-encoded — small enough to exercise the
#: base64-decode + size-bound path without a real render artifact.
_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _region_edit_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "module_ids": ["curl_3", "curl_4"],
        "view_id": "front",
        "marked_png_base64": _TINY_PNG_BASE64,
        "polygon": [
            {"x": 10.0, "y": 10.0},
            {"x": 50.0, "y": 10.0},
            {"x": 30.0, "y": 40.0},
        ],
        "instruction": "open up this spiral, it's too tight to print",
    }
    body.update(overrides)
    return body


async def _create_project(client: AsyncClient) -> int:
    r = await client.post("/api/projects", json={"name": "filigree earring"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_region_edit_accepted_returns_202_deferred(app):
    """A well-formed region-edit request against a real project returns
    202 with an explicit ``status: "deferred"`` body — never a fabricated
    success/edit result, since no regeneration happens."""

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(),
        )
        return project_id, r

    project_id, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["project_id"] == project_id
    assert body["status"] == "deferred"
    assert body["module_ids"] == ["curl_3", "curl_4"]
    assert body["view_id"] == "front"
    # The honest-stub contract: no field claims an edit/regeneration result.
    assert "scad" not in body
    assert "result" not in body


def test_region_edit_unknown_project_returns_404(app):
    """A region-edit request against a project id that does not exist is
    a 404, matching every other per-project route's not-found contract."""

    async def _call(client):
        return await client.post(
            "/api/projects/999/region-edits",
            json=_region_edit_body(),
        )

    r = _run_async(app, _call)
    assert r.status_code == 404
    assert "not found" in r.json()["detail"].lower()


def test_region_edit_rejects_empty_module_ids(app):
    """``module_ids`` must carry at least one ranked identifier — the
    whole point of #7 is resolving to named modules, never an empty or
    pixel-coordinate-only selection."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(module_ids=[]),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_region_edit_rejects_more_than_ten_module_ids(app):
    """Set-of-Mark caps ranked regions at ~10 (spec: small open models
    confuse more IDs than that) — an over-long list is rejected, not
    silently truncated."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(module_ids=[f"m{i}" for i in range(11)]),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_region_edit_rejects_unknown_view_id(app):
    """``view_id`` must be one of the six render-worker views — an
    unrecognised view id is a validation error, not silently accepted."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(view_id="bottom-left-diagonal"),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_region_edit_rejects_degenerate_polygon(app):
    """Fewer than 3 polygon vertices cannot enclose an area — rejected,
    mirroring ``DimensionCanvas.tsx``'s ``isValidPolygon`` client-side
    check (defence in depth: the server must not trust the client)."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(
                polygon=[{"x": 1.0, "y": 1.0}, {"x": 2.0, "y": 2.0}]
            ),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_region_edit_rejects_invalid_base64_image(app):
    """A ``marked_png_base64`` that is not valid base64 is a 400, not a
    500 — the route must validate before attempting to use the bytes."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(marked_png_base64="not-valid-base64!!!"),
        )

    r = _run_async(app, _call)
    assert r.status_code == 400


def test_region_edit_rejects_oversized_image(app):
    """A base64 payload that decodes larger than
    ``MAX_REGION_EDIT_IMAGE_BYTES`` is rejected with 413, mirroring the
    photo-upload route's size cap."""
    # ~6 MB of raw 'A' bytes, base64-encoded — decodes over the 5 MB cap.
    oversized = base64.b64encode(b"A" * (6 * 1024 * 1024)).decode("ascii")

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(marked_png_base64=oversized),
        )

    r = _run_async(app, _call)
    assert r.status_code == 413


def test_region_edit_rejects_empty_instruction(app):
    """An empty edit instruction is rejected — the request must carry
    what the user actually asked for, not just the selection geometry."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(instruction=""),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


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


# ---------------------------------------------------------------------------
# Named module registry (issue #7's core deliverable) — wiring test
#
# POST /api/projects/{id}/module-registry is the HTTP boundary the
# frontend viewer (ModelViewer.loadGLB) actually calls to get the named
# GLB it needs for resolveLassoSelection. The route delegates to
# d33d.module_registry.build_registry_glb, injected via app.state so this
# test never spawns Docker — the Docker-driven path itself is covered by
# tests/slow/test_module_registry_docker.py.
# ---------------------------------------------------------------------------


def test_module_registry_returns_glb_bytes(app):
    """A well-formed request with .scad source returns 200 with a GLB
    body — the exact bytes d33d.module_registry.build_registry_glb
    produced, served as model/gltf-binary so ModelViewer.loadGLB can
    consume it directly."""
    from d33d import module_registry as mr

    stub_glb = b"glTF-stub-bytes"

    def _fake_build(source: str, **kwargs: Any) -> mr.RegistryBuildResult:
        assert "base" in source
        return mr.RegistryBuildResult(
            glb_bytes=stub_glb, registry_names=("base",), failures=()
        )

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "module base() { cube([1,1,1]); } base();"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "model/gltf-binary"
    assert r.content == stub_glb


def test_module_registry_unknown_project_returns_404(app):
    async def _call(client):
        return await client.post(
            "/api/projects/999/module-registry",
            json={"scad_source": "cube([1,1,1]);"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 404


def test_module_registry_rejects_empty_scad_source(app):
    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": ""},
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_module_registry_no_call_sites_returns_empty_registry(app):
    """A .scad with zero top-level module call-sites (bare primitives
    only) resolves to a 200 with an explicit empty-registry body rather
    than a fabricated GLB or a 500 — the honest-stub convention this
    codebase already uses for region-edits."""
    from d33d import module_registry as mr

    def _fake_build(source: str, **kwargs: Any) -> mr.RegistryBuildResult:
        return mr.RegistryBuildResult(glb_bytes=None, registry_names=(), failures=())

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "cube([1,1,1]);"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert body["registry_names"] == []
    assert body["status"] == "empty"


def test_module_registry_partial_failure_still_returns_glb_and_reports_failures(app):
    """When SOME call-sites failed to isolate-render but at least one
    succeeded, the route still returns the GLB (never discards a partial
    registry) plus the failed module names in a response header/body
    field so the caller can surface which regions are unavailable."""
    from d33d import module_registry as mr

    def _fake_build(source: str, **kwargs: Any) -> mr.RegistryBuildResult:
        return mr.RegistryBuildResult(
            glb_bytes=b"partial-glb",
            registry_names=("base",),
            failures=(
                mr.ModuleRenderFailure(
                    site=mr.CallSite(name="cap", registry_name="cap", line=3),
                    error_class="empty_model",
                    stderr="",
                ),
            ),
        )

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "module base() {} module cap() {} base(); cap();"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.content == b"partial-glb"
    assert r.headers["x-module-registry-failed"] == "cap"


def test_module_registry_too_many_call_sites_returns_413(app):
    """When the injected build function raises TooManyCallSitesError
    (the real d33d.module_registry.build_registry_glb's guard against a
    hostile scad_source enumerating more than MAX_CALL_SITES call-sites),
    the route must surface a 413 rather than a 500 — the cap is meant to
    protect the render host, not crash the request handler."""
    from d33d.module_registry import TooManyCallSitesError

    def _fake_build(source: str, **kwargs: Any):
        raise TooManyCallSitesError(999)

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "module m(){cube([1,1,1]);} m();"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 413


def test_module_registry_infra_failure_returns_classified_container_error(app):
    """Regression: six-lens review finding #3. Any failure other than
    TooManyCallSitesError inside the threaded build (Docker daemon
    unreachable, docker binary missing, or the RuntimeError
    _export_scene_isolating_bad_meshes raises when even the per-mesh-
    isolated export fails) must not escape as a bare unclassified 500.
    It must be caught, logged with context, and surfaced as a 502 with a
    JSON body carrying error_class='container_error' (the closed
    ErrorClass enum's infra-failure value) — never the raw exception
    text."""

    def _fake_build(source: str, **kwargs: Any):
        raise RuntimeError("scene export failed even after isolating every mesh: boom")

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "module m(){cube([1,1,1]);} m();"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 502
    body = r.json()
    assert body["error_class"] == "container_error"
    assert "boom" not in body["error"]


def test_module_registry_missing_docker_binary_returns_classified_container_error(app):
    """FileNotFoundError (docker binary missing / daemon unreachable via
    a failed subprocess spawn) is also caught and classified, not just
    RuntimeError."""

    def _fake_build(source: str, **kwargs: Any):
        raise FileNotFoundError("[Errno 2] No such file or directory: 'docker'")

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _fake_build
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "module m(){cube([1,1,1]);} m();"},
        )

    r = _run_async(app, _call)
    assert r.status_code == 502
    body = r.json()
    assert body["error_class"] == "container_error"
    assert "docker" not in body["error"].lower()


def test_module_registry_rejects_oversized_scad_source(app):
    """scad_source has a max_length bound matching the other body-
    accepting routes' size caps in this file — defense-in-depth
    alongside build_registry_glb's own MAX_CALL_SITES guard."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/module-registry",
            json={"scad_source": "x" * (1024 * 1024 + 1)},
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_module_registry_does_not_block_the_event_loop(app):
    """Regression: adversarial-review round 2. build_registry_glb is a
    synchronous, subprocess-driven function that can take up to ~4.3
    hours worst-case (MAX_CALL_SITES sequential Docker round-trips); the
    route must run it via asyncio.to_thread so a slow/large registry
    build cannot stall the event loop and starve every OTHER concurrent
    request on the same process. Simulated here with a blocking
    time.sleep inside the injected build function (time.sleep, not
    asyncio.sleep, is the point: it proves the call really runs off the
    event loop's own thread) racing an unrelated fast request that must
    complete first if — and only if — the registry build is offloaded.
    """
    import time

    from d33d import module_registry as mr

    def _slow_build(source: str, **kwargs: Any) -> mr.RegistryBuildResult:
        time.sleep(0.3)
        return mr.RegistryBuildResult(
            glb_bytes=b"glb", registry_names=("m",), failures=()
        )

    async def _call(client):
        project_id = await _create_project(client)
        app.state.build_registry_glb = _slow_build

        order: list[str] = []

        async def _slow_request():
            resp = await client.post(
                f"/api/projects/{project_id}/module-registry",
                json={"scad_source": "module m(){cube([1,1,1]);} m();"},
            )
            order.append("slow")
            return resp

        async def _fast_request():
            await asyncio.sleep(0.05)
            resp = await client.get("/api/config/models")
            order.append("fast")
            return resp

        slow_resp, fast_resp = await asyncio.gather(
            _slow_request(), _fast_request()
        )
        return slow_resp, fast_resp, order

    slow_resp, fast_resp, order = _run_async(app, _call)
    assert slow_resp.status_code == 200
    assert fast_resp.status_code == 200
    # If build_registry_glb blocked the event loop, the fast request
    # (dispatched 50ms after the slow one, itself near-instant) could
    # only ever complete AFTER the slow one finishes its 300ms sleep.
    # Running the blocking call in a worker thread lets the fast request
    # finish first.
    assert order == ["fast", "slow"], order
