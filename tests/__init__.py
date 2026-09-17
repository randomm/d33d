"""Test-package init: run the dependency + provenance guards.

Both guards run at IMPORT TIME, which happens before pytest collects a
single test — i.e. even when pytest itself is the wrong binary.

Why here and not in a test file:
- A guard written as a test function cannot protect against the wrong-pytest
  failure mode; if pytest lacks the project's deps the guard's test is in
  the modules it can't import and never executes.
- `tests/` is imported by pytest on every run (conftest discovery walks
  it) and the guard module itself uses only the standard library, so the
  import succeeds under any interpreter and any pytest version.

Why here and not in tests/conftest.py:
- conftest.py is only loaded by pytest itself. Running the guards from a
  plain `python -c "import tests"` (which the committed regression test
  does) would not trigger them unless we went through pytest.
- The guard's own failure mode is "pytest is the wrong binary"; the guard
  must therefore not depend on pytest.
"""

from __future__ import annotations

import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# Dependency guard
# ---------------------------------------------------------------------------
# The project's runtime dependencies live in pyproject.toml [project].
# If this interpreter can't import them it is the wrong interpreter
# (e.g. system Python 3.14 with none of the project deps, as opposed to
# the project venv with everything present). Fail loudly, naming what is
# missing, BEFORE any test is collected.

_REQUIRED_PACKAGES = (
    "d33d",  # the project package itself — editable install present
    "pydantic",
    "trimesh",
    "cryptography",
    "fastapi",
    "yaml",  # PyYAML
    "networkx",
)


def _check_dependencies() -> None:
    missing = []
    for name in _REQUIRED_PACKAGES:
        try:
            __import__(name)
        except ImportError:
            missing.append(name)
    if missing:
        sys.stderr.write(
            "GATE FAILED (dependency guard): required package(s) not importable "
            "by the current interpreter: "
            f"{', '.join(missing)}.\n"
            f"  interpreter: {sys.executable}\n"
            "  This interpreter is missing the project's dependencies.\n"
            "  Run the gate as: `./scripts/test` (or `uv run pytest -m "
            '"not slow"`) from a checkout that has the project deps installed.\n'
        )
        sys.exit(3)


_check_dependencies()

# ---------------------------------------------------------------------------
# Provenance guard
# ---------------------------------------------------------------------------
# The `d33d` package that this run will import MUST be the one under the
# working tree that pytest was invoked in. Otherwise we are testing a
# different source tree than the one about to be merged — e.g. a stale
# editable install, an inherited venv, or a worktree whose sys.path[0]
# points at the main checkout's `d33d/`.
#
# We check that d33d.__file__ is a *descendant* of the repo root (the
# parent of this tests/ directory), not merely that it starts with the
# same prefix, so a sibling tree at the same filesystem depth cannot slip
# through a naive prefix check.

import d33d as _d33d  # noqa: E402

_THIS_TESTS_DIR = Path(__file__).resolve().parent
_REPO_ROOT = _THIS_TESTS_DIR.parent
_D33D_PATH = Path(_d33d.__file__).resolve()


def _is_within(child: Path, parent: Path) -> bool:
    """True if `child` is the same as, or a descendant of, `parent`."""
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _check_provenance() -> None:
    if not _is_within(_D33D_PATH, _REPO_ROOT):
        sys.stderr.write(
            "GATE FAILED (provenance guard): d33d resolves to a different "
            "source tree than the one being tested.\n"
            f"  expected inside: {_REPO_ROOT}\n"
            f"  resolved to:     {_D33D_PATH}\n"
            "  This run is measuring a foreign source tree. The result is "
            "meaningless.\n"
            "  Fix: ensure the venv / install points at the worktree you "
            "are testing, or run the gate from the correct tree.\n"
        )
        sys.exit(4)


_check_provenance()

# Expose for the regression test in tests/test_test_gate.py.
TESTS_DIR = _THIS_TESTS_DIR
REPO_ROOT = _REPO_ROOT
D33D_FILE = _D33D_PATH
REQUIRED_PACKAGES = _REQUIRED_PACKAGES
