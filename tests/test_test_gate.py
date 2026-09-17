"""Regression tests for the deterministic test gate (issue #145).

These tests prove the two guards actually refuse — not merely that the
right invocation works:

1. Dependency guard — simulates the wrong-pytest condition by running
   ``import tests`` in a bare, dependency-free interpreter (system
   ``python3`` has none of the project's packages). The import must
   exit non-zero with a dependency-guard diagnostic, never quietly
   succeed or under-report.

2. Provenance guard — simulates the wrong-tree condition by creating a
   minimal foreign ``d33d`` package and putting it first on ``sys.path``
   so it shadows the real one, while the interpreter still has the
   project's deps (so the dependency guard passes). The provenance
   guard must refuse and name both paths.

The dependency-guard simulation deliberately uses the bare system
interpreter (not ``uv run``) because that is exactly the failure
condition: a pytest/python that does not have the project's
dependencies.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]


def _env() -> dict[str, str]:
    """A clean environment: no PYTHONPATH, no inherited venv."""
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env.pop("VIRTUAL_ENV", None)
    return env


def _interpreter_candidates() -> list[str]:
    """Ordered interpreter candidates for probing, platform-aware.

    The macOS Homebrew path is only a candidate on macOS: on Linux it
    does not exist, and ``subprocess.run`` on a missing executable
    raises ``FileNotFoundError`` rather than returning a non-zero exit
    code — so a list that puts the Homebrew path first would crash the
    probe on Linux instead of falling through to the next candidate.
    Discriminate on ``sys.platform`` so a platform-specific path is
    never probed on a platform it does not belong to.

    ``sys.executable`` is first for the deps probe because the gate
    (``scripts/test``) only ever runs an interpreter that has the
    project's deps — the one running this test is the strongest
    candidate there.
    """
    candidates = ["python3", "python"]
    if sys.platform == "darwin":
        candidates.insert(0, "/opt/homebrew/bin/python3")
    # Deduplicate while preserving order (sys.executable may be found
    # via the same PATH lookup as ``python3``).
    return list(dict.fromkeys([sys.executable, *candidates]))


def _probe(interp: str, code: str) -> int | None:
    """Run ``interp -c code``; return the exit code, or None if the
    interpreter itself could not be executed. A missing executable is a
    *no match* in a candidate probe, not an error — the loop must
    continue to the next candidate, never crash."""
    try:
        probe = subprocess.run(
            [interp, "-c", code],
            capture_output=True,
            env=_env(),
        )
    except (FileNotFoundError, OSError):
        return None
    return probe.returncode


def _depless_interpreter() -> str | None:
    """Find an interpreter that lacks the project deps (the bare system
    python). On macOS that is the Homebrew python; on Linux it is the
    system python, which the gate's pip-installed deps do not reach."""
    for interp in _interpreter_candidates():
        if _probe(interp, "import pydantic") == 1:
            return interp
    return None


def _venv_python() -> str | None:
    """Find an interpreter that HAS the project deps."""
    for interp in _interpreter_candidates():
        if _probe(interp, "import pydantic, trimesh, fastapi, yaml, networkx, d33d") == 0:
            return interp
    return None


def test_dependency_guard_refuses_bare_interpreter() -> None:
    """``import tests`` under a bare interpreter (no project deps) must
    exit non-zero with the dependency-guard diagnostic.

    Simulates the wrong-pytest condition: the system interpreter has
    none of pydantic/trimesh/fastapi/etc., so the guard must refuse
    instead of allowing a silently partial run.
    """
    interp = _depless_interpreter()
    if interp is None:
        import pytest

        tried = ", ".join(_interpreter_candidates())
        pytest.skip(
            "no dependency-free interpreter found (tried: "
            f"{tried}) — cannot simulate the wrong-pytest condition"
        )

    result = subprocess.run(
        [interp, "-c", "import tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        env=_env(),
    )
    assert result.returncode != 0, (
        f"dependency guard did not refuse; interpreter={interp}\n"
        f"stderr: {result.stderr.decode()}"
    )
    assert "GATE FAILED (dependency guard)" in result.stderr.decode()


def test_dependency_guard_names_missing_packages() -> None:
    """The refusal must name at least one missing package and the
    interpreter — a number without provenance is the defect."""
    interp = _depless_interpreter()
    if interp is None:
        import pytest

        tried = ", ".join(_interpreter_candidates())
        pytest.skip(
            "no dependency-free interpreter found (tried: "
            f"{tried}) — cannot simulate the wrong-pytest condition"
        )

    result = subprocess.run(
        [interp, "-c", "import tests"],
        cwd=REPO_ROOT,
        capture_output=True,
        env=_env(),
    )
    stderr = result.stderr.decode()
    assert "interpreter:" in stderr
    assert any(p in stderr for p in ("pydantic", "trimesh", "fastapi"))


def test_provenance_guard_safe_path() -> None:
    """When d33d resolves inside REPO_ROOT (the correct tree), the
    guard passes. Documents the safe case; the refusal is proven by
    test_provenance_guard_refuses_stale_tree."""
    venv_py = _venv_python()
    if venv_py is None:
        import pytest

        tried = ", ".join(_interpreter_candidates())
        pytest.skip(
            "no interpreter with the project deps found (tried: "
            f"{tried}) — venv not installed"
        )

    code = f"import sys; sys.path.insert(0, r'{REPO_ROOT / 'tests'}'); import tests"
    result = subprocess.run(
        [venv_py, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        env=_env(),
    )
    assert result.returncode == 0, (
        f"safe-path check failed: {result.stderr.decode()}"
    )


def test_provenance_guard_refuses_stale_tree() -> None:
    """The decisive provenance simulation: an interpreter that HAS the
    project's deps (so the dependency guard passes) but whose ``d33d``
    resolves to a DIFFERENT tree than the one being tested.

    Built by creating a minimal foreign ``d33d`` package in a sibling
    directory and putting it *first* on ``sys.path`` so it shadows the
    real package. The provenance guard must refuse and name both paths.
    """
    venv_py = _venv_python()
    if venv_py is None:
        import pytest

        tried = ", ".join(_interpreter_candidates())
        pytest.skip(
            "no interpreter with the project deps found (tried: "
            f"{tried}) — venv not installed"
        )

    import tempfile
    from pathlib import Path as P

    with tempfile.TemporaryDirectory() as td:
        foreign = P(td) / "foreign-tree"
        pkg = foreign / "d33d"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("__version__ = '0.0.0-foreign'\n")

        # The foreign d33d must win over the real one: put the foreign
        # tree first on sys.path, the real tests dir second.
        code = (
            "import sys\n"
            f"sys.path.insert(0, r'{pkg.parent}')\n"
            f"sys.path.insert(1, r'{REPO_ROOT / 'tests'}')\n"
            "import tests"
        )
        result = subprocess.run(
            [venv_py, "-c", code],
            cwd=REPO_ROOT,
            capture_output=True,
            env=_env(),
        )
        stderr = result.stderr.decode()
        assert result.returncode != 0, (
            f"provenance guard did not refuse foreign d33d; "
            f"returncode={result.returncode}\nstderr: {stderr}"
        )
        assert "GATE FAILED (provenance guard)" in stderr
        # Both paths must be named — the expected tree and the resolved
        # one — so a report can never omit the provenance.
        assert str(foreign) in stderr
        assert "resolved to" in stderr
        assert str(REPO_ROOT) in stderr


def test_gate_reports_resolved_paths() -> None:
    """The guard must expose the resolved d33d.__file__ so any count
    can be quoted with its provenance."""
    import tests

    assert tests.D33D_FILE.name == "__init__.py"
    assert REPO_ROOT in tests.D33D_FILE.parents
