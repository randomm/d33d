"""Fast-layer test: ``run_container`` wall-clock timeout enforcement.

No Docker required — ``subprocess.run`` is patched with
``unittest.mock``. Proves the HIGH-severity review finding that a runaway
``.scad`` (infinite loop under the pid limit) is actually killed:
``run_container`` wraps ``subprocess.run`` with ``timeout=timeout_s`` and,
on ``TimeoutExpired``, runs ``docker kill`` + ``docker rm`` (the container
is never started with ``--rm``) before returning a sentinel result that
``classify(timed_out=True)`` maps to the ``timeout`` row of the closed
``ErrorClass`` enum.
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import d33d.render_worker as rw

ARGV = [
    "docker",
    "run",
    "--name",
    "render-12345678",
    "openscad/openscad:trixie",
    "-D",
    "X=1",
]


def _completed(returncode: int) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(
        args=ARGV, returncode=returncode, stdout=b"", stderr=b""
    )


def test_run_container_passes_timeout_s_to_subprocess_run() -> None:
    """The caller contract: ``timeout_s`` is threaded through to
    ``subprocess.run`` verbatim, plus ``capture_output=True``."""
    with patch("d33d.render_worker.subprocess.run") as mock_run:
        mock_run.return_value = _completed(0)
        proc = rw.run_container(ARGV, timeout_s=120)

    mock_run.assert_called_once_with(
        ARGV, timeout=120, capture_output=True, check=False
    )
    assert proc.returncode == 0


def test_run_container_success_passes_through_completed_process() -> None:
    with patch("d33d.render_worker.subprocess.run") as mock_run:
        sentinel = _completed(5)
        mock_run.return_value = sentinel
        proc = rw.run_container(ARGV, timeout_s=42)
    assert proc is sentinel


def test_run_container_timeout_kills_and_removes_container() -> None:
    """On ``TimeoutExpired`` the wrapper kills AND removes the named
    container (no ``--rm`` is used at start, so nothing else cleans up),
    in that order."""
    with patch("d33d.render_worker.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=ARGV, timeout=120),
            _completed(0),  # docker kill
            _completed(0),  # docker rm
        ]
        proc = rw.run_container(ARGV, timeout_s=120)

    assert mock_run.call_count == 3
    kill_call = mock_run.call_args_list[1]
    rm_call = mock_run.call_args_list[2]
    assert kill_call.args[0] == ["docker", "kill", "render-12345678"]
    assert rm_call.args[0] == ["docker", "rm", "render-12345678"]
    assert proc.returncode == 124


def test_run_container_timeout_maps_to_classify_timeout_row() -> None:
    """The full contract: a ``subprocess.TimeoutExpired`` from the
    invocation layer lands in the ``timeout`` row of the closed
    ``ErrorClass`` enum via ``classify(timed_out=True)``."""
    with patch("d33d.render_worker.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=ARGV, timeout=120),
            _completed(0),
            _completed(0),
        ]
        proc = rw.run_container(ARGV, timeout_s=120)
        timed_out = proc.returncode == 124

    error_class = rw.classify(exit_code=proc.returncode, timed_out=timed_out)
    assert error_class == "timeout"
    assert error_class in rw.ERROR_CLASSES


def test_run_container_timeout_without_name_skips_cleanup() -> None:
    """An argv with no ``--name`` (defensive) still returns the 124
    sentinel but performs no docker cleanup."""
    argv_no_name = ["docker", "run", "openscad/openscad:trixie"]
    with patch("d33d.render_worker.subprocess.run") as mock_run:
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=argv_no_name, timeout=5)
        proc = rw.run_container(argv_no_name, timeout_s=5)

    mock_run.assert_called_once_with(
        argv_no_name, timeout=5, capture_output=True, check=False
    )
    assert proc.returncode == 124


def test_argv_container_name_extracts_name_from_build_docker_argv_output() -> None:
    """``_argv_container_name`` finds ``--name`` on real
    ``build_docker_argv`` output, so the timeout path cleans up the
    right container."""
    argv = rw.build_docker_argv("openscad/openscad:trixie", "render-abcdef01")
    assert rw._argv_container_name(argv) == "render-abcdef01"
