"""Unit tests for ``build_registry_glb``'s per-call-site classification
and partial-failure handling, with ``subprocess.run`` mocked — no Docker
required. The real Docker-driven end-to-end path is covered by the slow
layer (``tests/slow/test_module_registry_docker.py``).

Exercises every branch ``_classify_isolated_render`` and the populate/
harvest helpers can take: populate failure, non-zero openscad exit,
timeout, oom, empty STL, and the "one bad module never discards the
whole registry" partial-success guarantee.

Issue #280 workstream: also pins that the ``registry-put-*`` /
``registry-get-*`` helper containers are removed in a ``finally`` on
EVERY path — success, non-zero exit and timeout — not only on timeout
(previously the populate helper was cleaned only on timeout and the
harvest helper never).
"""

from __future__ import annotations

import subprocess
from unittest.mock import patch

import pytest
import trimesh

from d33d.module_registry import (
    MAX_CALL_SITES,
    TooManyCallSitesError,
    build_registry_glb,
)

TWO_MODULE_SCAD = """
module base() { cube([20,20,20]); }
module cap() { sphere(r=8); }
base();
cap();
"""


def _completed(returncode: int, stdout: bytes = b"", stderr: bytes = b""):
    return subprocess.CompletedProcess(
        args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr
    )


def _box_stl_bytes() -> bytes:
    mesh = trimesh.creation.box(extents=(1, 1, 1))
    return mesh.export(file_type="stl")


def _name_of(argv) -> str | None:
    """The ``-n``/``--name`` value of a ``docker run`` argv, or ``None``
    for non-run argv (``volume create``/``rm``, ``kill``, ``rm``)."""
    for i, tok in enumerate(argv[:-1]):
        if tok in ("--name", "-n"):
            return argv[i + 1]
    return None


def _is_populate(argv) -> bool:
    return any("cat > /work/" in a for a in argv)


def _is_harvest(argv) -> bool:
    return any("cat /work/" in a for a in argv) and not _is_populate(argv)


class _RecordingRun:
    """A fake ``subprocess.run`` recording every invocation in ``.calls``
    and every ``docker kill``/``docker rm`` invocation in ``.cleanup_calls``.

    Per-argv-shape behaviour is overridable with ``on_*`` hooks: each
    receives ``(argv, state)`` and returns either a ``CompletedProcess``
    or raises. Defaults make everything succeed.
    """

    def __init__(self, **overrides):
        self.calls: list = []
        self.cleanup_calls: list = []
        self.on_populate = overrides.get("populate")
        self.on_harvest = overrides.get("harvest")
        self.on_openscad = overrides.get("openscad")
        self.state: dict = {"n": 0}

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[:2] == ["docker", "kill"] or argv[:2] == ["docker", "rm"]:
            self.cleanup_calls.append(list(argv))
            return _completed(0)
        if "openscad" in argv:
            if self.on_openscad:
                return self.on_openscad(argv, self.state)
            return _completed(0)
        if _is_populate(argv):
            if self.on_populate:
                return self.on_populate(argv, self.state)
            return _completed(0)
        if _is_harvest(argv):
            if self.on_harvest:
                return self.on_harvest(argv, self.state)
            return _completed(0)
        return _completed(0)

    # --- helpers for assertions ---------------------------------------

    def started_names(self, predicate) -> list[str]:
        """Names of ``docker run`` calls matching ``predicate(argv)``."""
        names = []
        for argv in self.calls:
            if argv[:2] == ["docker", "run"] and predicate(argv):
                name = _name_of(argv)
                if name is not None:
                    names.append(name)
        return names

    def cleaned(self, name: str) -> bool:
        """True iff ``docker kill <name>`` AND ``docker rm <name>`` were
        both emitted — the full ``_cleanup_container`` contract."""
        return (
            ["docker", "kill", name] in self.cleanup_calls
            and ["docker", "rm", name] in self.cleanup_calls
        )


def _fake_run_for(stl_bytes: bytes):
    """The minimal fake used by the pre-existing (non-#280) tests below."""

    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if _is_populate(argv):
            return _completed(0)
        if _is_harvest(argv):
            return _completed(0, stdout=stl_bytes)
        return _completed(0)

    return _fake_run


def test_all_call_sites_succeed_assembles_full_registry() -> None:
    with patch("subprocess.run", side_effect=_fake_run_for(_box_stl_bytes())):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert result.failures == ()
    assert set(result.registry_names) == {"base", "cap"}
    assert result.glb_bytes is not None


def test_populate_failure_is_recorded_as_container_error_and_other_sites_still_run() -> None:
    stl_bytes = _box_stl_bytes()
    populate_calls = {"count": 0}

    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if _is_populate(argv):
            populate_calls["count"] += 1
            if populate_calls["count"] == 1:
                return _completed(1, stderr=b"disk full")
            return _completed(0)
        if _is_harvest(argv):
            return _completed(0, stdout=stl_bytes)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "container_error"
    assert result.failures[0].site.name == "base"
    # The SECOND call-site still succeeded — partial registry preserved.
    assert result.registry_names == ("cap",)
    assert result.glb_bytes is not None


def test_nonzero_openscad_exit_classifies_container_error() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(1, stderr=b"ERROR: parse error")
        if _is_populate(argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 2
    assert all(f.error_class == "container_error" for f in result.failures)
    assert result.glb_bytes is None


def test_populate_helper_timeout_classifies_timeout_cleans_up_container_and_preserves_partial_registry() -> None:
    """A hung populate-helper container (``registry-put-*``) must not
    abort the whole registry build, must not leak the container it
    started (started without ``--rm`` per ``build_docker_argv``), and
    must classify as ``timeout`` — not propagate ``subprocess.
    TimeoutExpired`` uncaught, which would discard every already-
    succeeded call-site and never reach the route's error classification
    at all."""
    stl_bytes = _box_stl_bytes()

    def _populate(argv, state):
        state["n"] += 1
        if state["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return _completed(0)

    fake = _RecordingRun(
        populate=_populate,
        harvest=lambda argv, state: _completed(0, stdout=stl_bytes),
    )

    with patch("subprocess.run", side_effect=fake):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "timeout"
    assert result.failures[0].site.name == "base"
    # The SECOND call-site still succeeded — partial registry preserved,
    # not discarded by the first call-site's uncaught TimeoutExpired.
    assert result.registry_names == ("cap",)
    assert result.glb_bytes is not None
    # The hung helper container was killed and removed, not leaked.
    put_names = fake.started_names(_is_populate)
    assert fake.cleaned(put_names[0])
    assert fake.cleaned(put_names[1])


def test_openscad_timeout_classifies_as_timeout() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(124)
        if _is_populate(argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "timeout" for f in result.failures)
    assert result.glb_bytes is None


def test_openscad_oom_classifies_as_oom() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(137)
        if _is_populate(argv):
            return _completed(0)
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "oom" for f in result.failures)
    assert result.glb_bytes is None


def test_empty_stl_harvest_classifies_empty_model() -> None:
    def _fake_run(argv, **kwargs):
        if "openscad" in argv:
            return _completed(0)
        if _is_populate(argv):
            return _completed(0)
        if _is_harvest(argv):
            return _completed(0, stdout=b"")  # empty harvest
        return _completed(0)

    with patch("subprocess.run", side_effect=_fake_run):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert all(f.error_class == "empty_model" for f in result.failures)
    assert result.glb_bytes is None


def test_source_with_no_call_sites_never_invokes_subprocess() -> None:
    with patch("subprocess.run") as mock_run:
        result = build_registry_glb("cube([1,1,1]);\n")
    mock_run.assert_not_called()
    assert result.glb_bytes is None
    assert result.registry_names == ()
    assert result.failures == ()


def test_too_many_call_sites_raises_before_any_docker_invocation() -> None:
    """A source enumerating more than MAX_CALL_SITES call-sites must be
    rejected BEFORE any Docker volume/container is created — the DoS
    vector this cap closes: a small, cheaply-parsed source built from a
    repeated call pattern must never enqueue unbounded sequential
    container work."""
    hostile_source = "module m(){cube([1,1,1]);}\n" + "m();\n" * (MAX_CALL_SITES + 1)

    with patch("subprocess.run") as mock_run, pytest.raises(TooManyCallSitesError) as exc_info:
        build_registry_glb(hostile_source)

    mock_run.assert_not_called()
    assert exc_info.value.count == MAX_CALL_SITES + 1


def test_exactly_max_call_sites_is_allowed() -> None:
    """The boundary itself (exactly MAX_CALL_SITES) must still be
    accepted — the cap rejects call-site counts strictly greater than
    MAX_CALL_SITES, not the limit value itself."""
    source = "module m(){cube([1,1,1]);}\n" + "m();\n" * MAX_CALL_SITES

    with patch("subprocess.run", side_effect=_fake_run_for(_box_stl_bytes())):
        result = build_registry_glb(source)

    assert len(result.registry_names) == MAX_CALL_SITES


def test_scene_export_exception_isolates_bad_mesh_and_preserves_rest_of_registry() -> None:
    """Regression: adversarial-review round 2. Every harvested STL is
    attacker-influenced (an OpenSCAD render driven by untrusted .scad
    source) and ``trimesh.load`` is guarded against it, but the final
    ``scene.export(file_type="glb")`` call — operating on the SAME
    attacker-influenced mesh data — previously had no equivalent
    protection: an export failure (e.g. a degenerate mesh trimesh.load
    accepted but export chokes on) propagated as an unhandled exception,
    discarding every OTHER call-site's already-succeeded geometry and
    surfacing as an unclassified 500 from the route. A single mesh that
    fails at export time must classify as artifact_error and the
    remaining geometries must still assemble into a GLB."""
    stl_bytes = _box_stl_bytes()

    def _fake_scene_export(self, *args, **kwargs):
        # Whole-scene export (both meshes present) always fails; the
        # per-mesh isolation probe fails ONLY for the scene containing
        # "base" so "base" is deterministically the poisoned mesh, and
        # the final retry (with only "cap" surviving) succeeds.
        if "base" in self.geometry:
            raise ValueError("corrupt geometry, cannot triangulate")
        return b"stub-glb-bytes"

    with patch("subprocess.run", side_effect=_fake_run_for(stl_bytes)), patch(
        "trimesh.Scene.export", new=_fake_scene_export
    ):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "artifact_error"
    assert result.failures[0].site.name == "base"
    # "base" was isolated as the poisoned mesh; "cap" still contributes
    # to the assembled (retried) registry.
    assert result.registry_names == ("cap",)
    assert result.glb_bytes == b"stub-glb-bytes"


def test_trimesh_load_exception_classifies_artifact_error_without_aborting_registry() -> None:
    """If trimesh.load ever raises on a harvested STL (e.g. a future
    trimesh version or an unusual OpenSCAD STL variant), the call-site
    must classify as artifact_error and let every OTHER call-site's
    already-succeeded render still stand — a single bad module must
    never abort the whole registry build with an unhandled exception."""
    stl_bytes = _box_stl_bytes()

    with patch("subprocess.run", side_effect=_fake_run_for(stl_bytes)), patch(
        "trimesh.load", side_effect=[ValueError("corrupt STL"), trimesh.creation.box()]
    ):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "artifact_error"
    assert result.failures[0].site.name == "base"
    assert result.registry_names == ("cap",)
    assert result.glb_bytes is not None


# ---------------------------------------------------------------------------
# Issue #280 — helper containers removed on EVERY path, not just timeout
# ---------------------------------------------------------------------------


def test_populate_helper_removed_on_success_path() -> None:
    """A SUCCESSFUL ``registry-put-*`` helper (exit 0) must still be
    removed — the helper is started without ``--rm`` (``build_docker_argv``),
    so without the fix every successful populate leaks an exited
    ``registry-put-*`` container and the live VM accumulates them
    alongside the render container leak."""
    stl_bytes = _box_stl_bytes()
    fake = _RecordingRun(
        harvest=lambda argv, state: _completed(0, stdout=stl_bytes)
    )

    with patch("subprocess.run", side_effect=fake):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert result.failures == ()
    put_names = fake.started_names(_is_populate)
    assert len(put_names) == 2  # two call-sites -> two populate helpers
    for name in put_names:
        assert name.startswith("registry-put-")
        assert fake.cleaned(name), f"registry-put container {name} was not removed"


def test_harvest_helper_removed_on_success_path() -> None:
    """A SUCCESSFUL ``registry-get-*`` helper (exit 0, stdout = the STL)
    must still be removed — before the fix the harvest helper was never
    removed on the success path at all, so every isolated render leaked
    an exited ``registry-get-*`` container."""
    stl_bytes = _box_stl_bytes()
    fake = _RecordingRun(
        harvest=lambda argv, state: _completed(0, stdout=stl_bytes)
    )

    with patch("subprocess.run", side_effect=fake):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert result.failures == ()
    get_names = fake.started_names(_is_harvest)
    assert len(get_names) == 2  # two call-sites -> two harvest helpers
    for name in get_names:
        assert name.startswith("registry-get-")
        assert fake.cleaned(name), f"registry-get container {name} was not removed"


def test_populate_helper_removed_on_nonzero_exit_path() -> None:
    """A FAILED ``registry-put-*`` helper (non-zero exit -> ``RuntimeError``
    -> ``container_error``) must be removed too — the success-path fix
    alone would still leak it here, because the non-zero-exit branch is a
    distinct path from timeout."""
    stl_bytes = _box_stl_bytes()

    def _populate(argv, state):
        state["n"] += 1
        if state["n"] == 1:
            return _completed(1, stderr=b"disk full")
        return _completed(0)

    fake = _RecordingRun(
        populate=_populate,
        harvest=lambda argv, state: _completed(0, stdout=stl_bytes),
    )

    with patch("subprocess.run", side_effect=fake):
        result = build_registry_glb(TWO_MODULE_SCAD)

    assert len(result.failures) == 1
    assert result.failures[0].error_class == "container_error"
    assert result.registry_names == ("cap",)
    # The FAILED populate helper (first call-site) was still removed.
    put_names = fake.started_names(_is_populate)
    assert len(put_names) == 2
    assert fake.cleaned(put_names[0])
    assert fake.cleaned(put_names[1])


def test_harvest_helper_removed_on_timeout_path() -> None:
    """A TIMED-OUT ``registry-get-*`` helper (harvest hangs -> returns
    ``None`` -> ``empty_model``) must be removed — before the fix the
    timeout branch simply returned ``None`` and leaked the hung helper
    container outright."""
    stl_bytes = _box_stl_bytes()

    def _harvest(argv, state):
        state["n"] += 1
        if state["n"] == 1:
            raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
        return _completed(0, stdout=stl_bytes)

    fake = _RecordingRun(harvest=_harvest)

    with patch("subprocess.run", side_effect=fake):
        result = build_registry_glb(TWO_MODULE_SCAD)

    # First call-site's harvest hung -> empty_model for that site only.
    assert len(result.failures) == 1
    assert result.failures[0].error_class == "empty_model"
    assert result.registry_names == ("cap",)

    get_names = fake.started_names(_is_harvest)
    assert len(get_names) == 2
    # The TIMED-OUT harvest helper (first) was killed and removed, not leaked.
    assert fake.cleaned(get_names[0])
    assert fake.cleaned(get_names[1])
