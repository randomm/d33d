"""SPA static-file serving tests (issue #6, D7).

Covers:
- ``web/dist`` exists → ``GET /`` serves the built SPA's ``index.html``
  (via FastAPI's ``StaticFiles``, not :data:`d33d.app.STUB_HTML`).
- Existing ``/api/*`` routes (e.g. ``GET /api/config/models``) are NOT
  shadowed by the static mount — API routers are registered before the
  mount and Starlette matches routes in registration order (see
  ``d33d/app.py``'s ``create_app`` docstring for the ordering contract).
- ``web/dist`` does NOT exist → the app still starts (no crash) and falls
  back to serving :data:`d33d.app.STUB_HTML` at ``/``.

Same harness as ``tests/test_app.py``: httpx ``ASGITransport`` in-process,
no live server, no port 8080 bound, no ``pytest-asyncio`` (project's sync
test style driving the coroutine via ``asyncio.run``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import STUB_HTML, create_app


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under ``tmp_path``."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def fake_spa_dist(tmp_path: Path) -> Path:
    """A minimal fake ``web/dist`` (Vite build output) — an ``index.html``
    plus a hashed asset file, matching Vite's real output shape closely
    enough to exercise ``StaticFiles(html=True)``."""
    dist = tmp_path / "dist"
    assets = dist / "assets"
    assets.mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>d33d SPA build</body></html>"
    )
    (assets / "index-abc123.js").write_text("console.log('spa');")
    return dist


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan
    (mirrors ``tests/test_app.py``'s harness)."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


# ---------------------------------------------------------------------------
# web/dist exists → serve the real SPA build
# ---------------------------------------------------------------------------


def test_serves_built_spa_index_when_dist_exists(
    app_paths: dict[str, Path], fake_spa_dist: Path
):
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=fake_spa_dist,
    )

    async def _call(client):
        return await client.get("/")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/html")
    assert "d33d SPA build" in r.text
    # It is NOT the stub fallback.
    assert r.text != STUB_HTML


def test_serves_spa_hashed_asset_when_dist_exists(
    app_paths: dict[str, Path], fake_spa_dist: Path
):
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=fake_spa_dist,
    )

    async def _call(client):
        return await client.get("/assets/index-abc123.js")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert "console.log" in r.text


def test_api_routes_not_shadowed_by_static_mount(
    app_paths: dict[str, Path], fake_spa_dist: Path
):
    """The static mount is registered last; ``/api/config/models`` (an
    already-registered API route) must still resolve to the API handler,
    not fall through to the SPA's ``index.html`` fallback."""
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=fake_spa_dist,
    )

    async def _call(client):
        return await client.get("/api/config/models")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    body = r.json()
    # Shape from d33d/app.py's get_models(): providers/models/roles keys —
    # NOT the SPA's HTML fallback.
    assert set(body.keys()) >= {"source", "providers", "models", "roles"}


def test_other_api_route_also_not_shadowed(
    app_paths: dict[str, Path], fake_spa_dist: Path
):
    """A second, sibling-workstream API route (``/api/settings/credentials``,
    from ``projects.py``/``app.py``'s own router) is likewise unaffected."""
    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=fake_spa_dist,
    )

    async def _call(client):
        return await client.get("/api/settings/credentials")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/json")
    assert isinstance(r.json(), list)


# ---------------------------------------------------------------------------
# web/dist does NOT exist → graceful stub fallback, no crash
# ---------------------------------------------------------------------------


def test_falls_back_to_stub_when_dist_missing(
    app_paths: dict[str, Path], tmp_path: Path
):
    """``spa_dist_dir`` pointing at a nonexistent path must not raise at
    ``create_app`` time (``StaticFiles`` mounting is skipped entirely) and
    ``/`` must still serve the stub page."""
    missing_dist = tmp_path / "does-not-exist" / "dist"
    assert not missing_dist.exists()

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=missing_dist,
    )

    async def _call(client):
        return await client.get("/")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.text == STUB_HTML


def test_api_routes_still_work_when_dist_missing(
    app_paths: dict[str, Path], tmp_path: Path
):
    """API routes are unaffected by the fallback path either."""
    missing_dist = tmp_path / "does-not-exist" / "dist"

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=missing_dist,
    )

    async def _call(client):
        return await client.get("/api/config/models")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.json()["providers"] == {}


def test_dist_dir_present_but_missing_index_html_falls_back_to_stub(
    app_paths: dict[str, Path], tmp_path: Path
):
    """A ``dist`` directory that exists but has no ``index.html`` (e.g. an
    interrupted/partial build) must not be treated as a valid SPA build —
    falls back to the stub rather than mounting an index-less static dir."""
    partial_dist = tmp_path / "partial-dist"
    partial_dist.mkdir()
    (partial_dist / "assets").mkdir()

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
        spa_dist_dir=partial_dist,
    )

    async def _call(client):
        return await client.get("/")

    r = _run_async(app, _call)
    assert r.status_code == 200
    assert r.text == STUB_HTML
