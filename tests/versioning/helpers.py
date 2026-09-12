"""Shared test helpers for the issue #8 versioning tests.

The async-app driver (``run_async``) and the small git/DB probe helpers are
defined once here so a test-file change to the helper is one edit, not
seven copy-pasted ones.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient


def run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


async def create_project(client: AsyncClient, name: str = "test project") -> dict:
    """Create a project via the API; returns the full project row."""
    r = await client.post("/api/projects", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


async def create_version(client: AsyncClient, project_id: int, params: dict, **kw: Any) -> dict:
    """Create a version via the API; returns the version row."""
    body: dict[str, Any] = {"params": params}
    body.update(kw)
    r = await client.post(f"/api/projects/{project_id}/versions", json=body)
    assert r.status_code == 201, r.text
    return r.json()


def repo_path_for(app, project_id: int) -> Path:
    """The on-disk repo path (server-internal — the API masks it)."""
    for p in app.state.conn.list_projects():
        if p["id"] == project_id:
            return Path(p["git_repo_path"])
    raise AssertionError(f"project {project_id} not found")


def git_log(repo_dir: Path) -> list[str]:
    """The repo's commit subjects, one per line (newest first)."""
    cmd = ["git", "-C", str(repo_dir), "log", "--format=%s"]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if result.returncode != 0:
        raise RuntimeError(f"git log failed: {result.stderr}")
    return [l for l in result.stdout.strip().split("\n") if l]


def git_log_lines(repo_dir: Path) -> list[str]:
    """Commit subjects, one per line (empty list for an empty repo)."""
    cmd = ["git", "-C", str(repo_dir), "log", "--format=%s"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"git log failed: {r.stderr}")
    return [l for l in r.stdout.strip().split("\n") if l]


def git_rev_count(repo_dir: Path) -> int:
    """The repo's commit count (0 for an empty repo)."""
    cmd = ["git", "-C", str(repo_dir), "rev-list", "--count", "HEAD"]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=30, check=False)
    if r.returncode != 0:
        # An empty repo (no commits yet) reports rc=128 with no output —
        # that means zero commits, not a failure.
        if r.returncode == 128 and "remote HEAD" not in r.stderr:
            return 0
        raise RuntimeError(f"git rev-list failed: {r.stderr}")
    return int(r.stdout.strip())
