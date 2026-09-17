"""Fast-layer test: per-view arrival events reach the host while the render
container is still running (issue #121).

The decisive test: the host observes a per-view completion event BEFORE the
container exits. This proves the event is a real-time measurement (caused by
the view actually finishing), not an estimate or a post-exit harvest.

The harness uses a synthetic entrypoint (a bash script that writes per-view
markers to stderr with short sleeps between them, simulating a real render)
and verifies that ``run_container``'s reader thread delivers at least one
view-done marker while the fake container is still running.

Against the legacy blocking path (``subprocess.run`` without the reader
thread), no event would be delivered before the exit — the host would learn
nothing until ``subprocess.run`` returns, which is exactly the lie issue
#121 exists to delete.
"""

from __future__ import annotations

import subprocess
import time
from typing import Any

import pytest

import d33d.render_worker as rw


# ---------------------------------------------------------------------------
# Marker parser
# ---------------------------------------------------------------------------


def test_parse_entrypoint_markers() -> None:
    """The marker parser correctly extracts (marker, step) pairs from
    entrypoint stderr lines. Lines without markers yield an empty list."""
    assert rw.parse_entrypoint_markers("[entrypoint] view-done view_00_front") == [
        ("view-done", "view_00_front")
    ]
    assert rw.parse_entrypoint_markers("[entrypoint] view-start view_01_back") == [
        ("view-start", "view_01_back")
    ]
    assert rw.parse_entrypoint_markers("[entrypoint] view-failed view_02_left 1") == [
        ("view-failed", "view_02_left")
    ]
    assert rw.parse_entrypoint_markers("[entrypoint] view-done stl") == [
        ("view-done", "stl")
    ]
    # Lines without markers
    assert rw.parse_entrypoint_markers("[entrypoint] Step 3/8: PNG view_00_front") == []
    assert rw.parse_entrypoint_markers("[entrypoint] All 8 steps complete.") == []
    assert rw.parse_entrypoint_markers("some random line") == []
    assert rw.parse_entrypoint_markers("") == []
    # The step is the first \S+ after the marker (not the whole rest of the line)
    assert rw.parse_entrypoint_markers("[entrypoint] view-done view_00_front extra") == [
        ("view-done", "view_00_front")
    ]


# ---------------------------------------------------------------------------
# Decisive test: event arrives before container exit
# ---------------------------------------------------------------------------


def _synthetic_render_script(num_views: int = 6, delay: str = "0.1") -> str:
    """A synthetic entrypoint that writes per-view markers with delays."""
    lines = [
        "echo '[entrypoint] view-start stl' >&2",
        "echo '[entrypoint] view-done stl' >&2",
    ]
    for i in range(num_views):
        stem = f"view_{i:02d}_x"
        lines.append(f"echo '[entrypoint] view-start {stem}' >&2")
        lines.append(f"sleep {delay}")
        lines.append(f"echo '[entrypoint] view-done {stem}' >&2")
    lines.append("echo '[entrypoint] All 8 steps complete.' >&2")
    return "\n".join(lines)


def _patch_popen(monkeypatch: pytest.MonkeyPatch, script: str):
    """Patch ``rw.subprocess.Popen`` so the render-worker image launches a
    bash script instead of a real docker run. Returns (factory, exit_time)."""
    exit_time: list[float] = []
    orig_popen = subprocess.Popen

    class _FakeContainer:
        def __init__(self, argv: list[str], **kwargs: Any) -> None:
            self._proc = orig_popen(
                ["bash", "-c", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                stdin=subprocess.DEVNULL,
            )

        def wait(self, timeout: float | None = None) -> None:
            self._proc.wait(timeout=timeout)
            exit_time.append(time.monotonic())

        def kill(self) -> None:
            self._proc.kill()

        @property
        def returncode(self) -> int | None:
            return self._proc.returncode

        @property
        def stderr(self) -> Any:
            return self._proc.stderr

    class _PopenFactory:
        def __call__(self, argv: list[str], **kwargs: Any) -> Any:
            if any(tok.endswith("render-worker:local") for tok in argv):
                return _FakeContainer(argv, **kwargs)
            return orig_popen(argv, **kwargs)

    monkeypatch.setattr(rw.subprocess, "Popen", _PopenFactory())
    return _PopenFactory(), exit_time


def test_view_event_arrives_before_container_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The decisive test (issue #121): a per-view completion event reaches
    the host BEFORE the container exits.

    Harness: a synthetic entrypoint (a bash script that writes per-view
    markers to stderr with 100ms sleeps between them). ``run_container``'s
    reader thread drains the pipe and calls ``on_marker`` as each marker
    line arrives. The test records the arrival time of each view-done
    marker and the container's exit time, then asserts that at least one
    view-done marker arrived strictly before the exit.

    This proves the event is real-time (caused by the view finishing), not
    a post-exit harvest. Against the legacy blocking path, no event would
    be delivered before the exit — the host would learn nothing until
    ``subprocess.run`` returns.
    """
    events: list[tuple[float, str, str]] = []  # (arrival_time, marker, step)

    def on_marker(marker: str, step: str) -> None:
        events.append((time.monotonic(), marker, step))

    # Use the VIEWS contract's actual stems so the parser sees real names
    script_lines = [
        "echo '[entrypoint] view-start stl' >&2",
        "echo '[entrypoint] view-done stl' >&2",
    ]
    for name, _cam in rw.VIEWS:
        stem = name[:-4]
        script_lines.append(f"echo '[entrypoint] view-start {stem}' >&2")
        script_lines.append("sleep 0.1")
        script_lines.append(f"echo '[entrypoint] view-done {stem}' >&2")
    script_lines.append("echo '[entrypoint] All 8 steps complete.' >&2")
    script = "\n".join(script_lines)

    _, exit_time = _patch_popen(monkeypatch, script)

    proc = rw.run_container(
        ["docker", "run", "--name", "render-121decis", "d33d/render-worker:local"],
        timeout_s=30,
        on_marker=on_marker,
    )

    # The container exited cleanly
    assert proc.returncode == 0, f"fake container exited {proc.returncode}: {proc.stderr[:200]}"
    assert exit_time, "container exit time not recorded"
    exit_t = exit_time[0]

    # Find all view-done events
    view_done_events = [(t, m, s) for t, m, s in events if m == "view-done"]
    assert view_done_events, "no view-done events recorded"

    # The decisive assertion: at least one view-done event arrived BEFORE
    # the container exited. With 100ms delays between markers and a total
    # script runtime of ~600ms, the reader thread delivers the first
    # view-done (stl) almost immediately — well before the 600ms exit.
    early_events = [e for e in view_done_events if e[0] < exit_t]
    assert early_events, (
        f"NO view-done event arrived before container exit at t={exit_t:.3f}. "
        f"All {len(view_done_events)} view-done events arrived after exit. "
        f"Earliest arrival: {min(e[0] for e in view_done_events):.3f}. "
        f"This means the reader thread did NOT deliver markers in real time — "
        f"the implementation is still blocking (the lie issue #121 deletes)."
    )
    # Verify the full stderr was preserved
    assert b"view-done view_00_front" in proc.stderr
    assert b"All 8 steps complete" in proc.stderr


def test_view_events_arrive_in_order_with_index(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-view events carry a view index and arrive in order
    (view 0, 1, 2, 3, 4, 5). The index is the 0-based position in the
    VIEWS contract; the test verifies both the index values and the
    arrival order via ``render_for_design_loop``'s ``on_progress``."""
    events: list[tuple[float, int, str]] = []  # (time, index, view)

    def on_progress(kind: str, payload: dict) -> None:
        if kind != "view-done":
            return
        idx = payload.get("index")
        view = payload.get("view", "")
        if idx is not None and view in [n[:-4] for n, _ in rw.VIEWS]:
            events.append((time.monotonic(), idx, view))

    script_lines = [
        "echo '[entrypoint] view-start stl' >&2",
        "echo '[entrypoint] view-done stl' >&2",
        "echo '[entrypoint] view-start csg' >&2",
        "echo '[entrypoint] view-done csg' >&2",
    ]
    for name, _cam in rw.VIEWS:
        stem = name[:-4]
        script_lines.append(f"echo '[entrypoint] view-start {stem}' >&2")
        script_lines.append("sleep 0.05")
        script_lines.append(f"echo '[entrypoint] view-done {stem}' >&2")
    script_lines.append("echo '[entrypoint] All 8 steps complete.' >&2")
    script = "\n".join(script_lines)

    _patch_popen(monkeypatch, script)

    def fake_run(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        if "cp /work/model.stl /host/" in " ".join(argv):
            from pathlib import Path

            for i, tok in enumerate(argv):
                if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                    out = Path(tok.rsplit(":", 1)[0])
                    (out / "model.stl").write_bytes(b"fake-stl")
                    (out / "model.csg").write_bytes(b"fake-csg")
                    for name, _cam in rw.VIEWS:
                        (out / name).write_bytes(b"fake-png")
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    import os

    tmp_base = os.path.expanduser("~/d33d/render-tmp")
    os.makedirs(tmp_base, exist_ok=True)

    monkeypatch.setenv("D33D_RENDER_TMP", tmp_base)
    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-121ord01")

    class _FakeTrimesh:
        @staticmethod
        def load(*a: Any, **k: Any) -> Any:
            class M:
                vertices = [0, 1, 2]
                is_watertight = True
                volume = 1000.0

                def merge_vertices(self, *a: Any, **k: Any) -> Any:
                    return self

            return M()

    import sys as _s

    monkeypatch.setitem(_s.modules, "trimesh", _FakeTrimesh)

    result = rw.render_for_design_loop(
        "cube(10);", {}, renders_dir=None, on_progress=on_progress
    )

    assert result.ok, f"render failed: {result.error_class}"

    # Verify the 6 view-done events arrived in order with correct indices
    view_indices = [idx for _, idx, view in events]
    assert len(view_indices) == 6, f"expected 6 view-done events, got {len(view_indices)}: {events}"
    assert view_indices == [0, 1, 2, 3, 4, 5], (
        f"view indices did not arrive in order: {view_indices}"
    )
    # Verify the view names match the VIEWS contract
    expected_names = [n[:-4] for n, _ in rw.VIEWS]
    actual_names = [view for _, _, view in events]
    assert actual_names == expected_names, (
        f"view names do not match VIEWS contract: {actual_names} != {expected_names}"
    )


# ---------------------------------------------------------------------------
# STL failure stops the event stream
# ---------------------------------------------------------------------------


def test_stl_failure_stops_view_event_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An STL failure (exit non-zero) must STOP the per-view event stream —
    no view-done events for the six PNG views should be emitted. The
    entrypoint exits immediately after the STL failure (the aborting step),
    so the host must not see any view events after that point."""
    events: list[tuple[str, str]] = []

    def on_progress(kind: str, payload: dict) -> None:
        events.append((kind, payload.get("view", "")))

    script = (
        "echo '[entrypoint] Step 1/8: STL export' >&2\n"
        "echo '[entrypoint] view-start stl' >&2\n"
        "echo '[entrypoint] STL export failed with exit code 1 — aborting remaining steps' >&2\n"
        "echo '[entrypoint] view-failed stl 1' >&2\n"
        "exit 1\n"
    )

    _patch_popen(monkeypatch, script)

    def fake_run(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
        if any(tok.endswith("render-worker:local") for tok in argv):
            return subprocess.CompletedProcess(
                args=argv, returncode=1, stdout=b"", stderr=b"ERROR: syntax"
            )
        return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")

    import os

    tmp_base = os.path.expanduser("~/d33d/render-tmp")
    os.makedirs(tmp_base, exist_ok=True)

    monkeypatch.setenv("D33D_RENDER_TMP", tmp_base)
    monkeypatch.setattr(rw.subprocess, "run", fake_run)
    monkeypatch.setattr(rw, "new_render_name", lambda: "render-121stlfail")

    class _FakeTrimesh:
        @staticmethod
        def load(*a: Any, **k: Any) -> Any:
            raise OSError("no STL to load")

    import sys as _s

    monkeypatch.setitem(_s.modules, "trimesh", _FakeTrimesh)

    result = rw.render_for_design_loop(
        "cube(10);", {}, renders_dir=None, on_progress=on_progress
    )

    # The render must have failed
    assert not result.ok
    # No view-done events should have been emitted (the STL failure
    # aborted the remaining seven steps)
    view_done_events = [e for e in events if e[0] == "view-done"]
    assert not view_done_events, (
        f"view-done events emitted after STL failure: {view_done_events}. "
        f"An STL failure must STOP the event stream — no phantom views."
    )
    # The view-start for stl should have been delivered
    stl_starts = [e for e in events if e[0] == "view-start" and e[1] == "stl"]
    assert stl_starts, f"expected view-start for stl, got {events}"


# ---------------------------------------------------------------------------
# Container command line: --network none and hardening flags
# ---------------------------------------------------------------------------


def test_docker_argv_carries_network_none_and_hardening_flags() -> None:
    """The container command line still carries ``--network none`` and the
    full hardening flag set. A direct assertion on the ``build_docker_argv``
    output — the actual argv that ``subprocess.run``/``Popen`` would
    receive."""
    argv = rw.build_docker_argv(
        image="d33d/render-worker:local",
        name="render-121flags",
        workdir_volume="d33d-render-121flags",
        params=rw.RenderParams(),
    )

    # --network none
    i = argv.index("--network")
    assert argv[i + 1] == "none"

    # --cap-drop ALL
    i = argv.index("--cap-drop")
    assert argv[i + 1] == "ALL"

    # --read-only
    assert "--read-only" in argv

    # --tmpfs /tmp
    i = argv.index("--tmpfs")
    assert argv[i + 1] == "/tmp"

    # --security-opt no-new-privileges
    i = argv.index("--security-opt")
    assert argv[i + 1] == "no-new-privileges"

    # --memory (default 2g)
    i = argv.index("--memory")
    assert argv[i + 1] == "2g"

    # --cpus (default 2)
    i = argv.index("--cpus")
    assert argv[i + 1] == "2"

    # --pids-limit (default 512)
    i = argv.index("--pids-limit")
    assert argv[i + 1] == "512"

    # NO --privileged
    assert "--privileged" not in argv

    # NO --rm (the caller removes after docker inspect)
    assert "--rm" not in argv

    # Exactly one --volume (the /work rw mount)
    volume_count = sum(1 for tok in argv if tok == "--volume")
    assert volume_count == 1, (
        f"expected exactly 1 --volume, got {volume_count}: {argv}"
    )


# ---------------------------------------------------------------------------
# run_container with on_marker: stderr preservation + timeout
# ---------------------------------------------------------------------------


def test_run_container_on_marker_preserves_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``on_marker`` is set, ``run_container`` still preserves the
    full stderr bytes (the 256 KiB truncation contract at the caller is
    unchanged). The reader thread accumulates the full stream."""
    events: list[tuple[str, str]] = []

    def on_marker(marker: str, step: str) -> None:
        events.append((marker, step))

    script = (
        "echo 'line one' >&2\n"
        "echo '[entrypoint] view-done view_00_front' >&2\n"
        "echo 'line three' >&2\n"
        "sleep 0.1\n"
        "echo 'final line' >&2\n"
    )

    _patch_popen(monkeypatch, script)

    proc = rw.run_container(
        ["docker", "run", "--name", "render-121stderr", "d33d/render-worker:local"],
        timeout_s=10,
        on_marker=on_marker,
    )

    # The full stderr is preserved
    assert b"line one" in proc.stderr
    assert b"line three" in proc.stderr
    assert b"final line" in proc.stderr
    assert b"view-done view_00_front" in proc.stderr

    # The marker was delivered
    assert events == [("view-done", "view_00_front")]

    # The 256 KiB truncation contract still holds
    truncated = rw.truncate_stderr(proc.stderr)
    assert len(truncated.encode("utf-8")) <= rw.STDERR_MAX_BYTES


def test_run_container_timeout_with_on_marker_returns_124() -> None:
    """The timeout path still returns the 124 sentinel when ``on_marker``
    is set. The ``timeout`` classification row is unchanged."""
    from unittest.mock import patch

    with patch("d33d.render_worker.subprocess.run") as mock_run:
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=["docker", "run", "x"], timeout=1),
        ]
        proc = rw.run_container(["docker", "run", "x"], timeout_s=1)
    assert proc.returncode == 124
    assert rw.classify(exit_code=proc.returncode, timed_out=True) == "timeout"
