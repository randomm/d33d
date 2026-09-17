"""Live e2e suite (issue #108) — conftest.

FAILS, never skips: a silently-skipped live suite is indistinguishable
from a passing one — the exact defect class this ticket exists to remove
(seven integration defects shipped this week were all invisible to the
~1130 passing fast tests). Prerequisites are checked in
``pytest_runtest_setup`` — a public hook that fires per-test, AFTER
marker deselection and immediately before a test runs, so a fast run
(``-m "not slow and not live"``) that deselects every ``live`` test
never checks — and a missing prerequisite raises
:class:`LivePrerequisiteError`, an error at setup, so ``pytest -m live``
exits NON-ZERO (never 0, never a green skip). The fast suite carries a
hermetic meta-test that monkeypatches this check and asserts the named
error is raised (see ``tests/test_live_fail_not_skip.py``), plus a
subprocess check that the real exit code is non-zero.

Prerequisites (all three required, named in the failure message):
- Docker is invocable (the real render worker is a Docker container).
- The endpoint key ``TRAIL_OPENERS_LLM_KEY`` is set in the environment
  (models.yaml references ``${TRAIL_OPENERS_LLM_KEY}``; the key is
  never committed).
- The catalogue is resolvable: ``models.yaml`` at the repo root holds a
  ``design`` role (the model is configured, never hardcoded).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from pathlib import Path
from typing import Self

import pytest

# tests/live_e2e/ is its own directory (no __init__.py), so the repo
# root is parents[2].
REPO_ROOT = Path(__file__).resolve().parents[2]


def _catalogue_path() -> Path:
    """The real catalogue: ``models.yaml`` at THIS checkout's root
    (untracked — it holds no secret, only a ``${TRAIL_OPENERS_LLM_KEY}``
    reference). The operator keeps it at the repo root they run the
    suite from; worktrees cut from origin/main do not carry it, so a
    worktree run must place it at the worktree root first."""
    return REPO_ROOT / "models.yaml"


# Session wall-clock budget: each case gets the full budget (one
# case's design call + up to 3 iterations x 8 sequential openscad
# invocations); a single case exceeding it is a hard failure (a hung
# render or an unresponsive endpoint must surface, not burn the whole
# run). Tens of seconds per case is the norm (8 sequential openscad
# invocations per render, never parallel).
CASE_TIMEOUT_S = 600.0


class LivePrerequisiteError(RuntimeError):
    """A live-suite prerequisite is missing. Raised in
    ``pytest_runtest_setup`` so the run FAILS with a message naming what
    is missing — never a skip, never a silent pass."""


def check_live_prerequisites(
    *,
    docker_check=None,
    env: dict[str, str] | None = None,
    catalogue_path: str | Path | None = None,
) -> None:
    """Verify the three live-suite prerequisites; raise
    :class:`LivePrerequisiteError` naming the first missing one.

    ``docker_check`` / ``env`` / ``catalogue_path`` are injectable seams
    (the hermetic meta-test monkeypatches them); production callers
    pass nothing and the real checks run.
    """
    if docker_check is None:

        def docker_check() -> bool:
            if shutil.which("docker") is None:
                return False
            try:
                proc = subprocess.run(
                    ["docker", "version", "--format", "client"],
                    capture_output=True,
                    check=False,
                    timeout=15,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            return proc.returncode == 0

    if not docker_check():
        raise LivePrerequisiteError(
            "docker is not available: the live suite drives the real render "
            "worker (a Docker container). Start Docker (on macOS: Docker "
            "Desktop running) or run on the target box."
        )

    env = env if env is not None else dict(os.environ)
    if not env.get("TRAIL_OPENERS_LLM_KEY"):
        raise LivePrerequisiteError(
            "TRAIL_OPENERS_LLM_KEY is not set: the live suite sends real "
            "requests to the endpoint in models.yaml (the catalogue's "
            "provider key is ${TRAIL_OPENERS_LLM_KEY}). Export the key; "
            "it must never be committed."
        )

    catalogue = Path(catalogue_path) if catalogue_path else _catalogue_path()
    if not catalogue.is_file():
        raise LivePrerequisiteError(
            f"models.yaml not found at {catalogue}: the live suite resolves "
            "the design role from the real catalogue (the model is "
            "configured, never hardcoded)."
        )

    from d33d.config.catalogue import CatalogueError, load_catalogue
    from d33d.config.resolve import resolve_model

    try:
        cat = load_catalogue(catalogue)
        resolve_model(cat, "design")
    except (CatalogueError, LookupError, ValueError) as e:
        raise LivePrerequisiteError(
            f"the models.yaml catalogue at {catalogue} does not resolve a "
            f"design role: {e}"
        ) from e


def pytest_runtest_setup(item: pytest.Item) -> None:
    """Per-test prerequisite check — FAIL, not skip, but ONLY for
    ``live`` tests that actually run.

    ``pytest_runtest_setup`` is a public hook that fires per-test, AFTER
    marker deselection and immediately before a test executes. A fast
    run (``pytest -m "not slow and not live"`` — CI, which has no Docker
    and no ``TRAIL_OPENERS_LLM_KEY`` secret) deselects every ``live``
    test, so no live item ever reaches setup and the check never runs.
    When a ``live`` test is selected (``pytest -m live``), the check runs
    in setup and a missing prerequisite raises
    :class:`LivePrerequisiteError`, an error: a non-zero exit naming
    what is missing — never a skip, never a silent pass. The fast
    suite's hermetic meta-test (``tests/test_live_fail_not_skip.py``)
    and its subprocess exit-code test pin both halves of that contract.

    Gating on ``"live" in item.keywords`` here is safe — unlike
    ``pytest_collection_modifyitems`` (which pytest 9.x invokes BEFORE
    its own ``deselect_by_mark`` step, so it would see the live tests of
    a fast run still *pending* deselection), setup only fires for items
    that survived selection.
    """
    if "live" in item.keywords:
        check_live_prerequisites()


# ---------------------------------------------------------------------------
# Session runtime tracking (per-case and total, printed to STDOUT)
# ---------------------------------------------------------------------------

_RUNTIMES: list[tuple[str, float]] = []


class CaseTimer:
    """A context manager that times one live case's wall-clock runtime."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._start = 0.0

    def __enter__(self) -> Self:
        self._start = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> bool:
        _RUNTIMES.append((self._name, time.monotonic() - self._start))
        return False


def case_timer(case_id: str) -> CaseTimer:
    """Time one live case's wall-clock runtime (reported at session end)."""
    return CaseTimer(case_id)


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    """Print per-case and total runtime to STDOUT (the issue's "runtime
    is reported per case and in total"). One line per case
    (``case_id  <seconds>s``), then a ``TOTAL`` line — a stable format
    that can be asserted/budgeted against later."""
    if not _RUNTIMES:
        return
    print("\n=== live e2e runtime (issue #108) ===")
    for name, secs in _RUNTIMES:
        print(f"  {name}: {secs:.1f}s")
    print(f"  TOTAL: {sum(secs for _, secs in _RUNTIMES):.1f}s")
