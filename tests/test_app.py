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
import logging
import sqlite3
import textwrap
import uuid
from pathlib import Path
from typing import Any

import pytest
import yaml
from httpx import ASGITransport, AsyncClient

from d33d import print_validation as pv
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
def app_with_projects(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the git path pointed at ``tmp_path``
    (the part upload's route must be mounted to create a project with a
    part)."""
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default


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


def test_put_models_non_numeric_content_length_small_body_passes(
    app, app_paths, env_key
):
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
    # Built by mutating the PARSED document (rename the alias consistently
    # in models + roles) rather than string-replacing the raw YAML, so the
    # result is guaranteed to stay a structurally valid catalogue.
    new_doc = yaml.safe_load(MODELS_YAML)
    new_doc["models"][0]["id"] = "second-model"
    new_doc["roles"] = {
        k: "second-model" if v == "design-primary" else v
        for k, v in new_doc["roles"].items()
    }
    new_yaml = yaml.safe_dump(new_doc)
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

    # monkeypatch.undo() also reverts the ``env_key`` fixture's
    # ``TRAIL_OPENERS_LLM_KEY`` — re-set it so the retry PUT's catalogue
    # validation can resolve the ``${ENV}`` reference.
    monkeypatch.setenv("TRAIL_OPENERS_LLM_KEY", env_key)

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


# ---------------------------------------------------------------------------
# Issue #346: the render-worker image pre-flight at startup — the one-shot
# WARNING (image missing / label mismatch + the exact rebuild command)
# through the injectable ``image_check`` seam; no WARNING for a healthy
# image. No real Docker: the seam is injected, not the daemon probed.
# ---------------------------------------------------------------------------


def test_startup_image_missing_logs_one_warning_with_rebuild_command(
    app_paths, tmp_path, env_key, caplog
):
    """Issue #346: startup with a missing render-worker image logs ONE
    WARNING naming the problem (the image) and the exact rebuild command —
    the operator decision's startup observability; the seam is injected so
    no real Docker probe runs."""
    from d33d.app import create_app

    detail = {
        "reason": "image_missing",
        "expected": "abc123",
        "rebuild_command": "docker build --platform=linux/amd64 -t d33d/render-worker:local .",
    }
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=tmp_path / "no-dist-here",
        image_check=lambda: dict(detail),
    )

    async def _call(client):
        return None

    with caplog.at_level("WARNING"):
        _run_async(app, _call)

    startup_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "renderer image pre-flight" in r.getMessage()
    ]
    assert len(startup_warnings) == 1, "exactly one startup WARNING (not zero, not many)"
    msg = startup_warnings[0].getMessage()
    assert "image_missing" in msg or "missing" in msg, f"names the problem: {msg}"
    assert detail["rebuild_command"] in msg, f"names the rebuild command: {msg}"


def test_startup_image_label_mismatch_logs_one_warning_naming_both_hashes(
    app_paths, tmp_path, env_key, caplog
):
    """Issue #346: a label mismatch names BOTH build-hash values (actual
    vs expected) in the one startup WARNING + the rebuild command."""
    from d33d.app import create_app

    detail = {
        "reason": "label_mismatch",
        "expected": "abc123",
        "actual": "stale999",
        "rebuild_command": "docker build --platform=linux/amd64 -t d33d/render-worker:local .",
    }
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=tmp_path / "no-dist-here",
        image_check=lambda: dict(detail),
    )

    async def _call(client):
        return None

    with caplog.at_level("WARNING"):
        _run_async(app, _call)

    startup_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "renderer image pre-flight" in r.getMessage()
    ]
    assert len(startup_warnings) == 1
    msg = startup_warnings[0].getMessage()
    assert "stale999" in msg, f"names the image's actual label: {msg}"
    assert "abc123" in msg, f"names the working tree's expected hash: {msg}"
    assert detail["rebuild_command"] in msg


def test_startup_image_healthy_logs_no_renderer_image_warning(
    app_paths, tmp_path, env_key, caplog
):
    """Issue #346: a healthy image (probe returns None) logs NO
    renderer-image startup warning — the WARNING fires only on a verified
    fault."""
    from d33d.app import create_app

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=tmp_path / "no-dist-here",
        image_check=lambda: None,
    )

    async def _call(client):
        return None

    with caplog.at_level("WARNING"):
        _run_async(app, _call)

    startup_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "renderer image pre-flight" in r.getMessage()
    ]
    assert startup_warnings == [], (
        f"no renderer-image WARNING for a healthy image; got {startup_warnings}"
    )


def test_startup_image_probe_past_wall_clock_bound_logs_timeout_warning_and_completes(
    app_paths, tmp_path, env_key, monkeypatch, caplog
):
    """Issue #346: the startup image probe runs OFF the event loop with a
    wall-clock bound (``asyncio.wait_for(asyncio.to_thread(...), 20)``).
    A probe past the bound (a hung docker daemon) is treated like an
    ``OSError``: ONE WARNING saying the image check timed out, NO
    stale-image warning (the fault is not established), and startup still
    completes. The bound is patched down to 0.05 s so the test stays
    fast; the seam is injected, so no real Docker runs."""
    import d33d.app as app_mod

    monkeypatch.setattr(app_mod, "_IMAGE_CHECK_TIMEOUT_SECONDS", 0.05)

    def _slow_probe() -> dict[str, str] | None:
        import time

        time.sleep(0.3)
        return {
            "reason": "image_missing",
            "expected": "abc123",
            "rebuild_command": "docker build -t d33d/render-worker:local .",
        }

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=tmp_path / "no-dist-here",
        image_check=_slow_probe,
    )

    async def _call(client):
        return None

    with caplog.at_level("WARNING"):
        _run_async(app, _call)  # must return — startup completes

    startup_warnings = [
        r
        for r in caplog.records
        if r.levelno == logging.WARNING and "renderer image pre-flight" in r.getMessage()
    ]
    assert len(startup_warnings) == 1, "exactly one startup WARNING"
    msg = startup_warnings[0].getMessage()
    assert "timed out" in msg, f"the timeout WARNING names the timeout: {msg}"
    # The stale-image warning (the fault naming + rebuild command) must
    # NOT be emitted — the fault was not established.
    assert "rebuild" not in msg, f"no stale-image warning: {msg}"
    assert "image_missing" not in msg and "missing from" not in msg


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
# Region-scoped edit request (issue #7, workstream task-c; design-loop
# wiring by issue #68)
#
# The route validates the point-pick payload and drives the injected
# design loop (``app.state.run_design_loop``) in the background — the
# same adapter pattern as ``POST /{id}/chat``. The 202 body mirrors
# ``/chat`` (``{project_id, status: "accepted"}``); the version arrives
# only via the SSE stream. Full integration coverage (pass → version,
# exhausted → no version + terminal error frame, kwargs contract) lives
# in tests/versioning/test_design_loop_finalize.py; this file keeps the
# payload-validation gates (404/400/413/422) plus the 202 contract.
# ---------------------------------------------------------------------------

#: A minimal valid 1x1 PNG, base64-encoded — small enough to exercise the
#: base64-decode + size-bound path without a real render artifact.
_TINY_PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="


def _region_edit_body(**overrides: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "module_ids": ["curl_3", "curl_4"],
        "view_id": "front",
        "marked_png_base64": _TINY_PNG_BASE64,
        "point": {"x": 300.0, "y": 200.0},
        "instruction": "open up this spiral, it's too tight to print",
    }
    body.update(overrides)
    return body


async def _create_project(client: AsyncClient) -> int:
    r = await client.post("/api/projects", json={"name": "filigree earring"})
    assert r.status_code == 201, r.text
    return int(r.json()["id"])


def test_region_edit_accepted_returns_202_accepted(app):
    """A well-formed region-edit request against a real project returns
    202 with the concrete ``{project_id, status: "accepted"}`` body
    (mirroring ``/chat``) — no ``status: "deferred"`` field, no
    detail/module_ids/view_id echo; the version arrives only via the
    SSE stream, never in the 202 body."""

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(),
        )
        return project_id, r

    project_id, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"project_id": project_id, "status": "accepted"}


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


def test_region_edit_accepts_empty_module_ids(app):
    """``module_ids`` may be EMPTY (issue #98): a streamed unnamed STL
    still selects fine, grounded by the marked PNG + picked point alone.
    The module ids are supplementary context, never the grounding — an
    empty list is valid and must not 422."""

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(module_ids=[]),
        )
        return project_id, r

    project_id, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"project_id": project_id, "status": "accepted"}


def test_region_edit_accepts_negative_point(app):
    """A NEGATIVE but finite point coordinate is ACCEPTED: the server does
    not know the client's view dimensions, so bounds are the client's
    business (a client may use a different origin). Finiteness — not
    range — is the only constraint the server may enforce."""

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(point={"x": -10.5, "y": -20.25}),
        )
        return project_id, r

    project_id, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"project_id": project_id, "status": "accepted"}


def test_region_edit_empty_module_ids_composes_no_modules_request_text(app):
    """With an EMPTY ``module_ids`` list the composed request text is
    EXACTLY the no-modules form — no "on modules" prefix — because the
    with-modules branch and this one produce different strings and an
    unasserted branch can silently change. The stub design loop captures
    the ``request`` kwarg it was called with, which is the composed text.
    The registered event source is pumped synchronously (to its terminal
    frame) after the 202 — the loop runs then, not inside the request."""
    captured: dict[str, Any] = {}

    class _PassResult:
        """A minimal pass result the adapter can handle (no render
        artifacts, no best/scad — the adapter omits those fields)."""

        status = "pass"
        best = None

    async def _stub_loop(app, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _PassResult()

    app.state.run_design_loop = _stub_loop

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(module_ids=[]),
        )
        # The route registers the event source synchronously and the loop
        # is driven by whoever consumes the generator — pump it to its
        # terminal frame so the stub runs and captures its kwargs.
        source = app.state.event_sources.get(project_id)
        if source is not None:
            async for _event, _data in source:
                pass
        return project_id, r

    project_id, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    assert r.json() == {"project_id": project_id, "status": "accepted"}
    assert "request" in captured, "stub design loop was not invoked with the request kwarg"
    expected = (
        "Region edit at the marked point (view: front): "
        "open up this spiral, it's too tight to print"
    )
    assert captured["request"] == expected
    assert "on modules" not in captured["request"]


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


def test_region_edit_rejects_missing_point(app):
    """The single ``point`` field (the click location in the view's
    CSS-pixel space) is REQUIRED — the marked PNG is grounded at exactly
    this location, so a request without it cannot be audited or gated.
    This replaces the old degenerate-polygon gate (issue #98 re-based the
    ``polygon`` field into a single ``point``)."""

    async def _call(client):
        project_id = await _create_project(client)
        body = _region_edit_body()
        del body["point"]
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=body,
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_point_location_rejects_inf_coordinates(app):
    """±inf are NOT finite floats and NOT valid JSON — the wire path
    rejects them (``json.dumps`` raises on inf, which is a rejection at
    serialization), and the MODEL's finiteness validator rejects them
    for the in-process path. This test pins the model-level rule (the
    authoritative guard for the in-process boundary) so the finiteness
    requirement is pinned regardless of the serialization layer."""
    from pydantic import ValidationError

    from d33d.app import PointLocation

    with pytest.raises(ValidationError):
        PointLocation(x=float("nan"), y=200.0)
    with pytest.raises(ValidationError):
        PointLocation(x=300.0, y=float("-inf"))
    # A normal value is accepted (the validator is not over-restrictive).
    p = PointLocation(x=300.0, y=200.0)
    assert p.x == 300.0 and p.y == 200.0


def test_point_location_accepts_negative_finite_coordinates(app):
    """A negative finite coordinate is ACCEPTED — the server does not know
    the client's view dimensions, so bounds are the client's business (a
    client may use a different origin); finiteness is the only constraint.
    This is the model-level companion to the route-level
    ``test_region_edit_accepts_negative_point`` (which exercises the full
    202 path)."""
    from d33d.app import PointLocation

    p = PointLocation(x=-10.5, y=-20.25)
    assert p.x == -10.5 and p.y == -20.25


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


# ---------------------------------------------------------------------------
# Region-edit wire — hit_point_mm / face_normal (issue #338, operator
# decisions 4–5)
# ---------------------------------------------------------------------------


def _svc(app: Any) -> Any:
    """The app's version service on a LIVE connection (reconnect after a
    previous ``_run_async`` teardown closed the handle)."""
    svc = app.state.versions
    try:
        svc.conn.raw.execute("SELECT 1")
        return svc
    except sqlite3.Error:
        import d33d.db as db_mod
        from d33d import versions as versions_mod

        fresh = db_mod.connect(app.state.db_path)
        versions_mod.migrate(fresh)
        app.state.conn = fresh
        fresh_svc = versions_mod.VersionService(fresh)
        app.state.versions = fresh_svc
        return fresh_svc


def _set_part_columns(
    app: Any,
    pid: int,
    *,
    unit_status: str,
    hole_count: int | None = None,
) -> None:
    """Set the project's part columns directly on the DB (the same UPDATE
    the #325 settle path runs, minus the settle call). ``hole_count``
    (issue #351): when given, also writes the ``part_report`` JSON with
    ``{"hole_count": hole_count}`` — the stored import-time hole fact
    the fill-and-recut gate reads; default ``None`` leaves the report
    NULL (the legacy-row unknown case, which keeps the offer)."""
    import json as _json

    conn = app.state.conn
    report = _json.dumps({"hole_count": hole_count}) if hole_count is not None else None
    conn.raw.execute(
        "UPDATE projects SET part_filename='part.stl', part_format='stl', "
        "part_unit='mm', part_unit_status=?, part_scale=1.0, part_report=? "
        "WHERE id=?",
        (unit_status, report, pid),
    )
    conn.commit()


def test_region_edit_accepts_hit_point_and_face_normal(app):
    """A region edit with ``hit_point_mm`` and ``face_normal`` is accepted
    (202), and both fields are added to the grounding text the design loop
    receives. The stub loop captures the ``request_text`` kwarg."""
    captured: dict[str, Any] = {}

    class _PassResult:
        status = "pass"
        best = None

    async def _stub_loop(app, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _PassResult()

    app.state.run_design_loop = _stub_loop

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(
                hit_point_mm=[12.0, 0.0, 20.0],
                face_normal=[0.0, 1.0, 0.0],
            ),
        )
        source = app.state.event_sources.get(project_id)
        if source is not None:
            async for _event, _data in source:
                pass
        return project_id, r

    _pid, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    assert "request" in captured, "stub loop not invoked with request kwarg"
    rt = captured["request"]
    assert "Pick hit point in mm: 12, 0, 20" in rt, rt
    assert "Picked face normal (unit, world space): 0, 1, 0" in rt, rt


def test_region_edit_hit_point_only_still_accepted(app):
    """``hit_point_mm`` without ``face_normal`` is accepted (both are
    optional). The grounding text has the hit point but not the normal."""
    captured: dict[str, Any] = {}

    class _PassResult:
        status = "pass"
        best = None

    async def _stub_loop(app, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _PassResult()

    app.state.run_design_loop = _stub_loop

    async def _call(client):
        project_id = await _create_project(client)
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(hit_point_mm=[5.0, 3.0, 7.0]),
        )
        source = app.state.event_sources.get(project_id)
        if source is not None:
            async for _event, _data in source:
                pass
        return project_id, r

    _pid, r = _run_async(app, _call)
    assert r.status_code == 202, r.text
    rt = captured.get("request", "")
    assert "Pick hit point in mm: 5, 3, 7" in rt, rt
    assert "face normal" not in rt, rt


def test_region_edit_rejects_non_unit_face_normal(app):
    """A ``face_normal`` whose length is outside 1±0.01 is rejected with
    422 (a non-unit normal is a client defect the loop cannot use)."""

    async def _call(client):
        project_id = await _create_project(client)
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(face_normal=[2.0, 0.0, 0.0]),
        )

    r = _run_async(app, _call)
    assert r.status_code == 422


def test_region_edit_rejects_non_finite_hit_point(app):
    """A non-finite ``hit_point_mm`` component is rejected (422 — the same
    wire-format discipline as ``PointLocation``). We test with a NaN string
    which pydantic will reject as a type error (422)."""

    async def _call(client):
        project_id = await _create_project(client)
        body = _region_edit_body(hit_point_mm=["NaN", 0.0, 0.0])
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=body,
        )

    r = _run_async(app, _call)
    assert r.status_code == 422, r.text


def test_region_edit_rejects_out_of_bound_hit_point(app):
    """A ``hit_point_mm`` component with |value| > 1e6 is rejected (422)
    — a 1000+ km coordinate is a client defect, not a pick."""

    async def _call(client):
        project_id = await _create_project(client)
        body = _region_edit_body(hit_point_mm=[1e7, 0.0, 0.0])
        return await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=body,
        )

    r = _run_async(app, _call)
    assert r.status_code == 422, r.text


def test_region_edit_face_normal_within_tolerance_accepted(app):
    """A ``face_normal`` with length within 1±0.01 is accepted (202)."""

    class _PassResult:
        status = "pass"
        best = None

    async def _stub_loop(app, **kwargs: Any) -> Any:
        return _PassResult()

    app.state.run_design_loop = _stub_loop

    async def _call(client):
        project_id = await _create_project(client)
        # length = sqrt(0.5^2 + 0.5^2) ≈ 0.707 — outside tolerance → 422
        r = await client.post(
            f"/api/projects/{project_id}/region-edits",
            json=_region_edit_body(face_normal=[0.999, 0.0, 0.0]),
        )
        source = app.state.event_sources.get(project_id)
        if source is not None:
            async for _event, _data in source:
                pass
        return project_id, r

    _pid, r = _run_async(app, _call)
    # 0.999 is within 1±0.01 → accepted
    assert r.status_code == 202, r.text


def test_region_edit_fill_recut_trigger_no_loop(app_with_projects, monkeypatch):
    """For a project with an imported part, the region-edit route runs the
    fill_recut trigger BEFORE any loop. On trigger: no loop runs, the
    offer is stored as pending, and the boundary sentence is the answer.
    The stub loop is spied to confirm it is NOT called."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Imported Part"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(
                instruction="make the big hole 38 mm",
                hit_point_mm=[12.0, 0.0, 20.0],
                face_normal=[0.0, 1.0, 0.0],
            ),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    pid, status, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was NOT called
    assert not loop_calls, f"the design loop was called: {loop_calls}"
    # The boundary sentence is the answer
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "That hole came with your file" in msg, msg
    assert "Ø38 mm" in msg, msg
    # The done frame surfaces the fresh offer (the SPA renders the
    # [Yes, do that] / [Leave it] buttons from this field).
    assert done[0].get("fill_recut_offer") is True, done
    # The offer was recorded server-side
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is not None, "offer not recorded"
    assert offer["kind"] == "fill_recut", offer
    assert offer["noun"] == "hole", offer
    assert offer["size"] == 38.0, offer


def test_region_edit_fill_recut_no_hole_no_offer(app_with_projects, monkeypatch):
    """Issue #351 — the region-edit seam reads the SAME stored hole fact
    as the chat route (operator decision 3): a part with
    ``hole_count: 0`` + a region edit "make the hole 10 mm" (with a face
    normal — the pick IS on the surface) gets the honest no-hole reply,
    NO offer stored, NO ``fill_recut_offer`` flag, and NO loop. The pick
    can land on a plain face too — the stored import-time fact is the
    deterministic signal, never local geometry at the hit point."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Plain Box Region"})
        pid = r.json()["id"]
        _set_part_columns(
            app_with_projects, pid, unit_status="settled", hole_count=0
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(
                instruction="make the hole 10 mm",
                hit_point_mm=[12.0, 0.0, 20.0],
                face_normal=[0.0, 1.0, 0.0],
            ),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    pid, status, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was NOT called.
    assert not loop_calls, f"the design loop was called: {loop_calls}"
    # The honest no-feature reply — NOT the boundary sentence.
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "I don't see a hole on the part you brought" in msg, msg
    assert "That hole came with your file" not in msg, msg
    # NO offer stored — and NO offer flag on the done frame.
    assert "fill_recut_offer" not in done[0], done
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is None, f"offer should not be stored, got: {offer}"


def test_region_edit_fill_recut_no_normal_degradation(app_with_projects, monkeypatch):
    """A region edit with ``hit_point_mm`` but NO ``face_normal`` that
    triggers the boundary gets NO axis-dependent offer: the reply says
    the feature came with the file + the can't-tell-axis copy, no offer
    stored, no loop runs."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Normal"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(
                instruction="make the big hole 38 mm",
                hit_point_mm=[12.0, 0.0, 20.0],
                # face_normal NOT sent
            ),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    pid, status, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was NOT called
    assert not loop_calls, f"the design loop was called: {loop_calls}"
    # The no-normal degradation reply
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    msg = done[0]["message"]
    assert "came with your file" in msg, msg
    assert "I can't tell that feature's axis from where you pointed" in msg, msg
    # NO offer stored — and the done frame must NOT carry the offer
    # discriminator either (the SPA would render [Yes, do that] /
    # [Leave it] for an offer that was never stored — the string
    # comparison the discriminator replaced did exactly that).
    assert "fill_recut_offer" not in done[0], done
    svc = _svc(app_with_projects)
    offer = svc.get_pending_offer(pid)
    assert offer is None, f"offer should not be stored, got: {offer}"


def test_region_edit_fill_recut_yes_runs_loop(app_with_projects, monkeypatch):
    """A region edit that is a clean 'yes' on a LIVE fill-recut offer runs
    the loop with the fill-and-recut instruction. The offer is cleared."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Yes Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        svc = app_with_projects.state.versions
        svc.set_pending_offer(
            pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0,
                  "axis": [0.0, 1.0, 0.0]}
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="yes"),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    _pid, status, _frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was called with the fill-and-recut instruction
    assert loop_calls, "the design loop was not called"
    rt = loop_calls[0].get("request_text", "")
    assert "Fill-and-recut:" in rt, rt
    assert "hole" in rt, rt
    assert "38" in rt, rt
    # The offer was cleared
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(_pid) is None, "offer not cleared"


def test_region_edit_fill_recut_decline_clears_offer(app_with_projects, monkeypatch):
    """A region edit that is a clean 'no' on a LIVE fill-recut offer clears
    the offer and replies quietly. No loop runs."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Flow"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        svc = app_with_projects.state.versions
        svc.set_pending_offer(
            pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="leave it"),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    pid, status, frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop was NOT called
    assert not loop_calls, f"the design loop was called: {loop_calls}"
    # The decline acknowledgement
    done = [d for e, d in frames if e == "done"]
    assert done, f"no done frame: {frames}"
    assert done[0].get("kind") == "answer", done
    assert "leaving the part as it is" in done[0]["message"], done
    # The decline reply is plain — no offer buttons.
    assert "fill_recut_offer" not in done[0], done
    # The offer was cleared
    svc = _svc(app_with_projects)
    assert svc.get_pending_offer(pid) is None, "offer not cleared"


def test_region_edit_no_part_fallthrough_runs_loop(app_with_projects, monkeypatch):
    """A region edit on a project WITHOUT a part falls through the fill-recut
    pre-route (no trigger possible) and runs the design loop normally."""
    import d33d.design_loop_events as dle_mod
    loop_calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        loop_calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Part"})
        pid = r.json()["id"]
        # A region-edit WITHOUT a part still falls through the pre-route
        # (``None`` → the route's own ``_start_loop`` seam) — a fall
        # through the UNPATCHED real ``run_design_loop_with_events`` would
        # hang this test (a no-LLM-configured project loops until
        # exhaustion), so the assertion below (the patched seam was the
        # one that ran) is what makes this a real, non-hanging check.
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="make it bigger"),
        )
        source = app_with_projects.state.event_sources.get(pid)
        frames = []
        if source is not None:
            async for event, data in source:
                frames.append((event, data))
                if event in ("done", "error"):
                    break
        return pid, r2.status_code, frames

    _pid, status, _frames = _run_async(app_with_projects, _call)
    assert status == 202, status
    # The loop WAS called via the patched seam (fallthrough — the
    # unpatched real loop would have hung this test).
    assert loop_calls, "the design loop was not called"
    rt = loop_calls[0].get("request_text", "")
    assert "make it bigger" in rt, rt


async def _drive_sse(client: AsyncClient, app: Any, pid: int) -> list[tuple[str, dict]]:
    """Drain ``GET /api/stream/{pid}`` (the real SSE route) to a terminal
    frame and return the parsed ``(event, data)`` frames. Driving the
    route — not the raw event source — runs ``d33d.streaming._stream_events``,
    whose ``finally`` is the in-flight flag's single release point."""
    resp = await client.get(f"/api/stream/{pid}")
    assert resp.status_code == 200, resp.text
    frames: list[tuple[str, dict]] = []
    event = None
    for line in resp.text.splitlines():
        if line.startswith("event: "):
            event = line[len("event: ") :].strip()
        elif line.startswith("data: ") and event is not None:
            import json as _json

            frames.append((event, _json.loads(line[len("data: ") :].strip())))
            if event in ("done", "error"):
                break
    return frames


def test_region_edit_fill_recut_preroute_failure_releases_flag(
    app_with_projects, monkeypatch
):
    """If ``fill_recut_region_edit`` raises on the region-edit route's
    pre-route, the in-flight flag (claimed before the pre-route) is
    released and re-raised — the project is not 409-blocked: a follow-up
    region-edit (or chat) proceeds without a 409."""
    from starlette.exceptions import HTTPException

    import d33d.design_loop_events as dle_mod
    import d33d.fill_recut_region as fr_mod

    def _boom(*args, **kwargs):
        # Raised only on the FIRST call (the pre-route); the follow-up
        # region-edit returns None (fall through to the normal loop path).
        raise HTTPException(status_code=500, detail="forced (test)")

    def _flaky(*args, **kwargs):
        boom_counter[0] += 1
        if boom_counter[0] == 1:
            _boom()

    boom_counter = [0]
    monkeypatch.setattr(fr_mod, "fill_recut_region_edit", _flaky)

    def _fake_loop(app, pid, **kwargs):
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Boom"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # The pre-route raises — the flag must be released on the re-raise.
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="make the big hole 38 mm"),
        )
        assert r2.status_code == 500, r2.text
        inflight = app_with_projects.state.design_loop_inflight
        assert pid not in inflight, "inflight flag leaked after pre-route failure"
        # A follow-up region-edit is NOT 409-blocked (the stub loop runs).
        r3 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="make the big hole 38 mm"),
        )
        assert r3.status_code == 202, r3.text
        return True

    _run_async(app_with_projects, _call)


def test_region_edit_fallthrough_loop_setup_failure_releases_flag(
    app_with_projects, monkeypatch
):
    """A design-loop SETUP failure on the region-edit FALL-THROUGH path
    (no part — the fill-recut pre-route returns ``None``) releases the
    in-flight flag (claimed at the top of the route) and re-raises: the
    project is not 409-blocked — a follow-up region edit is not 409."""
    from starlette.exceptions import HTTPException

    import d33d.design_loop_events as dle_mod

    def _boom(*args, **kwargs):
        # Raised on the FALL-THROUGH call (the project has no part, so
        # the pre-route returns None and the route calls the loop
        # directly). Re-raised as a 500 through the route.
        raise HTTPException(status_code=500, detail="forced (test)")

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _boom)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Part Boom"})
        pid = r.json()["id"]
        # No part columns — the fill-recut pre-route falls through (no
        # part), so the route calls the (boom) loop directly.
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="make it bigger"),
        )
        assert r2.status_code == 500, r2.text
        inflight = app_with_projects.state.design_loop_inflight
        assert pid not in inflight, "inflight flag leaked after fall-through setup failure"
        return True

    _run_async(app_with_projects, _call)

    # Now with a stub loop, a follow-up region edit is NOT 409-blocked.
    def _fake_loop(app, pid, **kwargs):
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _follow(client):
        r = await client.post("/api/projects", json={"name": "No Part OK"})
        pid = r.json()["id"]
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="make it bigger"),
        )
        assert r2.status_code == 202, r2.text
        return True

    _run_async(app_with_projects, _follow)


def test_region_edit_answer_path_released_by_stream(app_with_projects, monkeypatch):
    """Single release point (item 2): the region-edit ANSWER path
    (boundary trigger — no loop) releases the in-flight flag via the
    STREAM (``d33d.streaming._stream_events``'s ``finally``), not an
    explicit discard: drain ``GET /api/stream/{pid}`` (the real SSE
    route), then assert the flag is cleared."""
    import d33d.design_loop_events as dle_mod

    def _fake_loop(app, pid, **kwargs):
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Answer Path"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(
                instruction="make the big hole 38 mm",
                hit_point_mm=[12.0, 0.0, 20.0],
                face_normal=[0.0, 1.0, 0.0],
            ),
        )
        assert r2.status_code == 202, r2.text
        inflight = app_with_projects.state.design_loop_inflight
        # The flag is HELD while the stream is un-drained (the stream's
        # finally is the release point — not the route).
        assert pid in inflight, "flag released before the stream drained"
        frames = await _drive_sse(client, app_with_projects, pid)
        assert pid not in inflight, "flag not released by the stream drain"
        # The boundary sentence is the answer frame.
        done = [d for e, d in frames if e == "done"]
        assert done and done[0].get("kind") == "answer", frames
        assert "That hole came with your file" in done[0]["message"], done
        return True

    _run_async(app_with_projects, _call)


def test_region_edit_loop_setup_failure_releases_flag_and_restores_offer(
    app_with_projects, monkeypatch
):
    """Item 2 companion: on the region-edit accept path, a design-loop
    SETUP failure restores the offer AND releases the in-flight flag
    (the event source is not registered, so the stream's finally never
    runs — the route's own discard is the release point here)."""
    from starlette.exceptions import HTTPException

    import d33d.design_loop_events as dle_mod

    def _boom(*args, **kwargs):
        # Re-raised as a 500 through the route (the test app has no
        # handler that would convert a bare RuntimeError to a response).
        raise HTTPException(status_code=500, detail="forced (test)")

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _boom)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Lost Yes"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        svc = app_with_projects.state.versions
        svc.set_pending_offer(
            pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="yes"),
        )
        assert r2.status_code == 500, r2.text
        inflight = app_with_projects.state.design_loop_inflight
        assert pid not in inflight, "flag leaked after loop setup failure"
        offer = svc.get_pending_offer(pid)
        assert offer is not None, "the accepted offer was lost to a setup failure"
        assert offer["kind"] == "fill_recut", offer
        return r2.status_code

    _run_async(app_with_projects, _call)


def test_region_edit_accept_clear_failure_is_best_effort(
    app_with_projects, monkeypatch
):
    """On the region-edit accept path, a post-registration offer-clear
    failure is logged and swallowed (best-effort) — the response is a 202
    and the event source is registered (the loop runs). The in-flight flag
    is left alone: the stream's ``finally`` owns it from registration."""
    import d33d.design_loop_events as dle_mod
    import d33d.versions as versions_mod

    def _fake_loop(app, pid, **kwargs):
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)

    _orig_set = versions_mod.VersionService.set_pending_offer

    def _boom_set(self, project_id, offer):
        # Only the post-registration clear (offer is None) raises; the
        # setup-failure restore path (a non-None offer) is unaffected.
        if offer is None:
            raise RuntimeError("forced clear failure (test)")
        return _orig_set(self, project_id, offer)

    monkeypatch.setattr(versions_mod.VersionService, "set_pending_offer", _boom_set)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Clear Fails"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        svc = app_with_projects.state.versions
        svc.set_pending_offer(
            pid, {"kind": "fill_recut", "noun": "hole", "size": 38.0}
        )
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="yes"),
        )
        # 202 despite the clear failure — the loop runs.
        assert r2.status_code == 202, r2.text
        # The event source IS registered (the loop runs).
        source = app_with_projects.state.event_sources.get(pid)
        assert source is not None, "the event source was not registered"
        # The in-flight flag is HELD — the stream's finally owns it.
        inflight = app_with_projects.state.design_loop_inflight
        assert pid in inflight, "flag released before the stream drained"
        return True

    _run_async(app_with_projects, _call)


def test_create_app_does_not_require_catalogue_file_to_exist(app, app_paths):
    """The app starts with an empty in-memory catalogue — no
    ``models.yaml`` required on disk until the first ``PUT``. (The
    ``ModelCatalogueLoader`` accepts a path that may not yet exist; the
    initial catalogue is ``None``.)"""
    assert not app_paths["cat"].exists()


# ---------------------------------------------------------------------------
# Issue #338 fix round — fill-recut axis / except-order / unit-gate
# ---------------------------------------------------------------------------


def test_chat_accept_carrys_axis_into_instruction(app_with_projects, monkeypatch):
    """Item 1 (MEDIUM): a fill-recut offer seeded through the REGION-EDIT
    route (with a ``face_normal``) carries the pick's axis; accepting it
    through the CHAT route ("Yes, do that") must build the loop
    instruction WITH the "on the same axis" clause. Regression: the
    chat route reads the offer via ``get_pending_offer``, which dropped
    the axis, so the accepted instruction lost the clause."""
    import d33d.chat_loop as _cl
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(_cl, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Axis Chat"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        # Seed the offer through the REGION-EDIT route with a face normal
        # (the pick's normal is stored as the offer's axis).
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(
                instruction="make the big hole 38 mm",
                hit_point_mm=[12.0, 0.0, 20.0],
                face_normal=[0.0, 1.0, 0.0],
            ),
        )
        assert r2.status_code == 202, r2.text
        svc = _svc(app_with_projects)
        offer = svc.get_pending_offer(pid)
        assert offer is not None, "offer not recorded by the region route"
        assert offer.get("axis") == [0.0, 1.0, 0.0], offer
        # The boundary-trigger path left the in-flight flag held (the loop
        # stub is lazy and never starts, so the stream's finally never runs)
        # — release it so the CHAT acceptance is not 409-blocked.
        inflight = app_with_projects.state.design_loop_inflight
        if inflight is not None:
            inflight.discard(pid)
        # Accept through the CHAT route (decision 7: Yes goes via chat).
        r3 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "Yes, do that"}
        )
        assert r3.status_code == 202, r3.text
        # Drain the event source — the stub _fake_loop is a lazy async
        # generator; calls.append fires when the generator starts.
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break
        return pid

    _pid = _run_async(app_with_projects, _call)
    assert calls, "the design loop was not called on chat acceptance"
    rt = calls[0].get("request_text", "")
    assert "Fill-and-recut:" in rt, rt
    assert "hole" in rt, rt
    # The accepted instruction carries the axis clause (the face normal).
    assert "axis 0, 1, 0" in rt, rt


def test_chat_accept_corrupt_axis_dropped_no_crash(app_with_projects, monkeypatch):
    """Item 1 (MEDIUM, security): a stored fill-recut offer whose axis is
    CORRUPT (non-finite component) reads back WITHOUT an axis and the
    offer still returns — the accepted instruction is built without the
    axis clause (no crash, no malformed axis leaked)."""
    import json as _json

    import d33d.chat_loop as _cl
    import d33d.design_loop_events as dle_mod

    calls: list[dict[str, Any]] = []

    async def _fake_loop(app, pid, **kwargs):
        calls.append(kwargs)
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _fake_loop)
    monkeypatch.setattr(_cl, "run_design_loop_with_events", _fake_loop)
    app_with_projects.state.answer_question = None

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Corrupt Axis"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        conn = app_with_projects.state.conn
        # Store a fill-recut offer with a NON-FINITE axis (NaN is valid
        # JSON; the reader must reject it).
        conn.raw.execute(
            "UPDATE projects SET pending_offer = ? WHERE id = ?",
            (
                _json.dumps(
                    {
                        "kind": "fill_recut",
                        "noun": "hole",
                        "size": 38.0,
                        "axis": [float("nan"), 0.0, 0.0],
                    }
                ),
                pid,
            ),
        )
        conn.commit()
        # Read back: the offer returns (noun/size) but WITHOUT the axis.
        svc = _svc(app_with_projects)
        offer = svc.get_pending_offer(pid)
        assert offer is not None, "the offer must still be returned"
        assert offer["kind"] == "fill_recut", offer
        assert offer["noun"] == "hole", offer
        assert offer["size"] == 38.0, offer
        assert "axis" not in offer, f"corrupt axis leaked: {offer}"
        # Accept via chat: builds an instruction with no axis clause.
        r3 = await client.post(
            f"/api/projects/{pid}/chat", json={"message": "Yes, do that"}
        )
        assert r3.status_code == 202, r3.text
        source = app_with_projects.state.event_sources.get(pid)
        if source is not None:
            async for _event, _data in source:
                if _event in ("done", "error"):
                    break
        return pid

    _pid = _run_async(app_with_projects, _call)
    assert calls, "the design loop was not called on chat acceptance"
    rt = calls[0].get("request_text", "")
    assert "Fill-and-recut:" in rt, rt
    assert "(axis" not in rt, f"corrupt axis leaked into the instruction: {rt}"


def test_region_accept_restore_failure_releases_flag_and_propagates(
    app_with_projects, monkeypatch
):
    """Item 2 (MEDIUM): on the region-edit accept path, if the offer
    RESTORE itself raises during a design-loop setup failure, the
    in-flight flag is STILL released (it is discarded FIRST) and the
    ORIGINAL loop-setup exception propagates (the restore failure is
    logged, not the one that surfaces)."""
    import d33d.design_loop_events as dle_mod
    from d33d.versions import VersionService as _VersionService

    def _boom(*args, **kwargs):
        raise RuntimeError("forced loop setup failure")

    monkeypatch.setattr(dle_mod, "run_design_loop_with_events", _boom)

    calls: list[int] = []

    def _flaky_set_pending_offer(self, pid, offer):
        calls.append(pid)
        raise RuntimeError("forced: restore (set_pending_offer) failed")

    monkeypatch.setattr(_VersionService, "set_pending_offer", _flaky_set_pending_offer)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "Restore Fail"})
        pid = r.json()["id"]
        _set_part_columns(app_with_projects, pid, unit_status="settled")
        app_with_projects.state.conn.raw.execute(
            "UPDATE projects SET pending_offer = ? WHERE id = ?",
            ('{"kind": "fill_recut", "noun": "hole", "size": 38.0}', pid),
        )
        app_with_projects.state.conn.commit()
        r2 = await client.post(
            f"/api/projects/{pid}/region-edits",
            json=_region_edit_body(instruction="yes"),
        )
        # The ORIGINAL loop-setup exception (RuntimeError) surfaces — the
        # restore failure is NOT the one that propagates. With no handler
        # converting RuntimeError to a 500 response, the ASGI transport
        # surfaces the original exception here.
        inflight = app_with_projects.state.design_loop_inflight
        assert pid not in inflight, "flag leaked (restore failure masked the release)"
        return r2.status_code

    try:
        _run_async(app_with_projects, _call)
        raised = None
    except RuntimeError as exc:
        raised = str(exc)
    assert raised is not None, "the original setup exception did not propagate"
    assert "forced loop setup failure" in raised, (
        f"expected the ORIGINAL loop-setup error, got: {raised}"
    )
    # The restore was ATTEMPTED exactly once (the flaky setter fired).
    assert len(calls) == 1, f"expected exactly one restore attempt, got {len(calls)}"


def test_region_edit_no_part_preroute_returns_none(app_with_projects, monkeypatch):
    """Item 3 (MEDIUM): the unit-status gate lives in ONE place —
    ``fill_recut_region_edit``. ``region_edit_preroute`` no longer
    re-checks it: a project WITHOUT an imported part makes the decision
    return ``None`` (the gate), so ``region_edit_preroute`` returns
    ``None`` (fall through to the design loop)."""
    import d33d.fill_recut_region as fr_mod

    async def _fake_loop(app, pid, **kwargs):
        yield ("progress", {"step": "design-loop-start"})
        yield ("done", {"message": "ok", "kind": "loop_done"})

    seen: dict[str, Any] = {}

    def _spied(app, project_id, instruction, face_normal=None, row=None, part=None):
        seen["part"] = part

    monkeypatch.setattr(fr_mod, "fill_recut_region_edit", _spied)

    async def _call(client):
        r = await client.post("/api/projects", json={"name": "No Part Gate"})
        pid = r.json()["id"]
        return pid

    _pid = _run_async(app_with_projects, _call)
    # Call the pre-route directly with part=None (no part) → None.
    result = fr_mod.region_edit_preroute(
        app_with_projects,
        _pid,
        row=None,
        part=None,
        instruction="make it bigger",
        face_normal=None,
        start_loop=_fake_loop,
        loop_kwargs={},
        inflight=set(),
    )
    assert result is None, "preroute must return None (fall through) when no part"
    assert "part" in seen, "the decision was not reached for the no-part project"
    assert seen["part"] is None


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
# GLB it needs for resolvePointPick. The route delegates to
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

        slow_resp, fast_resp = await asyncio.gather(_slow_request(), _fast_request())
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


# ---------------------------------------------------------------------------
# GET /api/config/envelope
# ---------------------------------------------------------------------------


def test_envelope_route_returns_values_matching_named_constant(app):
    """Wire x/y/z equal ``QIDI_PLUS_5_ENVELOPE_MM`` element-for-element —
    the primary drift guard between the route and the named constant."""

    async def _call(client):
        return await client.get("/api/config/envelope")

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert (body["x"], body["y"], body["z"]) == pv.QIDI_PLUS_5_ENVELOPE_MM


def test_envelope_route_wire_shape_is_five_flat_keys(app):
    """Response has exactly ``{x, y, z, unit, verified}`` — no nesting,
    no extra keys — with ``unit == "mm"`` and ``verified`` True (the
    QIDI Plus 5 envelope was verified per issue #134, so the flag's
    current value is ``True``)."""

    async def _call(client):
        return await client.get("/api/config/envelope")

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"x", "y", "z", "unit", "verified"}
    assert body["unit"] == "mm"
    assert body["verified"] is True


def test_envelope_route_returns_verified_true(app):
    """The wire's ``verified`` is ``True`` after the issue #134 flag flip,
    with the dimensions unchanged — the operator-approved verification of
    the QIDI Plus 5 build envelope. The module-flag mirror test above
    keeps the two in lock-step; this one pins the approved value itself
    so a silent flip back to ``False`` fails loudly here."""

    async def _call(client):
        return await client.get("/api/config/envelope")

    r = _run_async(app, _call)
    assert r.status_code == 200
    body = r.json()
    assert (body["x"], body["y"], body["z"], body["unit"]) == (320, 320, 300, "mm")
    assert body["verified"] is True


def test_envelope_route_verified_reads_module_flag(app):
    """The wire's ``verified`` equals the module flag, so the operator's
    future one-line flip propagates automatically."""

    async def _call(client):
        return await client.get("/api/config/envelope")

    r = _run_async(app, _call)
    body = r.json()
    assert body["verified"] == pv.QIDI_PLUS_5_ENVELOPE_VERIFIED


def test_envelope_route_source_reads_named_constant(app):
    """Source invariant (mirrors
    ``test_keep_out_source_invariant_reads_named_constant``):
    the handler bound to ``/api/config/envelope`` references the constant
    NAME, so nobody can swap in literals at the route site."""
    import inspect

    handler = next(
        route.endpoint for route in app.routes if route.path == "/api/config/envelope"
    )
    src = inspect.getsource(handler)
    assert "QIDI_PLUS_5_ENVELOPE_MM" in src
    assert "QIDI_PLUS_5_ENVELOPE_VERIFIED" in src


def test_envelope_verification_basis_still_in_module_source():
    """The ``True`` flag must carry its evidence: read the module source
    and confirm the comment records (a) that verification rests on the
    shipped slicer profile / vendor documentation, not a physical
    measurement, and (b) the unpublished-usable-Z caveat. If a future
    change strips or rewords the basis while the flag stays ``True``,
    this fails."""
    src = Path(pv.__file__).read_text(encoding="utf-8")
    assert "X-Plus 5" in src  # the shipped profile's model_id, per issue #134
    assert "320×320×300" in src
    assert "physical measurement" in src
    assert "usable-Z" in src
