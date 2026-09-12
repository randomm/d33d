"""Shared fixtures for the issue #8 versioning tests.

Every versioning test file previously carried an identical ``app_paths`` /
``app_with_versions`` pair (the ``db_mod._default_git_path`` monkeypatch is
essential to the fixture, not a helper) — this is the single home for them
so a new app state variable is added once, not in seven files.

Async/DB/git helpers live in ``tests/versioning/helpers.py`` (imported as
``from .helpers import run_async, ...`` in the test files).
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    """Isolated DB + master-key + catalogue paths under tmp_path."""
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_versions(app_paths: dict[str, Path], tmp_path: Path):
    """A ``create_app`` instance with the versions router (mounted by the
    factory) and the default git path pointed at ``tmp_path`` so the
    per-project repos are cleaned up by pytest."""
    import d33d.db as db_mod

    original_default = db_mod._default_git_path

    def _tmp_default_git_path(name: str) -> str:
        import uuid

        slug = uuid.uuid4().hex[:12]
        base = tmp_path / "repos" / slug
        base.mkdir(parents=True, exist_ok=True)
        return str(base)

    db_mod._default_git_path = _tmp_default_git_path

    from d33d.app import create_app

    app = create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )
    yield app
    db_mod._default_git_path = original_default
