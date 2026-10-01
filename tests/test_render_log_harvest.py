"""Issue #309: render.log harvest + classification integration test.

Drives ``render_for_design_loop`` with a stubbed docker layer that
reproduces QA's captured output verbatim (exit 1; stderr = only the
STL marker; /work/render.log tail = ``ERROR: Parser error``) and
asserts the result classifies as ``syntax_error`` — not
``container_error`` — so the design loop's repair path runs.

Also covers:
- the 64-KiB bounded tail (the harvest command uses ``tail -c 65536``)
- the render.log harvest is issued BEFORE the finally cleanup
- the failure is logged via the module logger (observability)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import d33d.render_worker as rw

STL_ABORT_MARKER = (
    "[entrypoint] STL export failed with exit code 1 — aborting remaining steps"
)
QA_RENDER_LOG = "ERROR: Parser error in /work/model.scad at line 3\n"


def _qa_stub(
    render_log_content: str = QA_RENDER_LOG,
    render_rc: int = 1,
    render_stderr: str = STL_ABORT_MARKER,
) -> tuple[Any, list[list[str]]]:
    """Build a ``subprocess.run`` stub that reproduces the QA capture:
    the render container exits ``render_rc`` with ``render_stderr`` on
    stderr; the render.log harvest helper writes ``render_log_content``
    to the harvest output dir; everything else succeeds.

    Returns ``(stub_function, calls_list)``."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        # The render worker run (carries the worker image)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv,
                returncode=render_rc,
                stdout=b"",
                stderr=render_stderr.encode(),
            )
        # The render.log harvest helper (tail -c 65536 /work/render.log)
        if "tail -c 65536 /work/render.log" in " ".join(argv):
            # Write the render.log content to the harvest output dir
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    out_dir = Path(tok.rsplit(":", 1)[0])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "render.log").write_text(render_log_content)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")
        # Default: everything else succeeds
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    return _record, calls


def _run_render_for_design_loop(
    monkeypatch: pytest.MonkeyPatch,
    stub: Any,
    tmp_path: Path,
    *,
    name: str = "render-000000f1",
    project_id: int = 42,
) -> rw.RenderResult:
    monkeypatch.setattr(rw.subprocess, "run", stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: name)
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))
    return rw.render_for_design_loop("cube(10);", {}, project_id=project_id)


def _harvest_indices(calls: list[list[str]]) -> tuple[int, int]:
    """Return (harvest_idx, volume_rm_idx) from the call list."""
    harvest_idx = next(
        i for i, c in enumerate(calls) if "tail -c 65536 /work/render.log" in " ".join(c)
    )
    vol_rm_idx = next(
        i for i, c in enumerate(calls) if c[:4] == ["docker", "volume", "rm", "-f"]
    )
    return harvest_idx, vol_rm_idx


def test_qa_verbatim_classifies_as_syntax_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """QA's exact capture: exit 1, stderr = only the STL marker,
    render.log = ERROR: Parser error → syntax_error."""
    stub, _calls = _qa_stub()
    result = _run_render_for_design_loop(monkeypatch, stub, tmp_path)

    assert result.ok is False
    assert result.error_class == "syntax_error"
    assert result.exit_code == 1


def test_render_log_harvest_uses_tail_c_65536(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The harvest command uses ``tail -c 65536`` (the 64-KiB bounded
    read) — not a full copy of the log."""
    stub, calls = _qa_stub()
    _run_render_for_design_loop(monkeypatch, stub, tmp_path)

    harvest_calls = [
        c for c in calls if "tail -c 65536 /work/render.log" in " ".join(c)
    ]
    assert harvest_calls, "no render.log harvest helper call found"
    # It uses the same busybox no-network pattern
    assert "--network" in harvest_calls[0]
    assert "none" in harvest_calls[0]
    assert "busybox:latest" in harvest_calls[0]


def test_render_log_harvest_before_volume_removal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The render.log harvest runs BEFORE the finally-block volume
    removal (a naive read after _remove_render_volume is a silent
    no-op)."""
    stub, calls = _qa_stub()
    _run_render_for_design_loop(monkeypatch, stub, tmp_path)

    harvest_idx, vol_rm_idx = _harvest_indices(calls)
    assert harvest_idx < vol_rm_idx, (
        f"harvest at index {harvest_idx} must run before volume rm at {vol_rm_idx}"
    )


def test_no_render_log_still_syntax_error_via_stl_marker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When the render.log harvest finds nothing (empty log), the STL
    marker in stderr alone still classifies as syntax_error."""
    stub, _calls = _qa_stub(render_log_content="")
    result = _run_render_for_design_loop(monkeypatch, stub, tmp_path)
    assert result.error_class == "syntax_error"


def test_container_error_when_no_markers(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero exit with no STL marker in stderr and no ERROR: in
    render.log → container_error (the entrypoint demonstrably never
    ran or a docker-layer failure)."""
    stub, _calls = _qa_stub(render_log_content="some benign output\n", render_stderr="docker: some error")
    result = _run_render_for_design_loop(monkeypatch, stub, tmp_path)
    assert result.error_class == "container_error"


def test_failure_is_logged_with_project_id(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Every failed render logs one ERROR line with the project id,
    error_class, and exit code (issue #309 observability)."""
    stub, _calls = _qa_stub()
    with caplog.at_level(logging.ERROR, logger=rw.__name__):
        _run_render_for_design_loop(monkeypatch, stub, tmp_path)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records, "no ERROR log record for a failed render"
    msg = error_records[0].message
    assert "project_id=42" in msg
    assert "error_class=syntax_error" in msg
    assert "exit_code=1" in msg


def test_render_log_included_in_log_tail(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The logged diagnostic text includes the harvested render.log
    tail (the OpenSCAD ERROR: line that is NOT in the container
    stderr)."""
    stub, _calls = _qa_stub()
    with caplog.at_level(logging.ERROR, logger=rw.__name__):
        _run_render_for_design_loop(monkeypatch, stub, tmp_path)

    error_records = [r for r in caplog.records if r.levelno == logging.ERROR]
    assert error_records
    msg = error_records[0].message
    assert "ERROR: Parser error" in msg
    assert "render.log tail" in msg
