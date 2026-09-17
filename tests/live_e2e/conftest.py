"""Live e2e suite (issue #108) — conftest.

FAILS, never skips: a silently-skipped live suite is indistinguishable
from a passing one — the exact defect class this ticket exists to remove
(seven integration defects shipped this week were all invisible to the
~1130 passing fast tests). Prerequisites are checked in
``pytest_configure`` — BEFORE any test is collected or runs — and a
missing prerequisite raises :class:`LivePrerequisiteError`, a
collection error, so ``pytest -m live`` exits NON-ZERO (never 0, never
a green skip). The fast suite carries a hermetic meta-test that
monkeypatches this check and asserts the named error is raised (see
``tests/evals/test_live_fail_not_skip.py``), plus a subprocess check
that the real exit code is non-zero.

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
    """The real catalogue. The operator maintains models.yaml at the main
    checkout (untracked — it holds no secret, only a
    ``${TRAIL_OPENERS_LLM_KEY}`` reference); worktrees cut from
    origin/main do not carry it, so resolve the main-checkout path first,
    falling back to the worktree root for a non-worktree checkout."""
    main = Path("/Users/janni/projects/d33d/models.yaml")
    return main if main.is_file() else REPO_ROOT / "models.yaml"


# Session wall-clock budget: each case gets the full budget (one
# case's design call + up to 3 iterations x 8 sequential openscad
# invocations); a single case exceeding it is a hard failure (a hung
# render or an unresponsive endpoint must surface, not burn the whole
# run). Tens of seconds per case is the norm (8 sequential openscad
# invocations per render, never parallel).
CASE_TIMEOUT_S = 600.0


class LivePrerequisiteError(RuntimeError):
    """A live-suite prerequisite is missing. Raised at collection time
    so the run FAILS with a message naming what is missing — never a
    skip, never a silent pass."""


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


def pytest_configure(config: pytest.Config) -> None:
    """Collection-time prerequisite check — FAIL, not skip.

    ``pytest_configure`` runs before collection, so a raised
    :class:`LivePrerequisiteError` aborts the whole invocation with a
    non-zero exit code and a message naming what is missing. The check
    is deliberately NOT gated on the ``live`` marker (the marker filter
    applies to test selection, which happens later) — gating on the
    marker would let ``pytest -m live`` skip the check when the
    expression excludes the marker, which is precisely the silent
    failure being guarded against.
    """
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
