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

# Running the import of the tests package triggers the guard checks in
# tests/__init__.py (dependency + provenance). If either guard fails it
# calls sys.exit() with a diagnostic and the pytest session aborts
# before collecting a single test.
import tests  # noqa: F401  (side-effect: guard checks)

_REPO_ROOT = Path(__file__).resolve().parents[1]
root_str = str(_REPO_ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
