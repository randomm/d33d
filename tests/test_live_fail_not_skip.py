"""The fail-not-skip invariant of the live suite (issue #108).

A silently-skipped live suite is indistinguishable from a passing one —
the exact defect class this ticket exists to remove. This is the
hermetic meta-test (in the FAST suite, no Docker/key required) that
proves the live suite's prerequisite check raises its NAMED error
(``LivePrerequisiteError``) when a prerequisite is missing — by
monkeypatching the injected seams, so it runs anywhere.

The exit-code side (a "refusal" that exits 0 is indistinguishable from
a pass) is proven by ``test_missing_key_exit_code_is_nonzero``: it runs
``pytest -m live`` in a subprocess with the key removed and asserts the
exit code is non-zero (1, not 0, not a green 0-skip).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

#: The real catalogue's location. The operator maintains it at the MAIN
#: CHECKOUT (repo root, untracked — it holds no secret, only a
#: ${TRAIL_OPENERS_LLM_KEY} reference), and it is NOT a worktree file
#: (worktrees cut from origin/main do not carry it). Resolve it there
#: first, falling back to the worktree root for a non-worktree checkout.
CATALOGUE_PATH = (
    Path("/Users/janni/projects/d33d/models.yaml")
    if Path("/Users/janni/projects/d33d/models.yaml").is_file()
    else REPO_ROOT / "models.yaml"
)


def _catalogue_path() -> Path:
    return CATALOGUE_PATH


from tests.live_e2e.conftest import (
    LivePrerequisiteError,
    check_live_prerequisites,
)


def _catalogue_path() -> Path:
    """The real catalogue. The operator maintains models.yaml at the main
    checkout (untracked — it holds no secret, only a
    ``${TRAIL_OPENERS_LLM_KEY}`` reference); worktrees cut from
    origin/main do not carry it, so resolve the main-checkout path first,
    falling back to the worktree root for a non-worktree checkout."""
    main = Path("/Users/janni/projects/d33d/models.yaml")
    return main if main.is_file() else REPO_ROOT / "models.yaml"


def test_missing_key_raises_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the key missing (and a fake docker + a real catalogue), the
    check raises the NAMED error — ``LivePrerequisiteError`` — not a
    skip, not a silent pass."""
    env = {
        k: v
        for k, v in __import__("os").environ.items()
        if k != "TRAIL_OPENERS_LLM_KEY"
    }
    with pytest.raises(LivePrerequisiteError) as exc:
        check_live_prerequisites(
            docker_check=lambda: True,  # docker "available" — isolates the key
            env=env,
            catalogue_path=_catalogue_path(),
        )
    assert "TRAIL_OPENERS_LLM_KEY" in str(exc.value)


def test_missing_docker_raises_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With Docker missing (and the key present), the check raises the
    NAMED error — naming Docker."""
    import os

    env = dict(os.environ)
    env.setdefault("TRAIL_OPENERS_LLM_KEY", "test-key-present")
    with pytest.raises(LivePrerequisiteError) as exc:
        check_live_prerequisites(
            docker_check=lambda: False,  # docker "unavailable"
            env=env,
            catalogue_path=_catalogue_path(),
        )
    assert "docker" in str(exc.value).lower()


def test_missing_catalogue_raises_named_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the catalogue missing (docker + key present), the check
    raises the NAMED error — naming models.yaml."""
    import os

    env = dict(os.environ)
    env.setdefault("TRAIL_OPENERS_LLM_KEY", "test-key-present")
    with pytest.raises(LivePrerequisiteError) as exc:
        check_live_prerequisites(
            docker_check=lambda: True,
            env=env,
            catalogue_path=REPO_ROOT / "nope-does-not-exist.yaml",
        )
    assert "models.yaml" in str(exc.value)


def test_missing_key_exit_code_is_nonzero() -> None:
    """The LIVE suite's collection-time check must produce a NON-ZERO
    pytest exit code when a prerequisite is missing — a refusal that
    exits 0 is indistinguishable from a pass (the very thing being
    guarded against).

    Runs ``pytest -m live`` in a subprocess with
    ``TRAIL_OPENERS_LLM_KEY`` removed from the environment. The
    conftest's ``pytest_configure`` check raises the named error at
    collection time -> a non-zero exit. This test is marked ``slow``
    (it spawns a full pytest; a few seconds) so the fast
    ``-m "not slow"`` CI job does not pay for it, and it is NOT
    marked ``live`` (it must run without the key).
    """
    import os

    env = {k: v for k, v in os.environ.items() if k != "TRAIL_OPENERS_LLM_KEY"}
    env["PYTHONPATH"] = str(REPO_ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    # The key is absent -> the conftest check raises at collection. The
    # subprocess still needs the catalogue to exist so the failure is
    # the KEY (not the catalogue): it is at the main checkout (see
    # _catalogue_path); the conftest's default is the worktree root, so
    # copy the real catalogue in place for the subprocess's REPO_ROOT.
    # (The catalogue holds no secret — only the ${ENV} reference.)

    wt_catalogue = REPO_ROOT / "models.yaml"
    main_catalogue = Path("/Users/janni/projects/d33d/models.yaml")
    copied = False
    if main_catalogue.is_file() and not wt_catalogue.is_file():
        wt_catalogue.write_text(
            main_catalogue.read_text(encoding="utf-8"), encoding="utf-8"
        )
        copied = True
    try:
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                "-m",
                "live",
                "tests/live_e2e",
                "-q",
            ],
            cwd=REPO_ROOT,
            check=False,
            env=env,
            capture_output=True,
            timeout=120,
        )
    finally:
        if copied:
            wt_catalogue.unlink(missing_ok=True)
    assert proc.returncode != 0, (
        f"pytest -m live exited {proc.returncode} (0) with the key missing — "
        "a refusal that exits 0 is indistinguishable from a pass. stderr: "
        f"{proc.stderr[-800:]!r}"
    )
    combined = (proc.stderr + proc.stdout).decode("utf-8", "replace")
    assert "TRAIL_OPENERS_LLM_KEY" in combined or "LivePrerequisiteError" in combined, (
        "the failure output does not name the missing prerequisite "
        f"(TRAIL_OPENERS_LLM_KEY): {combined[-800:]!r}"
    )


def test_fast_expression_excludes_live(tmp_path: Path) -> None:
    """The CI expression (``not slow and not live``) excludes the live
    marker — the fast CI job can never start calling the real LLM.
    Pinned structurally: the expression in ci.yml must contain
    ``not live`` (the file is read from the worktree — it IS a tracked
    repo file, unlike the untracked catalogue)."""
    ci = REPO_ROOT / ".github" / "workflows" / "ci.yml"
    text = ci.read_text(encoding="utf-8")
    assert 'pytest -m "not slow and not live"' in text, (
        f"the CI fast job's marker expression in {ci} does not exclude "
        "'live' — the fast job must never invoke the real LLM (issue #108)"
    )


def test_pyproject_declares_live_marker() -> None:
    """The ``live`` marker is declared in pyproject.toml (pytest warns
    on undeclared markers; the declaration documents the suite's
    contract) — pyproject IS a tracked worktree file."""
    pyproject = REPO_ROOT / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    assert '"live:' in text, (
        f"the 'live' marker is not declared in {pyproject} — pytest would "
        "warn and the marker exclusion would be invisible"
    )
