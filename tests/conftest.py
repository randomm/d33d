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


def _project_entry_names() -> set[str]:
    p = Path.home() / ".d33d" / "projects"
    if p.is_dir():
        return {e.name for e in p.iterdir()}
    return set()


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

    The tmp dir is NOT pre-created: ``projects_dir()`` creates its
    ``projects/`` child with ``mkdir(parents=True)`` on demand, and
    pre-creating ``d33d-data`` would collide with a test's own ``data_dir``
    fixture that mkdir's the same ``tmp_path`` path (``pytest`` shares one
    ``tmp_path`` per test across fixtures; a bare ``mkdir`` then raises
    FileExistsError).
    """
    import d33d.db as db_mod

    data_dir = tmp_path / "d33d-data"
    monkeypatch.setenv("D33D_DATA_DIR", str(data_dir))
    monkeypatch.setattr(db_mod, "APP_DATA_DIR", None)
    yield


@pytest.fixture(autouse=True, scope="session")
def _home_d33d_projects_guard():
    """Session guard: tests must not create entries in ``~/.d33d/projects``.

    Snapshots the entry NAMES at session start and asserts that no NEW
    entries (names absent from the snapshot) exist at session end. A raw
    count comparison would flake on operator activity on this same machine
    — the dev server creates real projects (and users delete them) during
    the session — so only growth caused by a name that was not present at
    session start is reported, and the message says so.
    """
    projects_dir = Path.home() / ".d33d" / "projects"
    before = _project_entry_names()
    yield
    new = _project_entry_names() - before
    assert not new, (
        f"tests created {len(new)} new directorie(s) in the operator's "
        f"real {projects_dir} ({sorted(new)[:5]}{' …' if len(new) > 5 else ''}) — "
        f"tests must isolate D33D_DATA_DIR "
        f"(see the autouse _isolate_data_dir fixture in tests/conftest.py). "
        f"Note: a live dev server on this machine creating real projects would "
        f"also trip this; pause it when running the suite."
    )
