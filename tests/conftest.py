"""Pytest conftest: ensure the worktree ``d33d`` package resolves.

Imports ``tests/__init__`` (the test-package init), which runs the
dependency and provenance guards at import time — before any test is
collected. See tests/__init__.py for the guard logic and rationale.

Also inserts the repository root (the parent of this ``tests/``
directory) at the front of ``sys.path`` so ``import d33d`` always finds
the package under test, even when the installed ``d33d`` egg-link points
elsewhere (e.g. a stale editable install or a parallel worktree). This
keeps ``./scripts/test`` green on a fresh checkout without Docker.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

# Running the import of the tests package triggers the guard checks in
# tests/__init__.py (dependency + provenance). If either guard fails it
# calls sys.exit() with a diagnostic and the pytest session aborts
# before collecting a single test.
import tests  # noqa: F401  (side-effect: guard checks)

_REPO_ROOT = Path(__file__).resolve().parents[1]
root_str = str(_REPO_ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)


def _count_projects_dirs() -> int:
    p = Path.home() / ".d33d" / "projects"
    if p.is_dir():
        return len(list(p.iterdir()))
    return 0


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point ``D33D_DATA_DIR`` at a per-test tmp dir and clear ``db.APP_DATA_DIR``.

    After #294, ``d33d.db._default_git_path`` resolves ``projects_dir()``
    to ``$D33D_DATA_DIR`` (default ``~/.d33d``) whenever ``APP_DATA_DIR``
    is None, so ``create_project`` calls in tests were creating empty
    directories in the operator's real ``~/.d33d/projects/`` on every
    run. Steer every test at its own ``tmp_path`` and clear the
    module-level ``APP_DATA_DIR`` so a value recorded by an earlier app
    lifespan can't leak between tests.
    """
    import d33d.db as db_mod

    data_dir = tmp_path / "d33d-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("D33D_DATA_DIR", str(data_dir))
    monkeypatch.setattr(db_mod, "APP_DATA_DIR", None)
    yield


@pytest.fixture(autouse=True, scope="session")
def _home_d33d_projects_guard():
    """Session guard: tests must not create entries in ``~/.d33d/projects``.

    Records the entry count at session start (zero when the directory is
    absent) and asserts it is unchanged at session end. Fails loudly
    naming this issue when the count grows; passes when the directory is
    absent or re-appears with the same entry count as at session start.
    """
    projects_dir = Path.home() / ".d33d" / "projects"
    count = _count_projects_dirs()
    yield
    now_count = _count_projects_dirs()
    assert now_count == count, (
        f"tests created {now_count - count} new directorie(s) in the operator's "
        f"real {projects_dir} — tests must isolate D33D_DATA_DIR "
        f"(see the autouse _isolate_data_dir fixture in tests/conftest.py). "
        f"Entries before: {count}, after: {now_count}."
    )
