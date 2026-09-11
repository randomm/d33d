"""Render worker: VIEWS contract, docker argv builder, classification, result.json.

Host-side core module for ticket #2. Nothing in this module touches
Docker or the filesystem — the ``render()`` caller API (``d33d/render.py``)
and the container entrypoint read this module's constants as their single
source of truth.

Empirical CLI verification (run 2026-09-11 inside the pinned image
``docker.io/openscad/openscad:trixie``, OpenSCAD 2026.01.19):

- ``--camera =tx,ty,tz,rx,ry,rz,dist`` — the 7-element gimbal form is
  accepted; rotations are applied about the origin **after** ``--autocenter``
  has shifted the origin to the bounding-box centre, so the camera tuples
  below are fixed constants independent of the model.
- ``--autocenter``, ``--projection o``, ``--imgsize 800,800``, ``--render``
  and ``--colorscheme "Tomorrow Night"`` are all present in the pinned
  build (the spec forbade assuming ``--autocenter``/``--colorscheme`` —
  both were confirmed by ``openscad --help``; the flag table is recorded in
  ``docs/bosl2-pinning.md``).
- The 6 camera tuples were verified empirically with a three-slab test
  model (see ``docs/bosl2-pinning.md``); they are pinned together with the
  ``VIEWS`` constant below. If a future OpenSCAD build changes the camera
  rotation semantics, ``VIEWS`` and its test must move in the same commit.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

ErrorClass = Literal[
    "ok",
    "syntax_error",
    "empty_model",
    "artifact_error",
    "timeout",
    "oom",
    "container_error",
]

#: Closed enum of every class a run can land in. Classification is
#: first-match-wins; the table is TOTAL — every combination of exit code
#: and artifact presence lands in exactly one class.
ERROR_CLASSES: frozenset[ErrorClass] = frozenset(
    {
        "ok",
        "syntax_error",
        "empty_model",
        "artifact_error",
        "timeout",
        "oom",
        "container_error",
    }
)

#: The six-view contract: ordered ``(filename, camera_tuple)`` pairs.
#: ``camera_tuple`` is the 7-element gimbal ``[tx, ty, tz, rx, ry, rz,
#: dist]`` — translate, rotate about the origin (post-``--autocenter``),
#: then distance, all in mm, orthographic 800x800. Single source of truth
#: for the entrypoint, the caller and the tests. Cameras are fixed
#: constants independent of the model — centre the model, don't fit the
#: camera, so renders are stable across runs.
VIEWS: list[tuple[str, tuple[float, float, float, float, float, float, float]]] = [
    ("view_00_front.png", (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 40.0)),
    ("view_01_back.png", (0.0, 0.0, 0.0, 0.0, 180.0, 0.0, 40.0)),
    ("view_02_left.png", (0.0, 0.0, 0.0, 0.0, 90.0, 0.0, 40.0)),
    ("view_03_right.png", (0.0, 0.0, 0.0, 0.0, -90.0, 0.0, 40.0)),
    ("view_04_top.png", (0.0, 0.0, 0.0, 90.0, 0.0, 0.0, 40.0)),
    ("view_05_iso.png", (0.0, 0.0, 0.0, 0.0, 45.0, 45.0, 55.0)),
]

#: Every ``openscad`` invocation carries these flags (verified present in
#: the pinned image — see the module docstring). The entrypoint applies
#: them to all eight invocations.
OPENSCAD_COMMON_FLAGS: tuple[str, ...] = (
    "--autocenter",
    "--projection",
    "o",
    "--imgsize",
    "800,800",
)

#: ``--render`` (full geometry evaluation, not preview) plus the dark
#: ``--colorscheme`` used for the six PNG renders so the model geometry
#: stands out against the background.
OPENSCAD_RENDER_FLAGS: tuple[str, ...] = (
    "--render",
    "--colorscheme",
    "Tomorrow Night",
)

#: Render size, in pixels — 800x800 orthographic on every view.
RENDER_SIZE: tuple[int, int] = (800, 800)

#: OpenSCAD diagnostic marker in stderr — the ``syntax_error`` gate.
OPENSCAD_DIAGNOSTIC_RE = re.compile(r"ERROR:")

#: stderr is truncated by the caller to the first 256 KiB — the binary
#: value 262144 bytes, NOT 256000. Decoded UTF-8 ``errors="replace"``.
STDERR_MAX_BYTES = 262144

#: Defaults for the container invocation, all overridable via params.json.
DEFAULT_MEMORY_LIMIT = "2g"
DEFAULT_CPU_LIMIT = "2"
DEFAULT_PID_LIMIT = 512
DEFAULT_TIMEOUT_S = 120

#: ``render-<8 hex chars from uuid4>`` — pinned as the testable contract;
#: container and volume share the name.
NAME_PATTERN_RE = re.compile(r"^render-[0-9a-f]{8}$")

#: The eight on-volume artifacts a fully successful run produces.
ARTIFACT_STL = "model.stl"
ARTIFACT_CSG = "model.csg"


def new_render_name() -> str:
    """Fresh ``render-<8 hex chars from uuid4>`` for one render run."""
    return f"render-{uuid.uuid4().hex[:8]}"


def validate_render_name(name: str) -> bool:
    """True iff ``name`` matches ``render-<8 hex chars>``."""
    return bool(NAME_PATTERN_RE.match(name))


@dataclass(frozen=True)
class RenderParams:
    """The ``params.json`` payload, with the four overridable defaults.

    ``defines`` is a flat string map passed as ``-Dname=value``; ``{}``
    means no defines.
    """

    defines: dict[str, str] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT_S
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    cpus: str = DEFAULT_CPU_LIMIT
    pids_limit: int = DEFAULT_PID_LIMIT

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RenderParams:
        """Parse a params dict; unknown keys are ignored, missing keys
        fall back to the module defaults."""
        return cls(
            defines={str(k): str(v) for k, v in (data.get("defines") or {}).items()},
            timeout_s=int(data.get("timeout_s", DEFAULT_TIMEOUT_S)),
            memory_limit=str(data.get("memory_limit", DEFAULT_MEMORY_LIMIT)),
            cpus=str(data.get("cpus", DEFAULT_CPU_LIMIT)),
            pids_limit=int(data.get("pids_limit", DEFAULT_PID_LIMIT)),
        )


def build_docker_argv(
    image: str,
    name: str,
    workdir_volume: str | None = None,
    params: RenderParams | dict[str, Any] | None = None,
) -> list[str]:
    """Build the ``docker run`` argv for one render.

    Contains every required containment flag, exactly two mounts (the rw
    ``/work`` named volume and the ``/tmp`` tmpfs) and nothing else, and
    no reference to ``/var/run/docker.sock``. Never contains
    ``--privileged`` or ``--rm`` — the caller removes the container
    explicitly in a finally-path *after* harvesting, because OOM detection
    needs ``docker inspect`` on the exited container.
    """
    if isinstance(params, dict):
        params = RenderParams.from_dict(params)
    elif params is None:
        params = RenderParams()
    volume = workdir_volume or name

    argv: list[str] = [
        "docker",
        "run",
        "--name",
        name,
        "--network",
        "none",
        "--cap-drop",
        "ALL",
        "--read-only",
        "--tmpfs",
        "/tmp",
        "--security-opt",
        "no-new-privileges",
        "--memory",
        params.memory_limit,
        "--cpus",
        params.cpus,
        "--pids-limit",
        str(params.pids_limit),
        "--volume",
        f"{volume}:/work",
        image,
    ]
    for key, value in params.defines.items():
        argv.extend(["-D", f"{key}={value}"])
    return argv


def truncate_stderr(stderr: bytes | str) -> str:
    """Truncate to the first 262144 bytes, decode UTF-8 ``errors=replace``.

    Exactly 262144 bytes passes through untruncated; 262145 bytes
    truncates to exactly 262144. The result is always valid UTF-8.
    """
    if isinstance(stderr, str):
        raw = stderr.encode("utf-8")
    else:
        raw = stderr
    return raw[:STDERR_MAX_BYTES].decode("utf-8", errors="replace")


def _png_valid_nonblank(png: str) -> bool:
    """True iff ``png`` is a path to a structurally valid PNG with more
    than one distinct colour. A structurally valid all-black PNG (camera
    missed the model) must fail the ``ok`` check."""
    try:
        from PIL import Image  # host-side only; never in the image

        with Image.open(png) as img:
            if img.format != "PNG":
                return False
            return len(img.getcolors(maxcolors=65536) or []) > 1
    except (OSError, ImportError, ValueError):
        return False


def _stl_valid(stl: Any, vertex_count: int, watertight: bool, volume: float) -> bool:
    """STL artifact present and non-degenerate: vertex count > 0,
    watertight, volume > 0. The host-side trimesh check — a ``.scad`` can
    compile cleanly to empty geometry, so exit code and artifact presence
    are not enough."""
    if not isinstance(stl, str) or not stl:
        return False
    if vertex_count <= 0:
        return False
    if not watertight:
        return False
    return volume > 0


def _csg_and_views_valid(csg: Any, views: Any) -> bool:
    """CSG present and the six PNGs present, valid and non-blank."""
    if not isinstance(csg, str) or not csg:
        return False
    if not isinstance(views, (list, tuple)) or len(views) != 6:
        return False
    for v in views:
        if not isinstance(v, str) or not v:
            return False
    return True


def classify(
    *,
    exit_code: int,
    stl_path: Any = None,
    csg_path: Any = None,
    views: Any = None,
    stderr: str = "",
    oomkilled: bool = False,
    timed_out: bool = False,
    vertex_count: int = 0,
    watertight: bool = False,
    volume: float = 0.0,
) -> ErrorClass:
    """Classify a render run. First match wins; the table is TOTAL.

    1. ``timeout`` — caller wall-clock fired; ``exit_code`` recorded 124
    2. ``oom`` — ``exit_code`` 137 or ``docker inspect`` reports ``OOMKilled``
    3. ``syntax_error`` — non-zero exit, no STL, stderr matches ``ERROR:``
    4. ``container_error`` — any other non-zero exit, no STL
    5. ``artifact_error`` — STL present but the CSG or any of the six PNGs
       is missing or invalid, regardless of exit code
    6. ``empty_model`` — all eight artifacts present but the STL has
       vertex count 0 / is not watertight / volume <= 0
    7. ``ok`` — all eight artifacts present and valid, exit 0, STL
       non-degenerate

    ``views`` must be a 6-element sequence of non-empty filename strings
    for class 5/6/7 to be reachable; a wrong length or a missing entry
    counts as the PNG missing.
    """
    if timed_out:
        return "timeout"
    if exit_code == 137 or oomkilled:
        return "oom"
    stl_present = isinstance(stl_path, str) and bool(stl_path)
    if not stl_present:
        if exit_code != 0 and OPENSCAD_DIAGNOSTIC_RE.search(stderr or ""):
            return "syntax_error"
        return "container_error"
    if not _csg_and_views_valid(csg_path, views):
        return "artifact_error"
    if not _stl_valid(stl_path, vertex_count, watertight, volume):
        return "empty_model"
    if exit_code == 0:
        return "ok"
    return "container_error"


def derive_ok(
    *,
    exit_code: int,
    stl_path: Any,
    csg_path: Any,
    views: Any,
    vertex_count: int,
    watertight: bool,
    volume: float,
) -> bool:
    """True iff ``exit 0`` AND all eight artifacts exist AND the six PNGs
    are valid/non-blank AND the STL loads in trimesh with vertex count >
    0. That last clause exists because a ``.scad`` can compile cleanly to
    empty geometry."""
    if exit_code != 0:
        return False
    if not (isinstance(stl_path, str) and stl_path):
        return False
    if not _csg_and_views_valid(csg_path, views):
        return False
    return _stl_valid(stl_path, vertex_count, watertight, volume)


@dataclass(frozen=True)
class RenderResult:
    """The ``result.json`` payload, as written by the Python caller."""

    ok: bool
    exit_code: int
    duration_ms: int
    error_class: ErrorClass
    stderr: str
    stl: str | None
    csg: str | None
    views: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "exit_code": self.exit_code,
            "duration_ms": self.duration_ms,
            "error_class": self.error_class,
            "stderr": self.stderr,
            "artifacts": {
                "stl": self.stl,
                "csg": self.csg,
                "views": list(self.views),
            },
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RenderResult:
        """Parse a result.json payload. ``artifacts.views`` holds the six
        bare filenames, never on-volume paths — the work volume is
        internal to the caller and its path must never leak."""
        artifacts = data.get("artifacts") or {}
        views_raw = artifacts.get("views") or []
        return cls(
            ok=bool(data["ok"]),
            exit_code=int(data["exit_code"]),
            duration_ms=int(data["duration_ms"]),
            error_class=data["error_class"],
            stderr=str(data.get("stderr", "")),
            stl=artifacts.get("stl"),
            csg=artifacts.get("csg"),
            views=tuple(views_raw),
        )


def result_to_json(result: RenderResult) -> str:
    """Serialise a ``RenderResult`` to the exact ``result.json`` shape
    the caller writes to the work volume."""
    return json.dumps(result.to_dict(), indent=2, sort_keys=False)
