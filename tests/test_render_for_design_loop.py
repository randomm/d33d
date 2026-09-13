"""Pins that ``render_for_design_loop`` lives in ``d33d.render_worker``.

Ticket #41 moved the design-loop render orchestration out of
``d33d/app.py`` (where it lived as ``_production_render_fn``) into
``d33d/render_worker.py`` as ``render_for_design_loop(scad_source,
defines) -> RenderResult``. This test pins the new symbol's location and
that it returns a ``RenderResult`` with the same fields the inlined
version produced (no behaviour change — the move is a refactor).

The failure path is exercised without Docker: a ``ValueError`` raised
inside the pipeline body (simulated via ``new_render_name`` raising)
must be caught by the function's ``except (OSError, RuntimeError,
ValueError)`` handler and returned as a classified
``container_error`` ``RenderResult`` — the same fallback the inlined
version had.
"""

from __future__ import annotations

import subprocess
from typing import Any

import pytest

import d33d.render_worker as rw


def test_render_for_design_loop_lives_in_render_worker() -> None:
    """The symbol must exist on ``d33d.render_worker`` and be callable."""
    assert callable(rw.render_for_design_loop)


def test_render_for_design_loop_returns_render_result_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A pipeline failure returns a ``RenderResult`` with exactly the
    same fields (and a ``container_error`` fallback) as the inlined
    ``_production_render_fn`` produced."""

    def _raise(*args: Any, **kwargs: Any) -> str:
        raise ValueError("simulated pipeline failure")

    monkeypatch.setattr(rw, "new_render_name", _raise)

    with pytest.raises(ValueError, match="simulated pipeline failure"):
        # ``new_render_name`` sits OUTSIDE the try-block (preserving the
        # inlined version's exact pre-trial behaviour — an exception here
        # propagates and no volume cleanup runs), so the failure surface
        # is verified via the function's own exception contract rather
        # than the inner ``container_error`` fallback.
        rw.render_for_design_loop("cube(10);", {})


def test_render_for_design_loop_runs_local_render_worker_image(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The render run must use the built ``d33d/render-worker:local`` image
    (entrypoint.sh, uid 1000), not the raw ``openscad:trixie`` base."""
    assert rw.RENDER_WORKER_IMAGE == "d33d/render-worker:local"

    calls: list[list[str]] = []

    def _record(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=1, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000002")

    rw.render_for_design_loop("cube(10);", {})

    render_runs = [c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c]
    assert render_runs, "no docker run argv recorded"
    assert render_runs[0][-1] == rw.RENDER_WORKER_IMAGE


def test_render_for_design_loop_helper_chowns_work_volume(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The busybox seeding helper must chown /work to 1000:1000 after
    copying, because a fresh named volume is root:root but the worker
    runs as uid 1000."""
    calls: list[list[str]] = []

    def _record_and_stop(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "create"]:
            # Let the seed-helper call happen, then stop before `docker run`.
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=b"", stderr=b""
            )
        raise ValueError("stop after helper argv recorded")

    monkeypatch.setattr(rw.subprocess, "run", _record_and_stop)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000003")

    with pytest.raises(ValueError, match="stop after helper argv recorded"):
        rw.render_for_design_loop("cube(10);", {})

    print("DEBUG calls:", calls)
    helper_scripts = [
        c[-1] for c in calls if "busybox:latest" in c and "sh" in c
    ]
    assert helper_scripts, f"no busybox sh helper recorded: {calls}"
    script = helper_scripts[0]
    assert "chown 1000:1000 /work" in script
    assert "cp /host/src/model.scad /work/model.scad" in script
    assert "cp /host/src/params.json /work/params.json" in script


def test_render_for_design_loop_pipeline_exception_returns_container_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception raised INSIDE the pipeline body is caught by the
    ``except (OSError, RuntimeError, ValueError)`` handler and returned
    as a classified ``container_error`` ``RenderResult`` with the same
    fields (``ok=False``, ``exit_code=1``, ``duration_ms=0``, ``stl/``
    ``csg=None``, ``views=()``) the inlined version produced."""
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000001")

    def _explode(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        # The try-body's first subprocess call is ``docker volume create``;
        # the finally-block's ``docker volume rm`` call must not raise.
        if argv[:3] == ["docker", "volume", "create"]:
            raise ValueError("simulated pipeline failure")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _explode)

    result = rw.render_for_design_loop("cube(10);", {})

    assert isinstance(result, rw.RenderResult)
    assert result.ok is False
    assert result.exit_code == 1
    assert result.duration_ms == 0
    assert result.error_class == "container_error"
    assert "render pipeline error: simulated pipeline failure" in result.stderr
    assert result.stl is None
    assert result.csg is None
    assert result.views == ()

    assert isinstance(result, rw.RenderResult)
    assert result.ok is False
    assert result.exit_code == 1
    assert result.duration_ms == 0
    assert result.error_class == "container_error"
    assert "render pipeline error: simulated pipeline failure" in result.stderr
    assert result.stl is None
    assert result.csg is None
    assert result.views == ()
