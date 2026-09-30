"""Fast-layer test: issue #309 — render.log tail feeds classify.

No Docker required. Covers:
- ``render_log`` kwarg: ``ERROR:`` in render.log → ``syntax_error``
- ``STL_ABORT_RE`` precedence: the STL marker in stderr takes priority
  over the ``ERROR:`` check (so a truncated render.log never regresses
  a true syntax error to ``container_error``)
- The QA regression case verbatim (exit 1, stderr = only the STL marker,
  render.log = ``ERROR: Parser error``) → ``syntax_error``
- The 64-KiB tail edge case: no STL marker AND no ``ERROR:`` in the
  truncated render.log → still ``syntax_error`` (the only source of the
  STL marker is the STL step)
- Timeout/oom precedence unchanged
"""

from __future__ import annotations

import d33d.render_worker as rw

SIX_VIEWS = [f"view_{i:02d}_x.png" for i in range(6)]

STL_ABORT_MARKER = (
    "[entrypoint] STL export failed with exit code 1 — aborting remaining steps"
)


def _classify(
    exit_code: int = 1,
    stderr: str = "",
    render_log: str = "",
    oomkilled: bool = False,
    timed_out: bool = False,
) -> rw.ErrorClass:
    return rw.classify(
        exit_code=exit_code,
        stl_path=None,
        csg_path=None,
        views=SIX_VIEWS,
        stderr=stderr,
        render_log=render_log,
        oomkilled=oomkilled,
        timed_out=timed_out,
    )


def test_stl_abort_marker_in_stderr_is_syntax_error() -> None:
    """The entrypoint's STL-abort marker (stderr) → syntax_error even
    when render.log is empty (no ERROR: harvested)."""
    assert _classify(stderr=STL_ABORT_MARKER, render_log="") == "syntax_error"


def test_error_in_render_log_is_syntax_error() -> None:
    """An OpenSCAD ``ERROR:`` line in the harvested render.log →
    syntax_error even when the container stderr has no such line."""
    assert _classify(stderr="", render_log="ERROR: Parser error at line 1") == "syntax_error"


def test_error_in_container_stderr_still_syntax_error() -> None:
    """Backward compatibility: ``ERROR:`` in the container stderr
    (pre-#309 path) still → syntax_error without a render_log."""
    assert _classify(stderr="ERROR: Parser error", render_log="") == "syntax_error"


def test_qa_regression_case_verbatim() -> None:
    """The exact QA capture from tmp/review-2026-09-29/REVIEW.md:
    exit 1, stderr = only the STL marker, render.log = ERROR: Parser
    error → syntax_error."""
    assert (
        _classify(
            stderr=STL_ABORT_MARKER,
            render_log="ERROR: Parser error in /work/model.scad at line 3",
        )
        == "syntax_error"
    )


def test_stl_marker_precedes_error_in_truncated_render_log() -> None:
    """The sharp edge case from the issue's resolved gate: the STL
    marker is in stderr AND the render.log tail (64 KiB window) is
    truncated so no ``ERROR:`` line survived — still syntax_error.
    The only source of the STL marker is the STL step, so the 64-KiB
    tail never regresses a true syntax error to container_error."""
    truncated_log = "x" * 1000  # large but no ERROR: line
    assert _classify(stderr=STL_ABORT_MARKER, render_log=truncated_log) == "syntax_error"


def test_no_markers_no_render_log_is_container_error() -> None:
    """No STL marker in stderr, no ERROR: in either source →
    container_error (the entrypoint demonstrably never ran or the
    docker layer failed)."""
    assert _classify(stderr="some other output", render_log="") == "container_error"


def test_timeout_still_wins_over_stl_marker() -> None:
    """Timeout has highest precedence — even with the STL marker,
    timed_out=True → timeout."""
    assert (
        _classify(stderr=STL_ABORT_MARKER, render_log="ERROR: x", timed_out=True)
        == "timeout"
    )


def test_oom_still_wins_over_stl_marker() -> None:
    """OOM has second-highest precedence — even with the STL marker,
    exit 137 → oom."""
    assert (
        _classify(exit_code=137, stderr=STL_ABORT_MARKER)
        == "oom"
    )


def test_exit_zero_with_stl_marker_is_container_error() -> None:
    """Exit 0 with a no-STL shape and no markers → container_error
    (the entrypoint never ran to produce a valid STL)."""
    assert _classify(exit_code=0, stderr=STL_ABORT_MARKER, render_log="") == "container_error"


def test_classify_accepts_render_log_kwarg() -> None:
    """The ``render_log`` parameter is accepted and defaults to ``""``
    (backward compatible — existing callers that don't pass it still
    work)."""
    assert rw.classify(exit_code=1, stl_path=None, stderr="ERROR: x") == "syntax_error"
    assert rw.classify(exit_code=1, stl_path=None, stderr="ERROR: x", render_log="") == "syntax_error"


def test_render_log_tail_bytes_constant_is_64_kib() -> None:
    """RENDER_LOG_TAIL_BYTES is 64 KiB (the spec's bounded-read size)."""
    assert rw.RENDER_LOG_TAIL_BYTES == 64 * 1024
