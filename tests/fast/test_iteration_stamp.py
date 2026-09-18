"""Fast-layer test: the PRODUCTION path stamps the 1-based design-loop
iteration index into the per-view progress payload (issue #118 review
finding).

The loop is the only component that knows which pass it is serving — the
render worker does not. The loop stamps the 1-based index onto the
``on_progress`` hook once per iteration (``d33d.design_loop.
_stamp_on_progress_iteration``) and the render worker's ``_on_marker``
closure forwards it into every marker payload as ``iteration``. The
adapter then carries it onto the SSE ``render-view-*`` frames, where the
client reducer resets its per-view counter on a strictly-greater value.

Without the stamp the payload never carries ``iteration``, the adapter
defaults every frame to ``iteration: 0``, and on a 2+-iteration run the
SPA counter keeps accumulating ("7 of 6", "8 of 6") — the exact outcome
the ticket forbade. This file proves the production path itself stamps a
correct, 1-based, increasing index — NOT a synthetic frame.

Red-check (proven): remove the stamp call in
``run_design_loop_async`` (or the ``on_progress_iteration`` wiring in
``app.py`` / ``versions_routes.py``) and this test fails — the payload
no longer carries ``iteration``.
"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path
from typing import Any

import pytest

import d33d.render_worker as rw
from d33d.design_loop import LLMResult, run_design_loop_async


def _llm_result() -> LLMResult:
    """A minimal design-role LLMResult: fenced SCAD the loop accepts."""
    return LLMResult(
        content="```scad\n" + "cube([20, 20, 20]);\n```",
        tool_calls=(),
        prompt_hash="h",
        tier="T1",
        status="ok",
        request_body={},
    )


def _stl_failure_render() -> rw.RenderResult:
    """A non-ok render result (the loop's iteration 2 exhausts)."""
    return rw.RenderResult(
        ok=False,
        exit_code=1,
        duration_ms=1,
        error_class="syntax_error",
        stderr="ERROR: something",
        stl=None,
        csg=None,
        views=(),
    )


def _fake_container_factory(real_popen: Any) -> Any:
    """A ``subprocess.Popen`` factory: the render-worker image launches a
    bash script that writes the entrypoint's real marker lines
    (``[entrypoint] view-start <stem>`` / ``view-done <stem>`` for
    ``stl``/``csg`` and the six VIEWS views) to stderr, so the render
    worker's REAL ``_StderrReader`` + ``parse_entrypoint_markers`` +
    ``_on_marker`` path is exercised end to end (the same path a real
    container would take). Every other ``Popen`` call goes to the real
    ``subprocess.Popen`` (no infinite recursion)."""

    class _PopenFactory:
        def __call__(self, argv: list[str], **kwargs: Any) -> Any:
            if any(tok.endswith("render-worker:local") for tok in argv):
                lines = [
                    "echo '[entrypoint] view-start stl' >&2",
                    "echo '[entrypoint] view-done stl' >&2",
                    "echo '[entrypoint] view-start csg' >&2",
                    "echo '[entrypoint] view-done csg' >&2",
                ]
                for name, _cam in rw.VIEWS:
                    stem = name[:-4]
                    lines.append(f"echo '[entrypoint] view-start {stem}' >&2")
                    lines.append("sleep 0.02")
                    lines.append(f"echo '[entrypoint] view-done {stem}' >&2")
                lines.append("echo '[entrypoint] All 8 steps complete.' >&2")
                script = "\n".join(lines)
                return real_popen(
                    ["bash", "-c", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                )
            return real_popen(argv, **kwargs)

    return _PopenFactory()


def _patch_ok_render(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Patch Docker subprocesses so ``render_for_design_loop`` runs the REAL
    marker-drain + ``_on_marker`` path without a container: ``subprocess.run``
    returns success for the helper/verify/harvest calls (the harvest leaves
    real STL + CSG + 6 view PNGs on disk) and ``subprocess.Popen`` launches
    the fake marker-emitting container for the render-worker image.
    ``trimesh.load`` returns a non-degenerate fake mesh so ``classify``
    lands in ``ok``."""

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        if "cp /work/model.stl /host/" in " ".join(argv):
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    out = Path(tok.rsplit(":", 1)[0])
                    (out / "model.stl").write_bytes(b"fake-stl")
                    (out / "model.csg").write_bytes(b"fake-csg")
                    for name, _cam in rw.VIEWS:
                        (out / name).write_bytes(b"fake-png")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(
        rw.subprocess, "Popen", _fake_container_factory(subprocess.Popen)
    )
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-118stamp")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    class _FakeMesh:
        vertices = [0, 1, 2]
        is_watertight = True
        volume = 1000.0

        def merge_vertices(self, *a: Any, **k: Any) -> Any:
            return self

    class _FakeTrimesh:
        @staticmethod
        def load(*a: Any, **k: Any) -> Any:
            return _FakeMesh()

    import sys as _s

    monkeypatch.setitem(_s.modules, "trimesh", _FakeTrimesh)


async def _drive_loop(
    on_progress: Any,
    renders_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
    render_count: list[int],
) -> None:
    """Drive ``run_design_loop_async`` through two iterations with the REAL
    ``render_for_design_loop`` as ``render_fn`` (Docker subprocesses
    patched out; the marker-drain path is exercised for real).

    Iteration 1: a fully ``ok`` render — its entrypoint's real marker
    lines flow through the render worker's stderr reader and
    ``_on_marker`` (6 ``view-start`` + 6 ``view-done`` + ``stl``/``csg``
    markers).

    Iteration 2: an STL-failure render — no view markers (the stream
    stops on a failed view, by design). The stamp must have moved to 2
    (verified via the hook's ``_current`` attribute).
    """
    def _render_fn(scad_source: str, defines: dict[str, str]) -> rw.RenderResult:
        render_count[0] += 1
        if render_count[0] == 1:
            # Iteration 1: a fully ok render (real marker drain).
            return rw.render_for_design_loop(
                scad_source, defines, renders_dir=renders_dir, on_progress=on_progress
            )
        # Iteration 2: an STL failure (no view markers — the stream stops
        # on a failed view, by design).
        return _stl_failure_render()

    async def _llm_fn(*a: Any, **k: Any) -> LLMResult:
        return _llm_result()

    await run_design_loop_async(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=_render_fn,
        llm_fn=_llm_fn,
        bbox_fn=lambda r: None,
        max_iterations=2,
        request="make a 20mm cube",
        on_progress=on_progress,
    )


def test_production_path_stamps_1_based_increasing_iteration(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The production path stamps the 1-based design-loop iteration index
    into the per-view progress payload.

    The REAL ``render_for_design_loop`` is the render_fn (Docker
    subprocesses patched out, but the stderr-drain + marker-parse +
    ``_on_marker`` path is exercised for real). The loop drives two
    iterations:

    - Iteration 1: a fully ``ok`` render — the six view markers fire and
      every payload MUST carry ``iteration: 1``.
    - Iteration 2: an STL-failure render — no view markers (the stream
      stops on a failed view, by design), but the stamp must have moved
      to 2 (verified via the hook's ``_current`` attribute).

    Without the stamp (the pre-fix production wiring), the payload never
    carries ``iteration`` at all — the test fails loudly (red-check).
    """
    events: list[tuple[str, dict[str, Any]]] = []
    render_count = [0]
    _patch_ok_render(monkeypatch, tmp_path)

    def on_progress(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, dict(payload)))

    asyncio.run(
        _drive_loop(on_progress, tmp_path / "renders", monkeypatch, render_count)
    )

    # --- Iteration 1: the six view markers MUST carry iteration: 1 ---
    view_done_events = [
        (kind, payload)
        for kind, payload in events
        if kind == "view-done" and payload.get("view") not in ("stl", "csg")
    ]
    assert len(view_done_events) == 6, (
        f"expected 6 view-done events (one per VIEWS view), got "
        f"{len(view_done_events)}: {view_done_events}"
    )
    for kind, payload in view_done_events:
        assert payload.get("iteration") == 1, (
            f"view-done payload for {payload.get('view')} carries "
            f"iteration={payload.get('iteration')!r}, expected 1. "
            f"The production path did NOT stamp the 1-based iteration "
            f"index (the documented payload contract is unmet)."
        )

    # --- Iteration 2: the stamp must have moved to 2 ---
    # The hook's ``_current`` attribute (set by the loop's stamp before
    # iteration 2's render) must be 2 — proving the index increased.
    stamped = getattr(on_progress, "_current", None)
    assert stamped == 2, (
        f"the hook's _current attribute is {stamped!r}, expected 2. "
        f"The loop did NOT stamp the iteration index before iteration 2's "
        f"render — the 1-based increasing contract is broken."
    )


def test_stamp_is_not_0_for_first_pass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The first pass stamps ``iteration: 1`` (never 0 — the reducer
    treats 0 as 'unknown' by design, and the adapter defaults to 0 when
    the field is absent)."""
    events: list[tuple[str, dict[str, Any]]] = []
    render_count = [0]
    _patch_ok_render(monkeypatch, tmp_path)

    def on_progress(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, dict(payload)))

    asyncio.run(
        _drive_loop(on_progress, tmp_path / "renders", monkeypatch, render_count)
    )

    first_view_done = next(
        payload
        for kind, payload in events
        if kind == "view-done" and payload.get("view") == "view_00_front"
    )
    assert first_view_done.get("iteration") == 1, (
        f"first view-done payload carries iteration={first_view_done.get('iteration')!r}, "
        f"expected 1. The first pass must NOT emit 0 (the reducer treats 0 as unknown)."
    )


def test_frame_shape_unchanged(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The per-view frame's name, shape and other fields are unchanged:
    the payload still carries ``view`` (the stem) and ``index`` (the
    0-based view index) alongside the new ``iteration`` field."""
    events: list[tuple[str, dict[str, Any]]] = []
    render_count = [0]
    _patch_ok_render(monkeypatch, tmp_path)

    def on_progress(kind: str, payload: dict[str, Any]) -> None:
        events.append((kind, dict(payload)))

    asyncio.run(
        _drive_loop(on_progress, tmp_path / "renders", monkeypatch, render_count)
    )

    view_done_events = [
        (kind, payload)
        for kind, payload in events
        if kind == "view-done" and payload.get("view") not in ("stl", "csg")
    ]
    expected_views = [n[:-4] for n, _ in rw.VIEWS]
    for expected_view, (_, payload) in zip(expected_views, view_done_events):
        assert payload["view"] == expected_view
        assert "index" in payload, f"payload for {expected_view} missing 'index': {payload}"
        assert "iteration" in payload, (
            f"payload for {expected_view} missing 'iteration': {payload}"
        )
        # The index is still the 0-based view index (unchanged)
        expected_index = expected_views.index(expected_view)
        assert payload["index"] == expected_index, (
            f"payload for {expected_view} carries index={payload['index']!r}, "
            f"expected {expected_index}"
        )
