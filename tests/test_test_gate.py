"""Regression tests for the deterministic test gate (issue #145, #148).

These tests prove the two guards actually refuse — not merely that the
right invocation works:

1. Dependency guard — simulates the wrong-pytest condition by shadowing
   required packages (pydantic, trimesh) with stub modules that raise
   ImportError, so that ``import tests`` in the *current* interpreter
   (which actually has the project deps) still triggers the dependency
   guard's refusal. The guard must exit with code 3, print its
   diagnostic, and name the shadowed packages.

2. Provenance guard — simulates the wrong-tree condition by creating a
   minimal foreign ``d33d`` package and putting it first on ``sys.path``
   so it shadows the real one, while the interpreter still has the
   project's deps (so the dependency guard passes). The provenance
   guard must refuse and name both paths.

Why shadowing rather than hunting for a bare interpreter (issue #148):
the original approach probed a candidate list of interpreters looking
for one that could NOT ``import pydantic``. On Linux CI every candidate
already has the project deps installed (``pip install -e ".[test]"``),
so no such interpreter exists and the tests skip. Shadowing a module
on ``sys.path`` is platform-independent: the shadow directory is placed
first on ``sys.path`` in the spawned subprocess, so ``import pydantic``
picks up the stub (which raises ImportError) before the real package
is ever reached. No special interpreter is needed.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Packages that the dependency guard checks and that the meta-tests
# shadow to simulate their absence. Two are shadowed so the
# "names missing packages" test can prove the guard enumerates more
# than one.
_SHADOWED_PACKAGES = ("pydantic", "trimesh")


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


def _venv_python() -> str | None:
    """Find an interpreter that HAS the project deps."""
    for interp in _interpreter_candidates():
        if _probe(interp, "import pydantic, trimesh, fastapi, yaml, networkx, d33d") == 0:
            return interp
    return None


def _shadow_env(tmpdir: Path) -> dict[str, str]:
    """Build an env dict for a subprocess where the shadow directory
    is first on PYTHONPATH, so stub modules shadow the real packages."""
    env = _env()
    existing = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = str(tmpdir) + (":" + existing if existing else "")
    return env


def _write_shadow_stubs(shadow_dir: Path) -> None:
    """Create stub .py files that raise ImportError when imported.

    The stubs simulate the situation where the real package is not
    installed: ``import pydantic`` finds the stub first (shadow dir is
    first on sys.path), executes ``raise ImportError``, and the
    dependency guard in tests/__init__.py records the package as
    missing.
    """
    for pkg in _SHADOWED_PACKAGES:
        stub = shadow_dir / f"{pkg}.py"
        stub.write_text(f"raise ImportError('{pkg} is not installed')\n")


def _run_import_tests(shadow_dir: Path | None) -> subprocess.CompletedProcess:
    """Run ``import tests`` in the current interpreter, with the shadow
    directory first on PYTHONPATH (if given). The shadow dir, if present,
    is consulted before site-packages, so the stub modules shadow the
    real packages."""
    env = _env()
    if shadow_dir is not None:
        env["PYTHONPATH"] = str(shadow_dir)
    code = f"import sys; sys.path.insert(0, r'{REPO_ROOT / 'tests'}'); import tests"
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        capture_output=True,
        env=env,
    )


def test_dependency_guard_refuses_bare_interpreter() -> None:
    """``import tests`` with required packages shadowed must exit with
    code 3 and the dependency-guard diagnostic.

    Simulates the wrong-pytest condition: pydantic and trimesh are
    shadowed by stubs that raise ImportError, so the guard must refuse
    instead of allowing a silently partial run.
    """
    with tempfile.TemporaryDirectory() as td:
        shadow = Path(td)
        _write_shadow_stubs(shadow)
        result = _run_import_tests(shadow)

    assert result.returncode == 3, (
        f"dependency guard did not refuse (expected exit 3, got "
        f"{result.returncode}); interpreter={sys.executable}\n"
        f"stderr: {result.stderr.decode()}"
    )
    assert "GATE FAILED (dependency guard)" in result.stderr.decode()


def test_dependency_guard_names_missing_packages() -> None:
    """The refusal must name every shadowed package and the interpreter
    — a number without provenance is the defect.

    Shadows two distinct packages (pydantic, trimesh) to prove the
    guard enumerates them, not just the first it encounters.
    """
    with tempfile.TemporaryDirectory() as td:
        shadow = Path(td)
        _write_shadow_stubs(shadow)
        result = _run_import_tests(shadow)

    stderr = result.stderr.decode()
    assert "interpreter:" in stderr
    # Both shadowed packages must be named in the refusal message.
    for pkg in _SHADOWED_PACKAGES:
        assert pkg in stderr, f"shadowed package {pkg!r} not named in: {stderr}"


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

    with tempfile.TemporaryDirectory() as td:
        foreign = Path(td) / "foreign-tree"
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
