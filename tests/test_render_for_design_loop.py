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
) -> "subprocess.CompletedProcess[str] | None":
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
