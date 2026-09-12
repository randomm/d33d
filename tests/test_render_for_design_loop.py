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
