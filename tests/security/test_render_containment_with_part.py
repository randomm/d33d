"""Security: render_for_design_loop containment invariants WITH a part present.

Issue #330: when ``part_path`` is given, the render must maintain the exact
same container containment as without a part — exactly two mounts in the
render argv (the rw /work named volume + /tmp tmpfs), --network none, no
--privileged, no docker.sock reference. The part rides the EXISTING ro
/host mount of the staging dir; no new mount is added.

These tests assert the FULL subprocess argv sequence (seed helper, verify,
render) with a part present, verifying no containment regression.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

import d33d.render_worker as rw


def _make_part_repo(tmp_path: Path) -> tuple[Path, Path]:
    """Create a minimal repo dir with a valid part.stl inside it.

    Returns (part_path, repo_dir).
    """
    repo = tmp_path / "repo"
    versions = repo / "versions" / "1"
    versions.mkdir(parents=True)
    part = versions / "part.stl"
    part.write_bytes(b"solid test\nendsolid test\n")
    return part, repo


def _stub_subprocess(
    tmp_path: Path, part_path: Path, repo_dir: Path
) -> tuple[Any, list[list[str]]]:
    """A subprocess.run stub that records all calls and simulates success
    for every docker call (except the render worker which returns 1 for
    the non-zero path, or 0 for the success path)."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        # Render worker run (has --memory flag): fail it to exercise the
        # non-zero path (so we can inspect the full argv sequence up to
        # and including the render argv).
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"ERROR: x"
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    return _record, calls


def test_with_part_no_new_mount_in_render_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The render argv (build_docker_argv output) with a part present has
    exactly one --volume (/work) and one --tmpfs (/tmp) — the same as
    without a part. No new mount is introduced."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0001")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop(
        "cube(10);", {}, part_path=part, repo_dir=repo
    )

    # The render argv is the one with --memory (build_docker_argv output).
    render_argv = next(
        c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c
    )
    assert render_argv.count("--volume") == 1, (
        f"render argv must have exactly one --volume, got {render_argv.count('--volume')}"
    )
    assert render_argv.count("--tmpfs") == 1, (
        f"render argv must have exactly one --tmpfs, got {render_argv.count('--tmpfs')}"
    )
    # No new mount type.
    for other in ("--mount", "-v", "--bind"):
        assert other not in render_argv, f"unexpected mount flag {other!r}"


def test_with_part_network_none(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The render argv with a part present still has --network none."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0002")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    render_argv = next(
        c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c
    )
    net_idx = render_argv.index("--network")
    assert render_argv[net_idx + 1] == "none"


def test_with_part_no_privileged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No --privileged flag in ANY argv with a part present."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0003")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    for c in calls:
        assert "--privileged" not in c, f"--privileged found in: {c}"


def test_with_part_no_docker_sock(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No /var/run/docker.sock reference in ANY argv with a part present."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0004")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    for c in calls:
        flat = " ".join(c)
        assert "docker.sock" not in flat, f"docker.sock reference in: {c}"


def test_with_part_seed_helper_exactly_two_mounts(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The seed helper argv with a part present has exactly two --volume
    flags (the work volume + the ro host staging dir) — same as without
    a part."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0005")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    # The seed helper is the busybox sh -c call (not the render, not verify).
    seed_calls = [
        c
        for c in calls
        if "busybox:latest" in c and "sh" in c and "cp /host/src" in c[-1]
    ]
    assert seed_calls, "no seed helper call found"
    seed_argv = seed_calls[0]
    assert seed_argv.count("--volume") == 2, (
        f"seed helper must have exactly 2 --volume, got {seed_argv.count('--volume')}"
    )


def test_with_part_part_path_not_in_render_argv(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The part's host path must NOT appear in the render argv — only in
    the seed helper's existing ro /host mount of the staging dir."""
    part, repo = _make_part_repo(tmp_path)
    stub, calls = _stub_subprocess(tmp_path, part, repo)

    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-cont0006")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    render_argv = next(
        c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c
    )
    flat = " ".join(render_argv)
    # The part's host path (or its repo) must never appear in the render
    # argv — the part rides only the seed helper's ro /host mount.
    assert str(repo) not in flat, (
        f"repo path leaked into render argv: {render_argv}"
    )
    assert str(part) not in flat, (
        f"part host path leaked into render argv: {render_argv}"
    )
