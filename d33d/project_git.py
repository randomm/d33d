"""Per-project git repo helpers (local only, no network).

The ``git init`` / ``git add -A`` / ``git commit`` primitives the
per-project repos use, shared by the project router (init + commit on
photo upload), the design-source PUT route (commit), the version-write
path (``d33d.versions``), and the design-state storage signal.

The commit-message sanitization lives here with the git primitives it
guards: the photo-upload, design-source, and version-write paths share
ONE definition (user-supplied filenames must never inject newlines or
shell metacharacters into the per-project repo's commit history).

``d33d.projects`` re-exports ``_sanitize_commit_message`` under its
historical name (``d33d.versions`` imports it from there) and keeps the
``repo_present`` predicate it owns.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

__all__ = [
    "commit_all",
    "init_git_repo",
    "remove_repo",
    "repo_present",
    "sanitize_commit_message",
]

# The per-repo identity (set on ``git init`` so commits work on machines
# without a global git identity).
GIT_USER_EMAIL = "d33d@local"
GIT_USER_NAME = "d33d"

_MAX_COMMIT_MESSAGE_LEN = 200


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a git command in ``repo_dir``. Raises on non-zero exit."""
    cmd = ["git", "-C", str(repo_dir), *args]
    result = subprocess.run(
        cmd, capture_output=True, text=True, timeout=30, check=False
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git {' '.join(args)} failed (rc={result.returncode}): {result.stderr.strip()}"
        )
    return result


def init_git_repo(repo_dir: Path) -> None:
    """``git init`` + set local identity. Idempotent (skips if .git exists)."""
    repo_dir.mkdir(parents=True, exist_ok=True)
    if not (repo_dir / ".git").exists():
        _git(repo_dir, "init", "-q")
        _git(repo_dir, "config", "user.email", GIT_USER_EMAIL)
        _git(repo_dir, "config", "user.name", GIT_USER_NAME)


def commit_all(repo_dir: Path, message: str) -> None:
    """Stage everything and commit. No-op if nothing to commit."""
    _git(repo_dir, "add", "-A")
    # Check if there is anything to commit
    status = _git(repo_dir, "status", "--porcelain")
    if not status.stdout.strip():
        return
    _git(repo_dir, "commit", "-q", "-m", message)


def remove_repo(repo_dir: Path) -> None:
    """Remove the repo directory entirely. No-op if missing."""
    if repo_dir.is_dir():
        shutil.rmtree(repo_dir)


def repo_present(row: dict[str, Any]) -> bool:
    """The single predicate for "is the project's git repo directory on
    disk?" — the shared check used by both ``d33d.projects``'s
    ``_storage_field`` (the project GET's ``storage.repo_present``) and
    the design-state route's ``history_missing`` flag (issue #316
    task-b).

    True when the repo directory exists; False when it is absent (deleted
    out-of-band, never created, etc.). Computed at call time — it can flip
    without a new version (the caller must not cache it per-project).
    """
    return Path(row["git_repo_path"]).is_dir()


def sanitize_commit_message(text: str) -> str:
    """Reduce ``text`` to a single line of safe alnum+``._-`` characters.

    Mirrors the filename-sanitization filter (defensively stricter than
    the caller needs): any character outside the safe set — including
    newlines, shell metacharacters, and other punctuation — is dropped,
    and the result is capped at ``_MAX_COMMIT_MESSAGE_LEN`` characters.
    """
    safe = "".join(c for c in text if c.isalnum() or c in "._-")
    return safe[:_MAX_COMMIT_MESSAGE_LEN]
