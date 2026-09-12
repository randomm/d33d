"""Commit-message sanitization tests (issue #8, adversarial round 1).

The version-creation and set-as-main commits embed the (user-editable)
version name in the commit subject. The name is user-supplied and the
per-project git history is parsed downstream, so the name must pass through
the same ``_sanitize_commit_message`` filter the photo-upload path uses —
newlines and shell metacharacters must never reach the commit body.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from tests.versioning.helpers import (
    create_project,
    create_version,
    repo_path_for,
    run_async,
)


def _latest_subject(repo: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), "log", "-1", "--format=%s"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    ).stdout


def _assert_single_line_subject(subject: str, prefix: str) -> None:
    """The subject is exactly one line starting with ``prefix``; only safe
    alnum + ``._-`` characters follow (the trailing newline is git's own)."""
    body = subject.rstrip("\n")
    assert "\n" not in body, f"commit message contains a newline: {subject!r}"
    assert body.startswith(prefix), body
    sanitized = body[len(prefix):]
    assert sanitized, "sanitized subject must be non-empty"
    assert all(c.isalnum() or c in "._-" for c in sanitized), (
        f"unsafe characters in commit subject: {sanitized!r}"
    )


def test_version_commit_message_is_sanitized(app_with_versions):
    """A malicious version name (embedded newlines + shell metacharacters)
    must never reach the git commit subject. The API display name is the
    user's exact string (rename is free-text) — only the commit is
    sanitized."""
    evil_name = "v1\n$(whoami);rm -rf / # " + """" ' `"""

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        r = await client.post(
            f"/api/projects/{pid}/versions",
            json={"params": {"W": 20}, "name": evil_name},
        )
        assert r.status_code == 201, r.text
        repo = repo_path_for(app_with_versions, pid)
        return r.json(), repo

    version, repo = run_async(app_with_versions, _call)
    # The API display name is the user's exact string (rename is free-text).
    assert version["name"] == evil_name
    # The commit subject is sanitized: one line, safe charset only.
    _assert_single_line_subject(_latest_subject(repo), "version: ")


def test_set_as_main_commit_message_is_sanitized(app_with_versions):
    """The set-as-main marker commit embeds the target version's name — the
    same sanitization applies."""
    evil_name = "main\n`id` && echo pwned"

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        v = await create_version(client, pid, {"W": 20}, name=evil_name)
        r = await client.post(f"/api/projects/{pid}/versions/{v['id']}/set-as-main")
        assert r.status_code == 200, r.text
        repo = repo_path_for(app_with_versions, pid)
        return repo

    repo = run_async(app_with_versions, _call)
    _assert_single_line_subject(_latest_subject(repo), "set as main: ")
