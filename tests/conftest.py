"""Pytest conftest: ensure the worktree ``d33d`` package resolves.

Imports ``tests/__init__`` (the test-package init), which runs the
dependency and provenance guards at import time — before any test is
collected. See tests/__init__.py for the guard logic and rationale.

Also inserts the repository root (the parent of this ``tests/``
directory) at the front of ``sys.path`` so ``import d33d`` always finds
the package under test, even when the installed ``d33d`` egg-link points
elsewhere (e.g. a stale editable install or a parallel worktree). This
keeps ``./scripts/test`` green on a fresh checkout without Docker.
"""

from __future__ import annotations

import importlib
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

# Running the import of the tests package triggers the guard checks in
# tests/__init__.py (dependency + provenance). If either guard fails it
# calls sys.exit() with a diagnostic and the pytest session aborts
# before collecting a single test.
# Side-effect-only import via ``importlib`` — the call below is the guard.
importlib.import_module("tests")

_REPO_ROOT = Path(__file__).resolve().parents[1]
root_str = str(_REPO_ROOT)
if root_str not in sys.path:
    sys.path.insert(0, root_str)


def _project_entry_names() -> set[str]:
    p = Path.home() / ".d33d" / "projects"
    if p.is_dir():
        return {e.name for e in p.iterdir()}
    return set()


@pytest.fixture(autouse=True, scope="session")
def _preimport_part_mesh():
    """Warm up the heavy mesh stack before the first test's measurement window.

    ``d33d.part_mesh`` imports numpy and trimesh at module top (heavy
    native-extension loads — numpy's BLAS, trimesh's shapely/CGAL deps).
    If the *first* trimesh import in the process landed inside a
    worker thread during a test (e.g. the upload route's
    ``asyncio.to_thread(parse_and_repair)``), the multi-second import
    cost would be misread by that test's timing/order probe as the
    event loop being blocked — the exact Linux-CI failure of
    ``test_parse_and_repair_runs_off_event_loop`` after the eval
    staging tests ran earlier in the same session (issue #344).

    Importing the module here (session scope — runs once on the main
    thread at session start, before the first test; cached in
    ``sys.modules`` afterwards) pays the import cost once per process,
    outside any test's measurement window. The ``importlib`` form is a side-effect-only import — no module-level binding to flag.
    """
    importlib.import_module("d33d.part_mesh")
    yield


@pytest.fixture(autouse=True)
def _isolate_data_dir(tmp_path, monkeypatch):
    """Point ``D33D_DATA_DIR`` at a per-test tmp dir and clear ``db.APP_DATA_DIR``.

    After #294, ``d33d.db._default_git_path`` resolves ``projects_dir()``
    to ``$D33D_DATA_DIR`` (default ``~/.d33d``) whenever ``APP_DATA_DIR``
    is None, so ``create_project`` calls in tests were creating empty
    directories in the operator's real ``~/.d33d/projects/`` on every
    run. Steer every test at its own ``tmp_path`` and clear the
    module-level ``APP_DATA_DIR`` so a value recorded by an earlier app
    lifespan can't leak between tests.

    The tmp dir is NOT pre-created: ``projects_dir()`` creates its
    ``projects/`` child with ``mkdir(parents=True)`` on demand, and
    pre-creating ``d33d-data`` would collide with a test's own ``data_dir``
    fixture that mkdir's the same ``tmp_path`` path (``pytest`` shares one
    ``tmp_path`` per test across fixtures; a bare ``mkdir`` then raises
    FileExistsError).
    """
    import d33d.db as db_mod

    data_dir = tmp_path / "d33d-data"
    monkeypatch.setenv("D33D_DATA_DIR", str(data_dir))
    monkeypatch.setattr(db_mod, "APP_DATA_DIR", None)
    yield


# Mutable override namespace for the hermetic render pre-flight stubs
# (issue #346). Tests that deliberately exercise the REAL probe (rather
# than the hermetic "image present, label matches" default) set
# ``image_detail_override`` to the real ``_render_worker_image_detail``
# function (imported BEFORE the conftest stub is installed — the test
# module's top-level import captures the real name) or to a custom
# callable. The conftest stub (below) reads this on every call.
_render_preflight_overrides: dict[str, Callable[..., dict[str, str] | None] | None] = {
    "image_detail": None
}


def image_detail_override(func: Callable[..., dict[str, str] | None] | None) -> None:
    """Set the conftest ``_render_worker_image_detail`` stub to *func*.

    Call this at the TOP of a test that deliberately exercises the real
    probe or a specific image-detail outcome. *func* is called with the
    same ``*a, **kw`` the conftest stub would receive (the probe's
    ``image=`` / ``repo_root=`` / ``expected_hash=`` / ``rebuild_command=``
    kwargs). When *func* is ``None`` (the default) the hermetic stub
    returns ``None`` (no fault). Tests that need the real probe do::

        from d33d.design_loop import _render_worker_image_detail as _real_probe
        image_detail_override(_real_probe)   # at the top of the test

    (The import at module top captures the real function BEFORE the
    conftest stub replaces it.)
    """
    _render_preflight_overrides["image_detail"] = func


@pytest.fixture(autouse=True)
def _hermetic_render_preflight(request, monkeypatch):
    """Stub the two render pre-flight seams so the fast suite is hermetic.

    (Issue #346.) The design loop's pre-flight — ``d33d.design_loop.
    renderer_is_available`` (the ``docker info`` probe, issue #277) and
    ``d33d.design_loop._render_worker_image_detail`` (the render-worker
    image existence / build-hash label probe, issue #346) — shells out to
    real Docker. On a machine with a correctly labelled
    ``d33d/render-worker:local`` image the fast suite passes; on CI (no
    image) the image probe reports ``image_missing`` and every design-loop
    test with a mocked ``render_fn`` that does not inject the ``image_check
    `` / ``renderer_check`` seams fails with ``renderer_image_stale``.

    Stubbing BOTH seams at the module level (the same names the loop's
    default ``None`` path resolves through, and the name ``d33d.app``
    ``_lifespan`` resolves through) makes the whole fast suite independent
    of the host's Docker and images: the default path reads
    "daemon up, image present and label matches" (``None`` — no fault),
    and tests that deliberately exercise the fault paths override via the
    loop's ``renderer_check`` / ``image_check`` parameters (they win over
    the module defaults) or via :func:`image_detail_override` (which
    re-points the conftest stub to the real probe or a custom callable).

    Gated on the ``slow`` and ``live`` markers: ``tests/slow``
    (``pytestmark = pytest.mark.slow``) and ``tests/live_e2e`` (``pytestmark
    = pytest.mark.live``) exercise the REAL Docker render path and must see
    the real probes; both are excluded from CI's fast gate (``-m "not slow
    and not live"``).
    """
    if "slow" in request.keywords or "live" in request.keywords:
        yield
        return
    import d33d.design_loop as _dl

    monkeypatch.setattr(_dl, "renderer_is_available", lambda *a, **kw: True)

    def _image_detail_stub(*a, **kw):
        override = _render_preflight_overrides.get("image_detail")
        if override is not None:
            return override(*a, **kw)
        return None

    monkeypatch.setattr(_dl, "_render_worker_image_detail", _image_detail_stub)
    yield
    _render_preflight_overrides["image_detail"] = None


@pytest.fixture(autouse=True, scope="session")
def _home_d33d_projects_guard():
    """Session guard: tests must not create entries in ``~/.d33d/projects``.

    Snapshots the entry NAMES at session start and asserts that no NEW
    entries (names absent from the snapshot) exist at session end. A raw
    count comparison would flake on operator activity on this same machine
    — the dev server creates real projects (and users delete them) during
    the session — so only growth caused by a name that was not present at
    session start is reported, and the message says so.
    """
    projects_dir = Path.home() / ".d33d" / "projects"
    before = _project_entry_names()
    yield
    new = _project_entry_names() - before
    assert not new, (
        f"tests created {len(new)} new directorie(s) in the operator's "
        f"real {projects_dir} ({sorted(new)[:5]}{' …' if len(new) > 5 else ''}) — "
        f"tests must isolate D33D_DATA_DIR "
        f"(see the autouse _isolate_data_dir fixture in tests/conftest.py). "
        f"Note: a live dev server on this machine creating real projects would "
        f"also trip this; pause it when running the suite."
    )
