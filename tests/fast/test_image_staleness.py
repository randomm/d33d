"""Fast-layer test: the render-worker image staleness guard (issue #236).

No Docker required. Unit-tests the hash computation (:func:`d33d.render_worker.build_hash`)
and the label comparison (:func:`d33d.render_worker._verify_render_worker_image`)
with a stubbed ``docker image inspect`` — the silent-stale-image failure mode of
the ``d33d/render-worker:local`` image (entrypoint.sh changes after build, nothing
rebuilds it) must fail loudly rather than render with baked-in code that no
longer matches the source.

Also a doc-drift guard: the canonical build command in
``docs/bosl2-pinning.md`` must supply the hash the Dockerfile requires as a
``--build-arg D33D_BUILD_HASH`` (computed via ``uv run python``) and the
``-t`` tag must match ``d33d.render_worker.RENDER_WORKER_IMAGE`` — the doc
previously showed ``-t d33d-render-worker:latest``, which did not match
the constant and drifted from the code, and a version that passed the hash
as ``--label`` while the Dockerfile demanded it as a build-arg made the
documented command fail at the Dockerfile's ``test -n`` gate.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

import d33d.render_worker as rw

REPO_ROOT = Path(__file__).resolve().parents[2]
DOCS_BUILD_CMD = REPO_ROOT / "docs" / "bosl2-pinning.md"


# --- build_hash -----------------------------------------------------------


def test_build_hash_is_stable_across_two_calls() -> None:
    assert rw.build_hash() == rw.build_hash()


def test_build_hash_changes_when_entrypoint_changes(
    tmp_path: Path,
) -> None:
    root = _copy_build_inputs(tmp_path)
    base = rw.build_hash(root)
    (root / "entrypoint.sh").write_bytes(b"#!/bin/bash\nexit 0\n")
    assert rw.build_hash(root) != base


def test_build_hash_changes_when_dockerfile_changes(
    tmp_path: Path,
) -> None:
    root = _copy_build_inputs(tmp_path)
    base = rw.build_hash(root)
    (root / "Dockerfile").write_bytes(b"FROM scratch\n")
    assert rw.build_hash(root) != base


def test_build_hash_ignores_out_of_scope_files(
    tmp_path: Path,
) -> None:
    root = _copy_build_inputs(tmp_path)
    base = rw.build_hash(root)
    (root / "README.md").write_bytes(b"# totally different content\n")
    assert rw.build_hash(root) == base


def test_build_hash_missing_input_raises_file_not_found(
    tmp_path: Path,
) -> None:
    root = _copy_build_inputs(tmp_path)
    (root / "entrypoint.sh").unlink()
    with pytest.raises(FileNotFoundError):
        rw.build_hash(root)


def _copy_build_inputs(dst: Path) -> Path:
    for name in ("entrypoint.sh", "Dockerfile"):
        (dst / name).write_bytes((REPO_ROOT / name).read_bytes())
    return dst


# --- _docker_image_labels -------------------------------------------------


def _run_stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Stub ``subprocess.run`` for ``docker image inspect``.

    Returns the label JSON the real docker CLI would emit, or a
    non-zero exit for an absent image.
    """
    if argv[:2] == ["docker", "image"]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b'{"d33d/build-hash": "abc"}', stderr=b""
        )
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


def test_docker_image_labels_returns_dict(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _run_stub)
    assert rw._docker_image_labels("d33d/render-worker:local") == {
        "d33d/build-hash": "abc"
    }


def test_docker_image_labels_none_when_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=1, stdout=b"", stderr=b"Error: No such image"
        )

    monkeypatch.setattr(rw.subprocess, "run", _stub)
    assert rw._docker_image_labels("d33d/render-worker:local") is None


def test_docker_image_labels_empty_dict_when_unlabeled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"null", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _stub)
    assert rw._docker_image_labels("d33d/render-worker:local") == {}


# --- _verify_render_worker_image ------------------------------------------


def _stub_labels(labels: dict[str, str] | None) -> "object":
    """Return a stubbed ``subprocess.run`` for a given label dict.

    ``labels=None`` means the image is absent (inspect non-zero).
    ``labels={}`` means present but unlabeled.
    """
    def _stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if labels is None:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"Error: No such image"
            )
        import json as _json

        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=_json.dumps(labels).encode(), stderr=b""
        )

    return _stub


def test_verify_passes_when_label_matches_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels({rw.BUILD_HASH_LABEL: "abc123"}))
    rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")


def test_verify_fails_loudly_on_hash_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels({rw.BUILD_HASH_LABEL: "stale"}))
    with pytest.raises(RuntimeError, match="docs/bosl2-pinning.md"):
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")


def test_verify_fails_loudly_when_label_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels({}))
    with pytest.raises(RuntimeError, match="docs/bosl2-pinning.md"):
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")


def test_verify_fails_loudly_when_image_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels(None))
    with pytest.raises(RuntimeError, match="docs/bosl2-pinning.md"):
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")


def test_verify_message_names_rebuild_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels(None))
    with pytest.raises(RuntimeError) as exc_info:
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")
    msg = str(exc_info.value)
    assert "docker build" in msg
    assert "--build-arg D33D_BUILD_HASH=" in msg
    assert "uv run python" in msg
    assert "d33d/render-worker:local" in msg


def test_verify_inspect_timeout_is_docker_query_failure_not_staleness(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """A hung daemon (``subprocess.TimeoutExpired`` from the inspect call)
    is a docker-query failure, never staleness: ``_verify_render_worker_image``
    must raise ``OSError`` (not ``RuntimeError``) and
    ``render_for_design_loop`` must not return the staleness container_error
    (the guard's rebuild-message RuntimeError) for it.
    """

    def _stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "image"]:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=15)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _stub)
    # OSError (docker-query failure) — not the staleness RuntimeError — is
    # what render_for_design_loop's broad except turns into its generic
    # "render pipeline error" container_error, never the staleness one.
    with pytest.raises(OSError, match="could not query docker"):
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")
    # Control: a genuine mismatch raises the staleness RuntimeError, so the
    # OSError above is a distinguishable docker-query failure, not staleness.
    monkeypatch.setattr(rw.subprocess, "run", _stub_labels(None))
    with pytest.raises(RuntimeError, match="docs/bosl2-pinning.md"):
        rw._verify_render_worker_image("d33d/render-worker:local", expected_hash="abc123")


# --- doc-drift guard -------------------------------------------------------


def _extract_build_command_block(src: str) -> str:
    """Extract the first ```bash …``` block from the doc."""
    m = re.search(r"```bash\n(.*?)```", src, re.DOTALL)
    assert m is not None, "no ```bash block found in docs/bosl2-pinning.md"
    return m.group(1)


def test_doc_build_command_passes_hash_as_build_arg() -> None:
    src = DOCS_BUILD_CMD.read_text(encoding="utf-8")
    block = _extract_build_command_block(src)
    assert "--build-arg D33D_BUILD_HASH=" in block, (
        "canonical build command in docs/bosl2-pinning.md must pass the "
        "working-tree hash as a --build-arg D33D_BUILD_HASH value — the "
        "Dockerfile requires it via `test -n \"${D33D_BUILD_HASH}\"` and "
        "stamps the label from the ARG itself (issue #236)"
    )
    assert "uv run python" in block, (
        "canonical build command in docs/bosl2-pinning.md must compute the "
        "hash with the project interpreter (`uv run python`), so the "
        "documented command works from a plain checkout without a "
        "pre-activated venv"
    )


def test_doc_supplies_every_arg_required_by_dockerfile() -> None:
    """Every ARG the Dockerfile gates on via ``test -n \"${X}\"`` must be
    supplied as ``--build-arg X`` in the doc's canonical command.

    The regression test for the bug that made the documented build
    command fail: the Dockerfile started requiring ``D33D_BUILD_HASH``
    while the doc only passed it as ``--label``, so a documented build
    could never succeed.
    """
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    required = set(re.findall(r'test -n "\$\{(\w+)\}"', dockerfile))
    assert required, "no `test -n`-gated ARGs found in the Dockerfile"
    src = DOCS_BUILD_CMD.read_text(encoding="utf-8")
    block = _extract_build_command_block(src)
    supplied = set(re.findall(r"--build-arg (\w+)=", block))
    missing = required - supplied
    assert not missing, (
        f"Dockerfile requires {sorted(missing)} via `test -n` but the "
        "canonical build command in docs/bosl2-pinning.md does not pass "
        "them as --build-arg — the documented build would fail"
    )


def test_doc_build_command_tag_matches_render_worker_image() -> None:
    src = DOCS_BUILD_CMD.read_text(encoding="utf-8")
    block = _extract_build_command_block(src)
    assert rw.RENDER_WORKER_IMAGE in block, (
        f"canonical build command in docs/bosl2-pinning.md tags a different "
        f"image than rw.RENDER_WORKER_IMAGE ({rw.RENDER_WORKER_IMAGE!r})"
    )
