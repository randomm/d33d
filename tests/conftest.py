"""Pytest conftest: ensure the worktree ``d33d`` package resolves.

Inserts the repository root (the parent of this ``tests/`` directory) at
the front of ``sys.path`` so ``import d33d`` always finds the package
under test, even when the installed ``d33d`` egg-link points elsewhere
(e.g. a stale editable install or a parallel worktree). This keeps
``pytest -m "not slow"`` green on a fresh checkout without Docker.
"""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
root_str = str(_REPO_ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)
