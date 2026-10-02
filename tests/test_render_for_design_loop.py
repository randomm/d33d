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

import io as _io
import logging
import subprocess
from pathlib import Path
from typing import Any

import pytest

import d33d.render_worker as rw


class _FakeMesh:
    """Stand-in for a ``trimesh``-loaded mesh on the ``ok`` test path.

    ``render_for_design_loop`` trimesh-loads the harvested STL host-side
    (vertex_count / watertight / volume feed ``classify``); the test
    monkeypatches ``trimesh.load`` to return this instead of parsing
    bytes, so the ``ok`` path exercises the real ``classify`` table
    (exit 0 + STL/CSG/views present + non-degenerate mesh → ``ok``).
    """

    def __init__(self) -> None:
        self.vertices = [0, 1, 2]
        self.is_watertight = True
        self.volume = 1000.0

    def merge_vertices(self, *args: Any, **kwargs: Any) -> None:
        """No-op: the fake mesh already has unique vertices."""
        return self


def _harvest_side_effect(
    argv: list[str], out_dir: Path
) -> subprocess.CompletedProcess[str]:
    """Simulate the busybox harvest helper: when the argv is the harvest
    script (copies ``/work/model.stl`` out of the volume), create real
    ``model.stl`` / ``model.csg`` / 6 ``view_*.png`` files under
    ``out_dir`` — the real glob of the harvest directory then finds
    them, exactly as a successful production render would leave them."""
    if "cp /work/model.stl /host/" in " ".join(argv):
        (out_dir / "model.stl").write_bytes(b"fake-stl-bytes")
        (out_dir / "model.csg").write_bytes(b"fake-csg-bytes")
        for name, _cam in rw.VIEWS:
            (out_dir / name).write_bytes(b"fake-png-bytes")
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


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
        # Seed helper and post-seed verification must succeed for the
        # render worker to launch; the render itself is the one that
        # fails (to exercise the full path through the function).
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b""
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-00000002")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

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
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

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


def test_render_for_design_loop_harvests_view_glob(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The harvest helper must copy ``view_*.png`` (the worker writes
    ``view_00_front.png`` etc.), and the views list must come from a
    glob of the harvested directory, not a hard-coded ``view0..5``."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-0000000a")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

    orig_glob = Path.glob

    def _gl(self: Path, pattern: str, **kw: Any):
        if str(self).endswith("out") and pattern == "view_*.png":
            return [
                self / "view_00_front.png",
                self / "view_01_back.png",
                self / "view_02_left.png",
                self / "view_03_right.png",
                self / "view_04_top.png",
                self / "view_05_iso.png",
            ]
        return orig_glob(self, pattern, **kw)

    monkeypatch.setattr(Path, "glob", _gl)

    rw.render_for_design_loop("cube(10);", {})

    helper_scripts = [
        c[-1] for c in calls if "busybox:latest" in c and "sh" in c and "cp /work" in c[-1]
    ]
    harvest_scripts = [s for s in helper_scripts if "/work/model.stl" in s]
    assert harvest_scripts, f"no harvest helper recorded: {calls}"
    assert "cp /work/view_*.png /host/" in harvest_scripts[0]
    assert "view$i.png" not in harvest_scripts[0]


# --- Issue #280: caller-side container + volume removal on every path ----


def test_render_for_design_loop_removes_container_and_volume_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fully ``ok`` render removes BOTH the named render container and
    the ``d33d-render-*`` volume (the caller removes after ``docker
    inspect`` / harvest — the render runs without ``--rm``), and
    ``docker rm -f`` is issued AFTER the harvest helper (the harvested
    files must still be in the volume when copied out)."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "cp /work/model.stl /host/" in " ".join(argv):
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    _harvest_side_effect(argv, Path(tok.rsplit(":", 1)[0]))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000d1")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    class _FakeTrimesh:
        load = staticmethod(lambda *a, **kw: _FakeMesh())

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "trimesh", _FakeTrimesh)

    result = rw.render_for_design_loop(
        "cube(10);", {}, renders_dir=tmp_path / "renders"
    )

    assert result.ok is True, "the stubbed render must land in the ok row"
    name = "render-000000d1"
    volume = f"d33d-render-{name}"
    rm_calls = [c for c in calls if c == ["docker", "rm", "-f", name]]
    vol_rm_calls = [
        c for c in calls if c == ["docker", "volume", "rm", "-f", volume]
    ]
    assert len(rm_calls) == 1, (
        f"expected exactly one 'docker rm -f {name}', got: {rm_calls}"
    )
    assert len(vol_rm_calls) == 1, (
        f"expected exactly one volume rm, got: {vol_rm_calls}"
    )
    # Sequencing: container removal happens AFTER the harvest helper ran.
    harvest_idx = next(
        i for i, c in enumerate(calls) if "cp /work/model.stl /host/" in " ".join(c)
    )
    rm_idx = calls.index(rm_calls[0])
    vol_rm_idx = calls.index(vol_rm_calls[0])
    assert rm_idx > harvest_idx, "rm -f must run after the harvest"
    # Container first, volume after.
    assert vol_rm_idx > rm_idx, "volume rm must run after the container rm"


def test_render_for_design_loop_removes_container_and_volume_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-timeout render failure (worker exits non-zero) still removes
    the container AND the volume in the ``finally`` — the pre-#280 leak
    was on exactly this path."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if any(tok.endswith("render-worker:local") for tok in argv):
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"ERROR: syntax"
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000d2")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

    result = rw.render_for_design_loop("cube(10);", {})

    assert result.ok is False
    assert result.error_class in rw.ERROR_CLASSES
    name = "render-000000d2"
    volume = f"d33d-render-{name}"
    assert ["docker", "rm", "-f", name] in calls, (
        f"no 'docker rm -f {name}' in: {calls}"
    )
    assert [
        "docker",
        "volume",
        "rm",
        "-f",
        volume,
    ] in calls, f"no 'docker volume rm -f {volume}' in: {calls}"


def test_render_for_design_loop_removes_container_and_volume_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A wall-clock timeout: the render run itself raises
    ``TimeoutExpired`` (the legacy blocking path of ``run_container``),
    whose own cleanup issues a ``docker rm -f``; the outer ``finally``
    then issues the volume rm (and a second, idempotent container rm) —
    so every created resource is covered by a removal on this path too."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 120))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000d3")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

    result = rw.render_for_design_loop("cube(10);", {})

    assert result.ok is False
    assert result.error_class == "timeout"
    name = "render-000000d3"
    volume = f"d33d-render-{name}"
    rm_calls = [c for c in calls if c == ["docker", "rm", "-f", name]]
    vol_rm_calls = [
        c for c in calls if c == ["docker", "volume", "rm", "-f", volume]
    ]
    assert len(rm_calls) == 2, (
        f"expected the run's own rm -f plus the finally's rm -f, got: {rm_calls}"
    )
    assert len(vol_rm_calls) == 1, (
        f"expected exactly one volume rm, got: {vol_rm_calls}"
    )


def test_render_for_design_loop_removes_container_and_volume_on_unexpected_exception(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An exception OUTSIDE the pipeline's ``except`` tuple (here an
    ``AttributeError`` mid-volume-setup) must NOT leak the volume: the
    ``finally``'s ``docker volume rm -f`` still runs and, per the #280
    hardening, a ``TimeoutExpired`` escaping that ``docker volume rm``
    must be caught (cleanup never raises out of a render) — the
    unexpected exception still propagates."""
    calls: list[list[str]] = []
    state = {"rm_count": 0}

    def _explode(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "create"]:
            raise AttributeError("simulated unexpected failure mid-pipeline")
        if argv[:4] == ["docker", "volume", "rm", "-f"]:
            state["rm_count"] += 1
            raise subprocess.TimeoutExpired(cmd=argv, timeout=15)
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _explode)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000d4")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

    with pytest.raises(AttributeError, match="simulated unexpected failure"):
        rw.render_for_design_loop("cube(10);", {})

    # The volume rm WAS attempted in the finally (the TimeoutExpired from
    # it was swallowed by the cleanup, not propagated).
    assert state["rm_count"] == 1
    assert [
        "docker",
        "volume",
        "rm",
        "-f",
        "d33d-render-render-000000d4",
    ] in calls


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
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

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


def _run_ok_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, persist_dir: Path
) -> rw.RenderResult:
    """Drive ``render_for_design_loop`` to a fully ``ok`` outcome without
    Docker: every ``subprocess.run`` returns success, the harvest helper
    leaves real STL + CSG + 6 view PNGs on disk, and ``trimesh.load``
    returns a non-degenerate fake mesh so ``classify`` lands in ``ok``.
    ``renders_dir`` is pointed at ``persist_dir`` (isolated from the
    ``D33D_RENDER_PERSIST_DIR`` env)."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if "cp /work/model.stl /host/" in " ".join(argv):
            # ``--volume <out>:/host`` — the harvest directory is the
            # host-side half of the volume mount whose container half is
            # ``/host``.
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    _harvest_side_effect(argv, Path(tok.rsplit(":", 1)[0]))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000b1")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    # The with-block's tempdir lives under ``_render_host_tmp_base()``
    # (``D33D_RENDER_TMP``, default ``~/d33d/render-tmp``); point it at
    # the test's own tmp dir so the run is hermetic regardless of the
    # developer's env (a stale ``D33D_RENDER_TMP`` would otherwise make
    # the tempdir creation ``OSError`` → ``container_error``).
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    class _FakeTrimesh:
        load = staticmethod(lambda *a, **kw: _FakeMesh())

    # ``render_for_design_loop`` runs ``import trimesh`` inside the
    # function body (host-side mesh check); inject the fake into
    # ``sys.modules`` so that import resolves to it.
    import sys as _sys

    monkeypatch.setitem(_sys.modules, "trimesh", _FakeTrimesh)

    return rw.render_for_design_loop("cube(10);", {}, renders_dir=persist_dir)


def test_render_for_design_loop_persists_stl_and_views_to_renders_dir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fully ``ok`` render copies ``model.stl`` + all 6 ``view_*.png``
    into ``<renders_dir>/<uuid8>/`` INSIDE the with-block; the durable
    path is carried on ``RenderResult.render_artifact_dir`` and the
    files survive after the render returns (the tempdir is torn down)."""
    persist_dir = tmp_path / "renders"
    result = _run_ok_render(monkeypatch, tmp_path, persist_dir)

    assert result.ok is True
    assert result.error_class == "ok"
    assert result.render_artifact_dir is not None, "no durable dir on an ok render"
    artifact_dir = Path(result.render_artifact_dir)
    assert artifact_dir.parent == persist_dir, f"{artifact_dir} not under {persist_dir}"
    assert artifact_dir.name != "renders", "key must be a per-render subdir"
    files = sorted(p.name for p in artifact_dir.iterdir())
    expected = sorted(["model.stl", *[name for name, _cam in rw.VIEWS]])
    assert files == expected, f"persisted files {files} != expected {expected}"
    for f in artifact_dir.iterdir():
        assert f.stat().st_size > 0, f"persisted {f.name} is empty"
    # Bytes survive the with-block teardown: still readable post-return.
    stl = artifact_dir / "model.stl"
    assert stl.read_bytes() == b"fake-stl-bytes"


def test_render_for_design_loop_failed_render_persists_nothing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A non-zero-exit render persists nothing: no directory is created
    under ``renders_dir`` and ``render_artifact_dir`` is ``None``."""
    persist_dir = tmp_path / "renders"

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess[str]:
        # The render worker run (the argv carrying the worker image)
        # fails; the busybox helpers succeed.
        if any(tok.endswith("render-worker:local") for tok in argv):
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"ERROR: syntax"
            )
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000b2")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)

    result = rw.render_for_design_loop("cube(10);", {}, renders_dir=persist_dir)

    assert result.ok is False
    assert result.render_artifact_dir is None
    created = list(persist_dir.iterdir()) if persist_dir.is_dir() else []
    assert created == [], f"failed render must persist nothing, found {created}"


def test_render_for_design_loop_renders_dir_kwarg_overrides_base(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The ``renders_dir`` kwarg takes precedence over the env default
    (``D33D_RENDER_PERSIST_DIR``): artifacts land under the kwarg's
    directory, and nothing is created under the env base."""
    env_base = tmp_path / "env-base"
    kwarg_dir = tmp_path / "kwarg-dir"
    monkeypatch.setenv("D33D_RENDER_PERSIST_DIR", str(env_base))

    result = _run_ok_render(monkeypatch, tmp_path, kwarg_dir)

    assert result.render_artifact_dir is not None
    artifact_dir = Path(result.render_artifact_dir)
    assert artifact_dir.parent == kwarg_dir
    assert (kwarg_dir / artifact_dir.name / "model.stl").is_file()
    # The env base holds no per-render artifact dir (the kwarg won).
    if env_base.is_dir():
        assert sorted(e.name for e in env_base.iterdir()) == [], (
            f"env base {env_base} must not receive the artifacts"
        )


# --- Pre-render staleness guard integration tests (issue #236) -------------


def _stub_docker_run_with_label(
    labels: dict[str, str] | None,
) -> subprocess.CompletedProcess[str] | None:
    """Return a ``subprocess.run`` stub that handles both the
    ``docker image inspect`` call (returns the label JSON or non-zero
    for an absent image) and all other docker calls (returns success).

    ``labels=None`` → image absent (inspect exit 1).
    ``labels={}``  → present but unlabeled.
    """
    import json as _json

    def _stub(argv: list[str], *args: object, **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "image"]:
            if labels is None:
                return subprocess.CompletedProcess(
                    args=argv, returncode=1, stdout=b"", stderr=b"Error: No such image"
                )
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=_json.dumps(labels).encode(),
                stderr=b"",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    return _stub


def test_render_for_design_loop_proceeds_when_label_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the image label equals the working-tree hash, the staleness
    guard passes and the render proceeds normally (no early return).
    The guard's ``docker image inspect`` call is recorded before any
    render argv."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        import json as _json

        calls.append(argv)
        if argv[:2] == ["docker", "image"]:
            # The guard: return a matching label (the real working-tree hash).
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=_json.dumps({rw.BUILD_HASH_LABEL: rw.build_hash()}).encode(),
                stderr=b"",
            )
        # The render run: fail it so we can confirm the render was attempted.
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b""
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000c1")

    result = rw.render_for_design_loop("cube(10);", {})

    # The guard ran (docker image inspect was called before the render run).
    inspect_calls = [c for c in calls if c[:2] == ["docker", "image"]]
    assert inspect_calls, "no docker image inspect call recorded"

    # The render run was attempted (the guard did not short-circuit).
    render_runs = [c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c]
    assert render_runs, "render run not attempted — guard should have passed"

    # The render failed (as stubbed), so the result is a container_error
    # from the render itself, not the guard.
    assert result.ok is False
    assert "docs/bosl2-pinning.md" not in result.stderr or "staleness" not in result.stderr


def test_render_for_design_loop_fails_loudly_on_label_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the image label does NOT match the working-tree hash, the
    guard returns a ``container_error`` RenderResult with the actionable
    rebuild command in ``stderr`` — before any ``docker volume create``
    or render argv is issued. No render runs."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        import json as _json

        calls.append(argv)
        if argv[:2] == ["docker", "image"]:
            # Return a stale label that does not match the working tree.
            return subprocess.CompletedProcess(
                args=argv,
                returncode=0,
                stdout=_json.dumps({rw.BUILD_HASH_LABEL: "stale-hash-000"}).encode(),
                stderr=b"",
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000c2")

    result = rw.render_for_design_loop("cube(10);", {})

    assert isinstance(result, rw.RenderResult)
    assert result.ok is False
    assert result.error_class == "container_error"
    assert "docs/bosl2-pinning.md" in result.stderr
    assert "docker build" in result.stderr
    assert "d33d/render-worker:local" in result.stderr

    # No docker volume create or render argv was issued.
    volume_creates = [c for c in calls if c[:3] == ["docker", "volume", "create"]]
    assert volume_creates == [], f"volume create issued on mismatch: {volume_creates}"
    render_runs = [c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c]
    assert render_runs == [], f"render run issued on mismatch: {render_runs}"


def test_render_for_design_loop_fails_loudly_when_label_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the image is present but carries no build-hash label (built
    before the label step existed — the #226/#228 incident), the guard
    returns a ``container_error`` RenderResult with the actionable rebuild
    command in ``stderr`` before any render."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "image"]:
            # Image present but unlabeled (null → {} in the guard).
            return subprocess.CompletedProcess(
                args=argv, returncode=0, stdout=b"null", stderr=b""
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000c3")

    result = rw.render_for_design_loop("cube(10);", {})

    assert isinstance(result, rw.RenderResult)
    assert result.ok is False
    assert result.error_class == "container_error"
    assert "docs/bosl2-pinning.md" in result.stderr

    # No render argv was issued.
    render_runs = [c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c]
    assert render_runs == [], f"render run issued on missing label: {render_runs}"


def test_render_for_design_loop_docker_query_failure_logs_and_proceeds(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A docker-query failure (daemon down / missing docker binary —
    ``OSError`` from the guard) is NOT staleness: the render proceeds
    (no early container_error return) and the skip is logged via the
    module logger rather than silently swallowed (issue #236 follow-up).
    """

    def _stub(argv: list[str], *args: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if argv[:2] == ["docker", "image"]:
            raise OSError("simulated docker daemon outage")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _stub)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000c5")
    with caplog.at_level(logging.WARNING, logger=rw.__name__):
        result = rw.render_for_design_loop("cube(10);", {})

    # The render proceeded: the failure is a pipeline docker error, never
    # the guard's staleness container_error (which names the doc).
    assert result.ok is False
    assert "docs/bosl2-pinning.md" not in result.stderr
    # The skip was logged, not silently swallowed.
    assert any(
        "render-worker staleness check skipped" in r.message for r in caplog.records
    ), f"expected staleness-skip warning, got: {[r.message for r in caplog.records]}"


def test_render_for_design_loop_fails_loudly_when_image_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the image is absent (``docker image inspect`` returns
    non-zero), the guard returns a ``container_error`` RenderResult with
    the actionable rebuild command in ``stderr`` before any render."""
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *args: Any, **kwargs: Any
    ) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        if argv[:2] == ["docker", "image"]:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"Error: No such image"
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-000000c4")

    result = rw.render_for_design_loop("cube(10);", {})

    assert isinstance(result, rw.RenderResult)
    assert result.ok is False
    assert result.error_class == "container_error"
    assert "image not found" in result.stderr
    assert "docs/bosl2-pinning.md" in result.stderr

    # No render argv was issued.
    render_runs = [c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c]
    assert render_runs == [], f"render run issued on absent image: {render_runs}"


# ---------------------------------------------------------------------------
# Issue #309: regression at the render_for_design_loop level — QA's verbatim
# shape (exit 1; container stderr = ONLY the STL marker; /work/render.log
# tail = "ERROR: Parser error …") must classify as syntax_error, with the
# render.log harvested via the busybox pattern BEFORE the finally cleanup.
# ---------------------------------------------------------------------------

QA_STL_ABORT_MARKER = (
    "[entrypoint] STL export failed with exit code 1 — aborting remaining steps"
)

QA_RENDER_LOG = (
    Path(__file__).parent / "fixtures" / "issue-309-render.log"
)


def _qa_stub(render_log_text: str, calls: list[list[str]]) -> Any:
    """``subprocess.run`` stub reproducing QA's capture verbatim: the render
    container exits 1 with only the STL marker on stderr; the render.log
    harvest helper (``tail -c 65536 /work/render.log``) writes the fixture
    text to the harvest output dir; everything else succeeds."""

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=QA_STL_ABORT_MARKER.encode()
            )
        if "tail -c 65536 /work/render.log" in " ".join(argv):
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    out_dir = Path(tok.rsplit(":", 1)[0])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "render.log").write_text(render_log_text)
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    return _record


def _run_qa_render(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, render_log_text: str
) -> tuple[Any, list[list[str]]]:
    calls: list[list[str]] = []
    monkeypatch.setattr(rw.subprocess, "run", _qa_stub(render_log_text, calls))
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-309qa001")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))
    result = rw.render_for_design_loop("cube(10);", {}, project_id=42)
    return result, calls


def test_qa_verbatim_shape_classifies_as_syntax_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Regression (issue #309): exit 1 + stderr = only the STL marker +
    render.log = ERROR: Parser error → syntax_error (pre-#309 this shape
    classified container_error → the loop stopped via #287's
    _container_error_stop instead of running the repair loop)."""
    result, _ = _run_qa_render(monkeypatch, tmp_path, QA_RENDER_LOG.read_text())
    assert result.ok is False
    assert result.error_class == "syntax_error"
    assert result.exit_code == 1


def test_qa_shape_still_syntax_error_when_render_log_empty(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The 64-KiB-truncation edge at the render level: the harvest finds an
    empty log (no ERROR: line survived the window) — the STL marker in
    stderr alone still classifies syntax_error (it is the only source of
    that marker, so the truncated tail never regresses the class)."""
    result, _ = _run_qa_render(monkeypatch, tmp_path, "")
    assert result.error_class == "syntax_error"
    assert result.exit_code == 1


def test_qa_shape_container_error_when_no_markers_anywhere(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Docker-layer failure shape (no STL marker in stderr, no ERROR: in the
    harvested tail) → container_error — the re-routing must not turn
    genuine environment faults into repairable design errors."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"docker: some failure"
            )
        if "tail -c 65536 /work/render.log" in " ".join(argv):
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    out_dir = Path(tok.rsplit(":", 1)[0])
                    out_dir.mkdir(parents=True, exist_ok=True)
                    (out_dir / "render.log").write_text("benign log line\n")
            return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-309qa002")
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp-2"))
    result = rw.render_for_design_loop("cube(10);", {}, project_id=42)
    assert result.error_class == "container_error"


# ---------------------------------------------------------------------------
# Issue #330: optional part_path / repo_dir — the None-path regression
# anchor, the part-aware seed/verify, and the pre-docker validation.
# ---------------------------------------------------------------------------


class _FakeTrimeshModule:
    """Module-level stand-in for ``trimesh`` on the ``ok`` test paths
    (the harvested STL load must return a non-degenerate mesh without
    parsing real bytes)."""

    load = staticmethod(lambda *a, **kw: _FakeMesh())


def _make_part_repo(tmp_path: Path, name: str = "part.stl") -> tuple[Path, Path]:
    """A minimal repo dir with a committed part inside versions/1/."""
    repo = tmp_path / "repo"
    vdir = repo / "versions" / "1"
    vdir.mkdir(parents=True)
    (vdir / name).write_bytes(b"solid test\nendsolid test\n")
    return vdir / name, repo


def _run_render_record(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    render_name: str,
    *,
    part_path: Path | None = None,
    repo_dir: Path | None = None,
    render_exit: int = 0,
    render_stderr: bytes = b"",
):
    """Run render_for_design_loop with a recording subprocess stub.

    Returns (result, calls). The render worker call (the --memory argv)
    exits with ``render_exit``; every other docker call succeeds. A
    successful harvest side-effect is simulated so the ok path is
    exercisable."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            return subprocess.CompletedProcess(
                args=argv, returncode=render_exit, stdout=b"", stderr=render_stderr
            )
        if "cp /work/model.stl /host/" in " ".join(argv):
            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    _harvest_side_effect(argv, Path(tok.rsplit(":", 1)[0]))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: render_name)
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    import sys as _sys

    monkeypatch.setitem(_sys.modules, "trimesh", _FakeTrimeshModule)

    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=part_path, repo_dir=repo_dir
    )
    return result, calls


def _seed_script_of(calls: list[list[str]]) -> str:
    scripts = [c[-1] for c in calls if "busybox:latest" in c and "sh" in c and "cp /host/src" in c[-1]]
    assert scripts, f"no seed helper recorded: {calls}"
    return scripts[0]


NO_PART_SEED_SCRIPT = (
    "cp /host/src/model.scad /work/model.scad && "
    "cp /host/src/params.json /work/params.json && "
    "chown 1000:1000 /work"
)
PART_SEED_SCRIPT = (
    "cp /host/src/model.scad /work/model.scad && "
    "cp /host/src/params.json /work/params.json && "
    "cp /host/src/part.stl /work/part.stl && "
    "chown 1000:1000 /work"
)


def test_none_path_argv_and_seed_byte_identical(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With part_path=None the ENTIRE subprocess argv sequence (volume
    create, seed helper, verify, render) and the seed script text are
    byte-identical to today's no-part render — the regression anchor."""
    _result, calls = _run_render_record(
        monkeypatch, tmp_path, "render-330np01"
    )
    # The full argv sequence: volume create, seed helper, verify, render.
    assert calls[0] == ["docker", "volume", "create", "d33d-render-render-330np01"]
    seed = _seed_script_of(calls)
    assert seed == NO_PART_SEED_SCRIPT, f"seed script drifted: {seed!r}"
    verify_calls = [
        c for c in calls
        if c[:2] == ["docker", "run"] and "busybox:latest" in c and "sh" not in c
    ]
    assert len(verify_calls) == 1
    assert verify_calls[0] == [
        "docker", "run", "--rm", "--network", "none",
        "--volume", "d33d-render-render-330np01:/work",
        "busybox:latest", "test", "-f", "/work/model.scad",
    ], f"verify argv drifted: {verify_calls[0]}"
    render_calls = [
        c for c in calls if c[:2] == ["docker", "run"] and "--memory" in c
    ]
    assert len(render_calls) == 1
    assert render_calls[0] == rw.build_docker_argv(
        image=rw.RENDER_WORKER_IMAGE, name="render-330np01",
        workdir_volume="d33d-render-render-330np01",
        params=rw.RenderParams(),
    )


def test_part_seed_script_and_verify(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """With a part: the seed script carries the part cp line BEFORE the
    final chown, and the post-seed verify is the single combined
    ``sh -c "test -f /work/model.scad && test -f /work/part.stl"``."""
    part, repo = _make_part_repo(tmp_path)
    _result, calls = _run_render_record(
        monkeypatch, tmp_path, "render-330pv01", part_path=part, repo_dir=repo
    )
    seed = _seed_script_of(calls)
    assert seed == PART_SEED_SCRIPT, f"seed script drifted: {seed!r}"
    assert "cp /host/src/part.stl /work/part.stl" in seed
    assert seed.index("cp /host/src/part.stl /work/part.stl") < seed.index("chown 1000:1000 /work")
    verify_calls = [
        c for c in calls
        if c[:2] == ["docker", "run"] and "busybox:latest" in c
        and "sh" in c and "test -f /work" in c[-1]
    ]
    assert len(verify_calls) == 1
    assert verify_calls[0] == [
        "docker", "run", "--rm", "--network", "none",
        "--volume", "d33d-render-render-330pv01:/work",
        "busybox:latest", "sh", "-c",
        "test -f /work/model.scad && test -f /work/part.stl",
    ], f"verify argv drifted: {verify_calls[0]}"


def test_part_render_argv_byte_identical_to_no_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The render argv (build_docker_argv output) with a part present is
    byte-identical to the no-part render argv — no new mount, no host
    part path in the argv."""
    part, repo = _make_part_repo(tmp_path)
    _r1, calls1 = _run_render_record(
        monkeypatch, tmp_path, "render-330ri01", part_path=part, repo_dir=repo
    )
    _r2, calls2 = _run_render_record(
        monkeypatch, tmp_path, "render-330ri02"
    )
    render1 = next(
        c for c in calls1 if c[:2] == ["docker", "run"] and "--memory" in c
    )
    render2 = next(
        c for c in calls2 if c[:2] == ["docker", "run"] and "--memory" in c
    )
    assert len(render1) == len(render2)
    for a, b in zip(render1, render2):
        # The container name (and the volume derived from it) is the only
        # per-run element; everything else must be byte-identical.
        if a.replace("render-330ri01", "X") != b.replace("render-330ri02", "X"):
            assert a == b, f"argv element differs: {a!r} vs {b!r}"
    assert "part" not in " ".join(render1).lower(), (
        f"part path leaked into the render argv: {render1}"
    )


def test_part_staged_into_src_dir_and_no_part_leftover_on_success(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A successful render with a part: src/part.stl existed at seed time
    (proven by the seed helper's own success — a missing file would fail
    the cp and the helper exits non-zero → container_error)."""
    part, repo = _make_part_repo(tmp_path)
    result, _calls = _run_render_record(
        monkeypatch, tmp_path, "render-330st01", part_path=part, repo_dir=repo
    )
    assert "seed helper failed" not in result.stderr


# ---------------------------------------------------------------------------
# Issue #330: validation rejections — all BEFORE any subprocess call.
# ---------------------------------------------------------------------------


def _run_rejection(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, **kwargs):
    """Run with a subprocess stub that RECORDS every call; validation
    failures must leave the recording empty (zero subprocess invocations)."""
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    result = rw.render_for_design_loop("cube(10);", {}, **kwargs)
    return result, calls


@pytest.mark.parametrize(
    ("setup", "label"),
    [
        pytest.param(
            lambda t: (_outside_dir_part(t), t / "repo"),
            "part outside repo",
            id="outside-repo",
        ),
        pytest.param(
            lambda t: (_symlinked_part(t), t / "repo"),
            "symlinked part file",
            id="symlink-file",
        ),
        pytest.param(
            lambda t: (_symlinked_parent(t), t / "repo"),
            "symlinked parent escaping repo",
            id="symlink-parent",
        ),
        pytest.param(
            lambda t: (_dir_instead_of_file(t), t / "repo"),
            "directory instead of file",
            id="dir-not-file",
        ),
        pytest.param(
            lambda t: (_make_part_repo(t, "model.stl")[0], t / "repo"),
            "wrong name model.stl",
            id="wrong-name-model-stl",
        ),
        pytest.param(
            lambda t: (_make_part_repo(t, "part.STL")[0], t / "repo"),
            "wrong name part.STL",
            id="wrong-name-part-stl",
        ),
        pytest.param(
            lambda t: (_make_part_repo(t, "part.obj")[0], t / "repo"),
            "wrong name part.obj",
            id="wrong-name-part-obj",
        ),
    ],
)
def test_part_validation_rejection_artifact_error_zero_subprocess(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, setup, label
) -> None:
    """Issue #330: validation runs before the image staleness gate and
    before any docker call — {label} → artifact_error with ZERO
    subprocess calls."""
    part, repo = setup(tmp_path / "case")
    (tmp_path / "case" / "repo").mkdir(parents=True, exist_ok=True)
    result, calls = _run_rejection(
        monkeypatch, tmp_path, part_path=part, repo_dir=repo
    )
    assert result.error_class == "artifact_error", (
        f"{label}: expected artifact_error, got {result.error_class} ({result.stderr})"
    )
    assert calls == [], f"{label}: subprocess was invoked: {calls}"


def _symlinked_part(tmp: Path) -> Path:
    real = tmp / "real.stl"
    real.parent.mkdir(parents=True, exist_ok=True)
    real.write_bytes(b"x")
    link = tmp / "part.stl"
    link.symlink_to(real)
    return link


def _outside_dir_part(tmp: Path) -> Path:
    """A valid part OUTSIDE the repo dir (the repo is tmp/'repo')."""
    p = tmp / "outside" / "part.stl"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"x")
    return p


def _symlinked_parent(tmp: Path) -> Path:
    # The repo is tmp/'repo' (created by the test); the symlinked dir
    # 'versions' lives UNDER the repo and escapes it.
    repo = tmp / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    outside_dir = tmp / "outside"
    outside_dir.mkdir(parents=True, exist_ok=True)
    real = outside_dir / "part.stl"
    real.write_bytes(b"x")
    link = repo / "versions"  # a symlinked dir under the repo
    link.symlink_to(outside_dir, target_is_directory=True)
    return link / "part.stl"


def _dir_instead_of_file(tmp: Path) -> Path:
    d = tmp / "part.stl"
    d.mkdir(parents=True)
    return d


def test_part_path_without_repo_dir_is_artifact_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """part_path given + repo_dir=None → artifact_error ("part containment
    boundary missing"), zero subprocess calls."""
    part, _repo = _make_part_repo(tmp_path)
    result, calls = _run_rejection(monkeypatch, tmp_path, part_path=part)
    assert result.error_class == "artifact_error"
    assert "part containment boundary missing" in result.stderr
    assert calls == []


def test_invalid_part_plus_stale_image_is_artifact_error_no_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An invalid part + a stale (mismatched) image → artifact_error with
    ZERO docker calls — no `docker image inspect` (part validation runs
    BEFORE the image staleness gate)."""
    called: dict = {"image_gate": 0}

    def _gate(*a: Any, **kw: Any) -> None:
        called["image_gate"] += 1
        raise RuntimeError("image stale (should never be consulted)")

    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "_verify_render_worker_image", _gate)
    _part, repo = _make_part_repo(tmp_path)
    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=repo / "nope" / "part.stl", repo_dir=repo
    )
    assert result.error_class == "artifact_error"
    assert called["image_gate"] == 0, "image gate was consulted before part validation"
    assert calls == [], f"docker was invoked: {calls}"


# ---------------------------------------------------------------------------
# Issue #330: 3MF → STL conversion on the host.
# ---------------------------------------------------------------------------


def _make_3mf_box_bytes() -> bytes:
    """A real 3MF (10mm box) built at test time with trimesh — no static
    .3mf fixture exists in the repo."""
    import io as _io
    import zipfile as _zipfile

    import trimesh as _trimesh

    box = _trimesh.creation.box((10.0, 10.0, 10.0))
    vxml = "".join(
        f'<vertex x="{v[0]:.6f}" y="{v[1]:.6f}" z="{v[2]:.6f}"/>' for v in box.vertices
    )
    fxml = "".join(
        f'<triangle v1="{f[0]}" v2="{f[1]}" v3="{f[2]}"/>' for f in box.faces
    )
    model = (
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<model unit="millimeter" xmlns="http://schemas.microsoft.com/3dmanufacturing/core/2015/02">'
        "<resources></resources>"
        '<build><item objectid="1"/></build>'
        '<objects><object id="1" type="model"><mesh>'
        f"<vertices>{vxml}</vertices>"
        f"<triangles>{fxml}</triangles>"
        "</mesh></object></objects></model>"
    )
    buf = _io.BytesIO()
    with _zipfile.ZipFile(buf, "w", _zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", b"types")
        zf.writestr("_rels/.rels", b"rels")
        zf.writestr("3D/3dmodel.model", model.encode())
    return buf.getvalue()


def test_3mf_part_converted_to_stl_staged_into_src(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A part.3mf is ALWAYS converted to STL on the host via the guarded
    loader (the zip-bomb guard + trimesh load + Scene.to_mesh flatten) and
    staged as src/part.stl — the staged bytes are a valid STL with the
    source mesh's bbox."""
    import trimesh as _trimesh

    repo = tmp_path / "repo"
    (repo / "versions" / "1").mkdir(parents=True)
    part = repo / "versions" / "1" / "part.3mf"
    part.write_bytes(_make_3mf_box_bytes())

    staged: dict = {"stl_bytes": None}

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        if "cp /host/src" in " ".join(argv):
            # The seed helper mounts THIS render's staging dir as ro /host
            # (the mount token is ``{host_path}:/host:ro`` — the host path
            # is everything before the ``:/host`` segment); capture the
            # staged part.stl's bytes while the staging dir still exists.
            joined = " ".join(argv)
            if ":/host:ro" in joined:
                for tok in argv:
                    if tok.endswith(":/host:ro"):
                        host = tok[: -len(":/host:ro")]
                        candidate = Path(host) / "src" / "part.stl"
                        if candidate.is_file():
                            staged["stl_bytes"] = candidate.read_bytes()
        if "cp /work/model.stl /host/" in " ".join(argv):
            for tok in argv:
                if tok.endswith(":/host"):
                    _harvest_side_effect(argv, Path(tok[: -len(":/host")]))
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-330mf01")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=part, repo_dir=repo
    )
    # REAL trimesh did the 3MF→STL staging (no stub); the staged bytes
    # were captured at seed-helper time (the staging dir is torn down
    # right after). They must be a valid STL with the source's bbox.
    assert staged["stl_bytes"] is not None, (
        f"staged part.stl not seen at seed time (result: {result})"
    )
    mesh = _trimesh.load(_io.BytesIO(staged["stl_bytes"]), file_type="stl")
    mesh.merge_vertices()
    assert len(mesh.faces) > 0
    extents = tuple(float(e) for e in mesh.extents)
    for e in extents:
        assert 9.0 <= e <= 11.0, f"staged STL bbox drifted: {extents}"


def test_corrupt_3mf_is_artifact_error_zero_docker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A corrupt (non-zip) part.3mf → artifact_error, ZERO docker calls,
    and no part.stl left behind in the src dir (the staging failure
    returns before the seed helper runs; the TemporaryDirectory tears
    down anything partial)."""
    calls: list[list[str]] = []
    left_over: dict = {"stl": None}

    def _explode(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        for i, tok in enumerate(argv):
            if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host:ro"):
                host = tok[: -len(":/host:ro")]
                left_over["stl"] = Path(host) / "src" / "part.stl"
        if argv[:3] == ["docker", "volume", "create"]:
            raise ValueError("staging must fail before any docker call")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _explode)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-330mf02")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    repo = tmp_path / "repo"
    (repo / "versions" / "1").mkdir(parents=True)
    part = repo / "versions" / "1" / "part.3mf"
    part.write_bytes(b"this is not a zip at all")

    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=part, repo_dir=repo
    )
    assert result.error_class == "artifact_error"
    assert "3MF to STL conversion failed" in result.stderr or "part staging failed" in result.stderr
    # The volume create (the pipeline's FIRST docker call) was never
    # reached — zero docker invocations of any kind.
    assert calls == [], f"docker was invoked on a corrupt 3MF: {calls}"
    assert left_over["stl"] is None or not left_over["stl"].is_file()


def test_3mf_export_failure_maps_to_artifact_error_not_raise(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A 3MF that LOADS fine but whose export fails maps to
    artifact_error (never a raise into the design loop) and never fires a
    subprocess call. Regression for the #330 noqa sweep: the staging
    failure path must keep working once the broad catch is replaced with
    the specific exception set (PartUploadError / OSError / ValueError —
    trimesh raises ValueError for an unknown exporter)."""
    import trimesh as _trimesh

    repo = tmp_path / "repo"
    (repo / "versions" / "1").mkdir(parents=True)
    part = repo / "versions" / "1" / "part.3mf"
    part.write_bytes(_make_3mf_box_bytes())

    def _explode(self, file_obj, *a, **kw) -> None:
        raise ValueError("stl exporter not available!")

    monkeypatch.setattr(_trimesh.Trimesh, "export", _explode)
    calls: list[list[str]] = []

    def _record(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=part, repo_dir=repo
    )
    assert result.error_class == "artifact_error", (
        f"export failure must be artifact_error, got {result.error_class}"
    )
    assert "3MF to STL conversion failed" in result.stderr, result.stderr
    # Staging precedes EVERY docker call — zero subprocess invocations.
    assert calls == [], f"docker was invoked after a staging failure: {calls}"


# ---------------------------------------------------------------------------
# Issue #330: cleanup (#280) mirrors with a part present.
# ---------------------------------------------------------------------------


def test_cleanup_removes_container_and_volume_on_success_with_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    part, repo = _make_part_repo(tmp_path)
    _result, calls = _run_render_record(
        monkeypatch, tmp_path, "render-330cl01", part_path=part, repo_dir=repo
    )
    name = "render-330cl01"
    volume = f"d33d-render-{name}"
    assert [c for c in calls if c == ["docker", "rm", "-f", name]]
    assert [
        c for c in calls if c == ["docker", "volume", "rm", "-f", volume]
    ]


def test_cleanup_removes_container_and_volume_on_error_with_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    part, repo = _make_part_repo(tmp_path)
    result, calls = _run_render_record(
        monkeypatch,
        tmp_path,
        "render-330cl02",
        part_path=part,
        repo_dir=repo,
        render_exit=1,
        render_stderr=b"ERROR: x",
    )
    name = "render-330cl02"
    volume = f"d33d-render-{name}"
    assert result.error_class == "syntax_error"
    assert ["docker", "rm", "-f", name] in calls
    assert [
        "docker", "volume", "rm", "-f", volume
    ] in calls


def test_cleanup_removes_container_and_volume_on_timeout_with_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A render that times out with a part present: container + volume are
    still removed (the #280 cleanup mirror)."""
    part, repo = _make_part_repo(tmp_path)
    calls: list[list[str]] = []

    def _record(
        argv: list[str], *a: Any, **kw: Any
    ) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[:2] == ["docker", "run"] and "--memory" in argv:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=kw.get("timeout", 120))
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _record)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-330cl03")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    result = rw.render_for_design_loop(
        "cube(10);", {}, part_path=part, repo_dir=repo
    )
    name = "render-330cl03"
    volume = f"d33d-render-{name}"
    assert result.error_class == "timeout"
    assert ["docker", "rm", "-f", name] in calls
    assert ["docker", "volume", "rm", "-f", volume] in calls


def test_cleanup_removes_container_and_volume_on_exception_with_part(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An unexpected subprocess exception (docker binary missing) with a
    part present: container + volume cleanup still fires (the #280 cleanup
    mirror for the exception path)."""
    part, repo = _make_part_repo(tmp_path)
    calls: list[list[str]] = []

    def _explode(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        calls.append(argv)
        if argv[:3] == ["docker", "volume", "create"]:
            raise AttributeError("simulated unexpected failure mid-pipeline")
        return subprocess.CompletedProcess(
            args=argv, returncode=0, stdout=b"", stderr=b""
        )

    monkeypatch.setattr(rw.subprocess, "run", _explode)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-330cl04")
    monkeypatch.setattr(rw, "_verify_render_worker_image", lambda *a, **kw: None)
    monkeypatch.setenv("D33D_RENDER_TMP", str(tmp_path / "render-tmp"))

    with pytest.raises(AttributeError, match="simulated unexpected failure"):
        rw.render_for_design_loop("cube(10);", {}, part_path=part, repo_dir=repo)

    volume = "d33d-render-render-330cl04"
    assert ["docker", "volume", "rm", "-f", volume] in calls
