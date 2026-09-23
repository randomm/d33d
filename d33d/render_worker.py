"""Render worker: VIEWS contract, docker argv builder, classification, result.json.

Host-side core module for ticket #2. Nothing in this module touches
Docker or the filesystem except the pre-render image staleness guard
(:func:`build_hash` / :func:`_verify_render_worker_image`, issue #236)
which reads two repo files and one `docker image inspect` before any
render; the ``render()`` caller API (``d33d/render.py``) and the
container entrypoint read this module's constants as their single
source of truth.

Empirical CLI verification (run 2026-09-11 inside the pinned image
``docker.io/openscad/openscad:trixie``, OpenSCAD 2026.01.19):

- ``--camera =tx,ty,tz,rx,ry,rz,dist`` — the 7-element gimbal form is
  accepted; rotations are applied about the origin **after** ``--autocenter``
  has shifted the origin to the bounding-box centre.
- ``--autocenter``, ``--projection o``, ``--imgsize 800,800``, ``--render``
  and ``--colorscheme "Tomorrow Night"`` are all present in the pinned
  build (the spec forbade assuming ``--autocenter``/``--colorscheme`` —
  both were confirmed by ``openscad --help``; the flag table is recorded in
  ``docs/bosl2-pinning.md``).
- The camera rotation semantics (which view sees which face) were verified
  empirically with a three-slab test model (see ``docs/bosl2-pinning.md``);
  they are pinned together with the ``VIEWS`` constant below. The camera
  *distance*, however, is **not** a fixed constant — it is fitted to the
  model's bounding box via :func:`cam_dist` (issue #111) so that a 60 mm
  part does not overflow its 800×800 frame. If a future OpenSCAD build
  changes the camera rotation semantics or the orthographic projection
  scale, ``VIEWS`` and its test must move in the same commit.
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from hashlib import sha256
from pathlib import Path
from typing import Any, Literal

logger = logging.getLogger(__name__)

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
#: for the entrypoint, the caller and the tests.
#
#: The first three elements (translate, ``0.0``) and the seventh element
#: (``dist``, ``0.0``) are **placeholders** — the entrypoint substitutes
#: both at render time: the translate with the model's bounding-box
#: centre in world coordinates (issue #223: with translate hard-coded at
#: ``(0,0,0)`` the camera looked at the world origin while
#: ``--autocenter`` recentred the scene at the bbox centre, so the two
#: shifts did not cancel and an asymmetric-bbox model rendered displaced
#: from the frame centre by exactly its bbox centre, rotated per view)
#: and the distance with the per-model fit computed by :func:`cam_dist`
#: (issue #111). The middle four elements (rotate) are fixed constants:
#: they pin *which* face each view sees, verified empirically. The
#: entrypoint never reads the placeholder ``0.0`` values at render time.
#: The sync test ``test_entrypoint_view_cameras_match_views_tuples``
#: compares only the four rotation elements (indices 3–5) between the
#: two files — the translate and dist are both substituted.
VIEWS: list[tuple[str, tuple[float, float, float, float, float, float, float]]] = [
    ("view_00_front.png", (0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)),
    ("view_01_back.png", (0.0, 0.0, 0.0, 0.0, 180.0, 0.0, 0.0)),
    ("view_02_left.png", (0.0, 0.0, 0.0, 0.0, 90.0, 0.0, 0.0)),
    ("view_03_right.png", (0.0, 0.0, 0.0, 0.0, -90.0, 0.0, 0.0)),
    ("view_04_top.png", (0.0, 0.0, 0.0, 90.0, 0.0, 0.0, 0.0)),
    ("view_05_iso.png", (0.0, 0.0, 0.0, 0.0, 45.0, 45.0, 0.0)),
]

#: Multiplier between the model's max bounding-box extent (mm) and the
#: camera distance (mm) for the five axis-aligned views. Calibrated
#: empirically against the pinned image (``openscad/openscad:trixie``,
#: OpenSCAD 2026.01.19): the orthographic projection maps a model of
#: max-extent *S* to *S* × 2016 / *d* pixels in the 800×800 frame, so
#: the exact-fit distance (model fills the frame) is *d* = 2.52 × *S*.
#: Using 3.0 gives a ~19 % margin (3.0 / 2.52) — the model occupies
#: ~84 % of the frame, leaving a visible background border on all four
#: sides for every size from 20 mm to 300 mm.
CAM_DIST_FACTOR: float = 3.0

#: Camera-distance multiplier for the isometric view (45° about Y then 45°
#: about Z), derived from the worst-case projection geometry (issue #234):
#:
#:   - A full-extent box (all three extents equal to ``max_extent`` *S*)
#:     projects to *S*·√3 in the iso view (the box's space diagonal).
#:   - The exact-fit ratio for the orthographic 800×800 frame is 2.52:
#:     a model of extent *S* fills the frame when ``d = 2.52 × S``
#:     (``projected_px = extent_mm × 2016 / d``; setting
#:     ``projected_px = 800`` and ``extent_mm = S`` gives
#:     ``d = S × 2016/800 = 2.52 × S``).
#:   - The minimum visible margin requirement is 3% of the frame on each
#:     edge (≥ 24 px of 800), so the model may occupy at most 94% of the
#:     frame: ``d ≥ 2.52 × S·√3 / 0.94``.
#:
#: ``CAM_DIST_ISO_FACTOR = 2.52 × √3 / 0.94 ≈ 4.6434``
#:
#: This replaces the previous ``CAM_DIST_FACTOR × √2`` (≈ 4.2426), which
#: was calibrated for a cube's 2D-diagonal silhouette (S·√2) and did not
#: cover the √3 worst case with the required 3% margin (it left the 20 mm
#: cube's left edge within the 3-px test band — issue #234).
CAM_DIST_ISO_FACTOR: float = 2.52 * 3.0**0.5 / (1 - 2 * 0.03)

#: Views whose camera tuple carries the isometric rotation (the last
#: element of ``VIEWS``). Used by :func:`cam_dist` to pick the iso
#: multiplier.
ISO_VIEW_NAMES: frozenset[str] = frozenset({"view_05_iso.png"})


def cam_dist(max_extent_mm: float, view_name: str = "") -> float:
    """Camera distance (mm) that frames a model of ``max_extent_mm``.

    Pure function of the bounding box: ``CAM_DIST_FACTOR × max_extent``
    for the five axis-aligned views, ``CAM_DIST_ISO_FACTOR × max_extent``
    for the isometric view. No timestamps, no randomised seeds, no
    wall-clock, no dict-iteration-order dependence — the same bounding
    box always yields the same distance, which is what makes the
    byte-stability acceptance gate (two renders of the same source
    produce identical PNG bytes) hold.

    ``max_extent_mm`` is the largest of the three bounding-box extents
    (``max(bounds[1] - bounds[0])`` from trimesh). A value of 0 or
    below yields 0.0 (the caller classifies degenerate meshes as
    ``empty_model`` before the views are ever rendered).

    This function is a **reference implementation** of the distance
    formula. The authoritative computation at render time is the awk
    in ``entrypoint.sh``, which parses the ASCII STL's vertex lines
    to get the bounding box and applies the same factor. The two are
    kept in sync by the ``CAM_DIST_FACTOR`` literal (guarded by
    ``tests/fast/test_entrypoint_views_sync.py``). The host-side
    trimesh ``mesh.bounds`` path in ``render_for_design_loop`` is not
    used for the render itself — the container self-computes the
    distance from the STL it just wrote — so this function is not
    exercised by the production render pipeline; it exists to pin the
    formula in tests and document the margin rule.
    """
    if max_extent_mm <= 0:
        return 0.0
    factor = CAM_DIST_ISO_FACTOR if view_name in ISO_VIEW_NAMES else CAM_DIST_FACTOR
    return factor * max_extent_mm

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

#: Range bounds for params.json overrides. A malicious or malformed
#: params.json must never be able to raise the resource ceilings above
#: the DoS guard's intent — ``RenderParams.from_dict`` rejects anything
#: outside these bounds with a ``ValueError`` rather than forwarding it
#: verbatim to ``docker run``.
MIN_MEMORY_MB = 64
MAX_MEMORY_MB = 8192
MIN_CPUS = 0.1
MAX_CPUS = 8.0
MIN_PID_LIMIT = 16
MAX_PID_LIMIT = 2048
MIN_TIMEOUT_S = 1
MAX_TIMEOUT_S = 900

#: ``docker --memory`` suffix -> bytes-per-unit multiplier, matching the
#: units \ ``docker run --memory`` itself accepts.
_MEMORY_UNIT_BYTES: dict[str, int] = {
    "b": 1,
    "k": 1024,
    "m": 1024**2,
    "g": 1024**3,
}

_MEMORY_LIMIT_RE = re.compile(r"^([0-9]+(?:\.[0-9]+)?)([bkmg]?)$")


def _parse_memory_limit_mb(value: str) -> float:
    """Parse a ``docker --memory``-style string (e.g. ``"2g"``, ``"512m"``)
    into megabytes. Raises ``ValueError`` on any value that does not
    match the accepted ``<number><unit>`` shape (unit one of b/k/m/g,
    case-insensitive; bare numbers are bytes, matching Docker's own
    default unit)."""
    match = _MEMORY_LIMIT_RE.match(value.strip().lower())
    if not match:
        raise ValueError(
            f"memory_limit {value!r} is not a valid docker --memory value "
            "(expected <number>[b|k|m|g])"
        )
    number_s, unit = match.groups()
    number = float(number_s)
    unit_bytes = _MEMORY_UNIT_BYTES[unit or "b"]
    return (number * unit_bytes) / (1024**2)


def _validate_memory_limit(value: str) -> str:
    mb = _parse_memory_limit_mb(value)
    if not (MIN_MEMORY_MB <= mb <= MAX_MEMORY_MB):
        raise ValueError(
            f"memory_limit {value!r} ({mb:.1f} MiB) is out of the allowed range "
            f"[{MIN_MEMORY_MB}, {MAX_MEMORY_MB}] MiB"
        )
    return value


def _validate_cpus(value: str) -> str:
    try:
        cpus = float(value)
    except ValueError as exc:
        raise ValueError(f"cpus {value!r} is not a valid number") from exc
    if not (MIN_CPUS <= cpus <= MAX_CPUS):
        raise ValueError(
            f"cpus {value!r} is out of the allowed range [{MIN_CPUS}, {MAX_CPUS}]"
        )
    return value


def _validate_pids_limit(value: int) -> int:
    if not (MIN_PID_LIMIT <= value <= MAX_PID_LIMIT):
        raise ValueError(
            f"pids_limit {value!r} is out of the allowed range "
            f"[{MIN_PID_LIMIT}, {MAX_PID_LIMIT}]"
        )
    return value


def _validate_timeout_s(value: int) -> int:
    if not (MIN_TIMEOUT_S <= value <= MAX_TIMEOUT_S):
        raise ValueError(
            f"timeout_s {value!r} is out of the allowed range "
            f"[{MIN_TIMEOUT_S}, {MAX_TIMEOUT_S}]"
        )
    return value


#: Wall-clock bound on the ``docker kill``/``docker rm`` cleanup calls in
#: ``run_container`` — a hung Docker daemon is correlated with the stuck
#: render they clean up, so these must never block indefinitely.
_CLEANUP_TIMEOUT_S = 15

#: Upper bound on the length of a single defines value, in characters.
#: The caller only sets small macro values (dimensions, booleans, enum
#: strings); a value above this bound is treated as a caller bug (possibly
#: a runaway LLM output) and rejected. Matches the ``DEF`` constant in
#: ``entrypoint.sh`` so both layers enforce the same limit.
DEFINES_VALUE_MAX_CHARS = 4096

#: Characters (ordinals < 32 or == 127) that must not appear in a defines
#: key or value. A tab in a value would silently split the TSV that the
#: entrypoint's ``read`` loop uses; other control characters are equally
#: ambiguous in a ``-D`` flag. Rejecting them outright is simpler and safer
#: than escaping them.
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")

#: ``render-<8 hex chars from uuid4>`` — pinned as the testable contract;
#: container and volume share the name.
NAME_PATTERN_RE = re.compile(r"^render-[0-9a-f]{8}$")

#: The locally built render-worker image (Dockerfile + entrypoint.sh, run
#: as uid 1000). Override in tests via monkeypatch on this module attribute.
RENDER_WORKER_IMAGE = "d33d/render-worker:local"

#: The image label the canonical build command stamps with the working
#: tree's :func:`build_hash` (issue #236). The pre-render staleness check
#: compares :func:`build_hash` against ``docker image inspect``'s value for
#: this label; a missing or mismatched label means the image was not built
#: from the current tree and must not render.
BUILD_HASH_LABEL = "d33d/build-hash"

#: The repo files whose content, plus the two BOSL2 build-arg values
#: resolved from the canonical build command in ``docs/bosl2-pinning.md``,
#: feed :func:`build_hash`. entrypoint.sh is COPY'd into the image at
#: build time; the Dockerfile itself is a build input (base pin, USER,
#: build-arg wiring). Both are render-affecting, so both are hashed.
#: Files outside this set (e.g. ``README.md``) are deliberately excluded —
#: they do not change the rendered output.
_HASHED_BUILD_INPUTS: tuple[Path, ...] = (
    Path("entrypoint.sh"),
    Path("Dockerfile"),
)

#: The two build-arg values from the canonical build command (the single
#: source of truth is ``docs/bosl2-pinning.md``), keyed by arg name. The
#: hash includes them because a rebuild under a different BOSL2 tag would
#: change the rendered geometry without touching either hashed file.
_HASHED_BUILD_ARGS: dict[str, str] = {
    "BOSL2_TAG": "v2.0.755",
    "BOSL2_COMMIT": "4e031aafe189efcf4eb0250c24d3216b6a429458",
}


def build_hash(repo_root: Path | None = None) -> str:
    """Content hash of the render-affecting build inputs (issue #236).

    Returns the hex sha256 of, in a fixed order: each file in
    ``_HASHED_BUILD_INPUTS`` (read from the working tree, not git — a
    dirty/modified entrypoint.sh that was never committed must still
    register as a mismatch) and each ``key=value`` pair in
    ``_HASHED_BUILD_ARGS`` sorted by key. Pure and deterministic: the same
    tree yields the same hash on every call; any change to a hashed file or
    arg changes the hash; files outside the hashed set do not.

    The canonical build command (``docs/bosl2-pinning.md``) stamps this
    value into the image as ``LABEL d33d/build-hash="${D33D_BUILD_HASH}"``;
    :func:`_verify_render_worker_image` compares the image's label against
    this hash before any render.
    """
    root = repo_root if repo_root is not None else Path(__file__).resolve().parents[1]
    h = sha256()
    for rel in _HASHED_BUILD_INPUTS:
        path = root / rel
        if not path.is_file():
            raise FileNotFoundError(
                f"build input {rel} missing from {root} — cannot compute the "
                "render-worker build hash; run from a complete checkout"
            )
        h.update(rel.as_posix().encode("utf-8"))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    for key in sorted(_HASHED_BUILD_ARGS):
        h.update(f"{key}={_HASHED_BUILD_ARGS[key]}".encode("utf-8"))
        h.update(b"\0")
    return h.hexdigest()


def _docker_image_labels(image: str) -> dict[str, str] | None:
    """``docker image inspect --format '{{json .Config.Labels}}' <image>``

    Returns the image's label dict, or ``None`` when the image is absent
    (inspect non-zero exit). A present-but-unlabeled image yields an empty
    dict — the caller's missing-label path handles it. A docker daemon
    outage or missing docker binary raises :class:`OSError` (or
    :class:`subprocess.SubprocessError`) so the caller can distinguish
    "cannot query docker" from "image absent/stale" and not claim
    staleness it cannot have verified.
    """
    proc = subprocess.run(
        ["docker", "image", "inspect", "--format", "{{json .Config.Labels}}", image],
        capture_output=True,
        check=False,
        timeout=15,
    )  # subprocess.run, not check=True: absent image is the normal path
    if proc.returncode != 0:
        return None
    parsed = json.loads(proc.stdout.decode("utf-8") or "null")
    # Docker emits `{}` for a present-but-unlabeled image and `null` for an
    # unlabeled one; a JSON value that is not a dict is malformed output —
    # treat it as no labels so the caller's missing-label path handles it
    # instead of crashing on an unexpected shape.
    if not isinstance(parsed, dict):
        return {}
    return parsed or {}


def _verify_render_worker_image(
    image: str,
    repo_root: Path | None = None,
    expected_hash: str | None = None,
) -> None:
    """Pre-render staleness guard (issue #236): the image must carry
    ``BUILD_HASH_LABEL`` equal to the working tree's :func:`build_hash`.

    Raises :class:`RuntimeError` naming the canonical rebuild command on:
    image absent (no labels), label missing, label not a string, or label
    != computed hash. A docker-query failure (daemon down, docker binary
    missing, a hung daemon — the inspect call's ``subprocess.TimeoutExpired``
    — or ``subprocess.run`` itself raising for any other
    :class:`subprocess.SubprocessError`; the design-loop test harnesses stub
    ``subprocess.run`` without an inspect branch and let it raise) is NOT
    staleness and propagates as an :class:`OSError` so the caller can
    distinguish it from a verified mismatch. Raises
    :class:`FileNotFoundError` when a hashed build input is missing (the
    caller maps that to its own loud failure).
    """
    if expected_hash is None:
        expected_hash = build_hash(repo_root)
    rebuild_msg = (
        "render-worker image staleness check failed: the image does not "
        "carry a build-hash matching the working tree (image absent, label "
        f"{BUILD_HASH_LABEL!r} missing, or label != working-tree hash). Rebuild "
        f"with the canonical command from docs/bosl2-pinning.md: {canonical_build_command()}"
    )
    try:
        labels = _docker_image_labels(image)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        # Docker-query failure (including the inspect call's
        # subprocess.TimeoutExpired — a hung daemon is "cannot query
        # docker", never a verified mismatch): not staleness. The caller
        # maps this to its own loud failure (the design-loop harnesses
        # stub subprocess.run without an inspect branch and let it raise —
        # that path must not be mistaken for a verified mismatch).
        raise OSError(
            "render-worker staleness check could not query docker: "
            f"{exc}"
        ) from exc
    if labels is None:
        raise RuntimeError(rebuild_msg + " (image not found)")
    label_value = labels.get(BUILD_HASH_LABEL)
    if not isinstance(label_value, str) or label_value != expected_hash:
        raise RuntimeError(rebuild_msg + " (label mismatch/missing)")


def canonical_build_command() -> str:
    """The canonical render-worker rebuild command, derived from the
    module's own constants (``_HASHED_BUILD_ARGS`` values and
    :data:`RENDER_WORKER_IMAGE`) — the single source of truth for the
    command the doc and the :func:`_verify_render_worker_image` error
    message both quote. ``D33D_BUILD_HASH`` is computed at build time via
    ``uv run python`` so the working tree's :func:`build_hash` stamps the
    image label (the doc-drift guard in
    ``tests/fast/test_image_staleness.py`` keeps doc, code and error
    message in sync).
    """
    bosl2_tag = _HASHED_BUILD_ARGS["BOSL2_TAG"]
    bosl2_commit = _HASHED_BUILD_ARGS["BOSL2_COMMIT"]
    return (
        "docker build --platform=linux/amd64 "
        f"--build-arg BOSL2_TAG={bosl2_tag} "
        f"--build-arg BOSL2_COMMIT={bosl2_commit} "
        "--build-arg D33D_BUILD_HASH=\"$(uv run python -c 'from d33d.render_worker "
        "import build_hash; print(build_hash())')\" "
        f"-t {RENDER_WORKER_IMAGE} ."
    )


def _render_host_tmp_base() -> Path:
    """Directory that host-side render temp dirs live under.

    Must NOT be ``/tmp`` — in Docker Desktop the host ``/tmp`` is not
    shared with the VM, so a helper container mounting ``/tmp/...``
    sees an empty directory. Use ``~/d33d/render-tmp`` instead, which
    the Docker daemon can bind-mount into the helper container.
    """
    path = Path(os.environ.get("D33D_RENDER_TMP", str(Path.home() / "d33d" / "render-tmp")))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _render_persist_base() -> Path | None:
    """Default base dir for persisted render artifacts (issue #72).

    The design loop's best-iteration STL + 6 view PNGs are copied here
    (``<base>/<render-key>/``) INSIDE the render worker's with-block,
    before the ``TemporaryDirectory`` is torn down — the SSE adapter
    reads them from disk into the frame's data URIs, so the bytes must
    survive the tempdir.

    ``D33D_RENDER_PERSIST_DIR`` (default ``~/.d33d/renders`` — mirrors
    ``D33D_DATA_DIR``'s ``~/.d33d`` convention). Returns ``None`` when
    the base cannot be created (read-only FS, permission) — persistence
    is best-effort and must never change the render outcome."""
    path = Path(
        os.environ.get("D33D_RENDER_PERSIST_DIR", str(Path.home() / ".d33d" / "renders"))
    )
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    return path


def project_renders_dir(data_dir: str | Path, project_id: int | str) -> Path:
    """The project-scoped renders directory (issue #72).

    ``<data_dir>/projects/{project_id}/renders`` — one durable subtree per
    project so the worker's post-harvest persistence
    (:func:`d33d.render_worker._persist_render_artifacts`) lands renders
    next to the project data rather than under the global
    ``D33D_RENDER_PERSIST_DIR`` default. The production design-loop call
    sites (``d33d.app._build_production_design_loop`` and
    ``d33d.versions_routes._finalize_loop_kwargs``) build a
    ``render_fn`` closure that binds this path and passes it as the
    ``renders_dir`` kwarg to :func:`d33d.render_worker.render_for_design_loop`
    so the persistence step actually fires for chat and finalize runs.
    """
    return Path(data_dir) / "projects" / str(project_id) / "renders"


def parse_defines(params_json_text: str) -> dict[str, str]:
    """Parse a ``params.json`` text payload and return the validated
    ``defines`` map.

    This is the **authoritative** validation for the ``params.json`` file
    written by the caller into the work volume before ``docker run``.
    It runs on the host where Python's ``json`` module is available — the
    entrypoint's awk gate is a second, trivially simple layer that keeps
    the container safe even if this validation is skipped.

    Raises ``ValueError`` (the render caller maps this to
    ``error_class="container_error"``) on:

    - the text is not valid JSON
    - the top-level value is not a JSON object
    - ``defines`` is present but is not an object
    - a defines key is not a string
    - a defines value is not a string
    - a defines key or value contains a tab, newline, or other control
      character (ord < 32 or == 127) — a tab in a value would silently
      split the TSV the entrypoint reads, and other control characters
      are equally ambiguous in a ``-D`` flag
    - a defines value exceeds ``DEFINES_VALUE_MAX_CHARS`` characters

    Returns an empty dict when ``defines`` is absent (the caller only
    writes the key when it has defines to pass).
    """
    try:
        data = json.loads(params_json_text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"params.json is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise TypeError("params.json must be a JSON object at the top level")
    raw = data.get("defines")
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise TypeError(
            f"params.json 'defines' must be an object, got {type(raw).__name__}"
        )
    return _validate_defines_map(raw)

#: The eight on-volume artifacts a fully successful run produces.
ARTIFACT_STL = "model.stl"
ARTIFACT_CSG = "model.csg"


def new_render_name() -> str:
    """Fresh ``render-<8 hex chars from uuid4>`` for one render run."""
    return f"render-{uuid.uuid4().hex[:8]}"


def validate_render_name(name: str) -> bool:
    """True iff ``name`` matches ``render-<8 hex chars>``."""
    return bool(NAME_PATTERN_RE.match(name))


def _validate_defines_map(raw: Any) -> dict[str, str]:
    """Validate a raw ``defines`` dict (from JSON or in-memory) and return
    a clean ``dict[str, str]``.

    Shared by :func:`parse_defines` (raw-text path) and
    ``RenderParams.from_dict`` (in-memory path) so both enforce the same
    invariants: keys and values must be strings, no control characters,
    and values must be at most ``DEFINES_VALUE_MAX_CHARS`` characters.
    """
    if not isinstance(raw, dict):
        raise TypeError(
            f"defines must be a dict/object, got {type(raw).__name__}"
        )
    result: dict[str, str] = {}
    for k, v in raw.items():
        if not isinstance(k, str):
            raise TypeError(f"defines key {k!r} is not a string")
        if not isinstance(v, str):
            raise TypeError(
                f"defines[{k!r}] value {v!r} is not a string "
                f"(got {type(v).__name__})"
            )
        if _CONTROL_CHAR_RE.search(k):
            raise ValueError(
                f"defines key {k!r} contains a control character"
            )
        if _CONTROL_CHAR_RE.search(v):
            raise ValueError(
                f"defines[{k!r}] value contains a control character"
            )
        if len(v) > DEFINES_VALUE_MAX_CHARS:
            raise ValueError(
                f"defines[{k!r}] value is {len(v)} characters, "
                f"exceeding the {DEFINES_VALUE_MAX_CHARS} character cap"
            )
        result[k] = v
    return result


@dataclass(frozen=True)
class RenderParams:
    """The ``params.json`` payload, with the four overridable defaults.

    ``defines`` is a flat string map passed as ``-Dname=value``; ``{}``
    means no defines.

    Every instance is range-validated in ``__post_init__`` regardless of
    construction path, so a ``RenderParams`` object can never carry
    out-of-bounds resource values into ``build_docker_argv`` — not via
    ``from_dict``, not via a direct constructor call, not via any future
    code path. Out-of-range or malformed values raise ``ValueError``.
    """

    defines: dict[str, str] = field(default_factory=dict)
    timeout_s: int = DEFAULT_TIMEOUT_S
    memory_limit: str = DEFAULT_MEMORY_LIMIT
    cpus: str = DEFAULT_CPU_LIMIT
    pids_limit: int = DEFAULT_PID_LIMIT

    def __post_init__(self) -> None:
        object.__setattr__(self, "defines", _validate_defines_map(self.defines))
        object.__setattr__(self, "timeout_s", _validate_timeout_s(self.timeout_s))
        object.__setattr__(
            self, "memory_limit", _validate_memory_limit(self.memory_limit)
        )
        object.__setattr__(self, "cpus", _validate_cpus(self.cpus))
        object.__setattr__(self, "pids_limit", _validate_pids_limit(self.pids_limit))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> RenderParams:
        """Parse a params dict; unknown keys are ignored, missing keys
        fall back to the module defaults.

        ``timeout_s``, ``memory_limit``, ``cpus`` and ``pids_limit`` are
        range-validated (via ``__post_init__``) against the module bounds
        (see ``MIN_*``/``MAX_*`` constants) so a malicious or malformed
        params.json can never raise the resource ceilings above the DoS
        guard's intent; out-of-range or malformed values raise
        ``ValueError``.

        ``defines`` values are validated for control characters and the
        ``DEFINES_VALUE_MAX_CHARS`` size cap — the same invariants
        enforced by :func:`parse_defines` for the raw-text path.
        """
        raw_defines = data.get("defines")
        return cls(
            defines=raw_defines if raw_defines is not None else {},
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


#: ``[entrypoint] <marker> <step>`` — the container's per-artifact progress
#: markers (issue #121). The entrypoint writes one ``view-start``/
#: ``view-done`` (or ``view-failed``) pair per artifact — ``stl`` and
#: ``csg`` plus the six view stems — to its STDERR, flushed after each line
#: (see entrypoint.sh), so the host can read them WHILE the container is
#: still running. The event is CAUSED by the artifact actually finishing
#: (the marker is written after the openscad call returns) — never by a
#: timer, an elapsed-time estimate, or an assumed per-view duration
#: (issue #121's core requirement: the measurement must be real).
_ENTRYPOINT_MARKER_RE = re.compile(
    r"\[entrypoint\] (view-start|view-done|view-failed) (\S+)"
)

#: The two progress markers the design loop surfaces as per-view SSE frames
#: (issue #121). ``view-start`` is the "render is working" signal; the
#: per-view progress the SPA's "4 of 6" counter tracks is the completed
#: view, so the completion marker (``view-done``) is the arrival event.
#: ``view-failed`` stops the stream for that artifact — a failed view must
#: not report as complete (phantom views).
MARKER_DONE = "view-done"
MARKER_FAILED = "view-failed"


@dataclass
class _StderrReader:
    """Drains a running child's stderr in a background thread.

    The render container writes unbounded stderr (openscad diagnostics are
    appended to /work/render.log, but the entrypoint's own markers and any
    uncaptured output flow through the pipe). A full pipe buffer would
    DEADLOCK the container, so the reader thread is the only thing that
    keeps the render unblocked; it is ALWAYS joined before
    :func:`run_container` returns (every path joins, including the timeout
    and exception paths) so no thread leaks past the call.

    The reader accumulates the full stderr byte stream (truncation to the
    256 KiB contract happens at the caller, exactly as with the legacy
    ``subprocess.run(capture_output=True)`` path) and feeds each
    line-buffered chunk to :func:`parse_entrypoint_markers` as it arrives —
    that is how a per-view event reaches the host before container exit.
    Line buffering is the reader's own (it splits on ``\\n`` across chunk
    boundaries); the container side never sees a full buffer because this
    thread is continuously consuming.
    """

    pipe: Any
    on_marker: Any  # Callable[[str, str], None] | None
    _eof: bool = False
    stderr_chunks: list[bytes] = field(default_factory=list)
    _lock: Any = None
    _thread: Any = None
    _buf: bytes = b""
    _done: Any = None  # threading.Event — set when the drain loop exits

    def __post_init__(self) -> None:
        import threading

        self._lock = threading.Lock()
        self._done = threading.Event()

    def start(self) -> None:
        import threading

        self._thread = threading.Thread(target=self._drain, daemon=True)
        self._thread.start()

    def _drain(self) -> None:
        try:
            while True:
                chunk = self.pipe.read(65536)
                if not chunk:
                    break
                with self._lock:
                    self.stderr_chunks.append(chunk)
                self._feed_lines(chunk)
            self.mark_eof()
            self._feed_lines(b"")  # flush any partial trailing line
        finally:
            self._done.set()

    def _feed_lines(self, chunk: bytes) -> None:
        with self._lock:
            self._buf += chunk
            ready: list[bytes] = []
            while b"\n" in self._buf:
                line, self._buf = self._buf.split(b"\n", 1)
                ready.append(line)
            if self._eof and self._buf:
                ready.append(self._buf)
                self._buf = b""
        # Dispatch OUTSIDE the lock — the callback (on_marker) may block
        # (e.g. a queue.put with a full queue); holding the lock would
        # deadlock the drain thread and stall the render (the pipe buffer
        # fills and the container blocks on write).
        for line in ready:
            self._dispatch(line)

    def _dispatch(self, line: bytes) -> None:
        if self.on_marker is None:
            return
        text = line.decode("utf-8", errors="replace")
        for marker, step in parse_entrypoint_markers(text):
            self.on_marker(marker, step)

    def mark_eof(self) -> None:
        self._eof = True

    def join(self) -> None:
        """Wait for the drain thread to exit. Always safe to call.

        The drain loop exits on EOF (the child closed its stderr) or an
        OSError (the child died); ``pipe.read`` on a closed/pipe-backed
        stream returns b"" at EOF, so a well-behaved child always
        terminates the thread. A pathological reader (a pipe that neither
        closes nor errors) is impossible for a subprocess whose stderr is
        a pipe — the child's death closes the write end.
        """
        if self._thread is not None:
            self._thread.join()

    def bytes(self) -> bytes:
        with self._lock:
            return b"".join(self.stderr_chunks)


def parse_entrypoint_markers(
    line: str,
) -> list[tuple[str, str]]:
    """The entrypoint markers carried by one stderr line.

    Returns ``(marker, step)`` pairs — ``marker`` is one of ``view-start``
    / ``view-done`` / ``view-failed`` and ``step`` the artifact name
    (``stl``, ``csg`` or a view stem such as ``view_00_front``). A line
    with no marker yields ``[]``; a line is allowed to carry at most one
    marker (the entrypoint writes one per line), but the return type is a
    list so a future multi-marker line parses without a contract change.
    """
    out: list[tuple[str, str]] = []
    for m in _ENTRYPOINT_MARKER_RE.finditer(line):
        out.append((m.group(1), m.group(2)))
    return out


def run_container(
    argv: list[str],
    timeout_s: int,
    on_marker: Any = None,
) -> subprocess.CompletedProcess:
    """Execute a ``docker run`` argv with a wall-clock timeout.

    ``subprocess.run`` enforces ``timeout_s`` on the whole call; on
    ``TimeoutExpired`` the container (started without ``--rm`` by
    ``build_docker_argv``) is killed and removed before returning, since
    nothing else in the render lifecycle would ever clean it up.

    A timed-out run returns a sentinel ``CompletedProcess`` with
    ``returncode == 124`` (the ``timeout`` row documented on
    :func:`classify`); the caller maps it via
    ``classify(timed_out=True, exit_code=proc.returncode, ...)`` — the
    ``timed_out`` flag is the authoritative signal, 124 is just the
    recorded exit code. ``build_docker_argv`` bounds memory, cpus and
    pids but NOT wall-clock time, so without this wrapper the ``timeout``
    class in the closed ``ErrorClass`` enum is unreachable for a runaway
    ``.scad``.

    The caller contract: pass ``params.timeout_s`` (default 120s) as
    ``timeout_s``.

    ``on_marker`` (issue #121): when given, the child's stderr is drained
    in a background thread instead of accumulated by ``subprocess.run``,
    and each entrypoint marker line reaches ``on_marker(marker, step)``
    WHILE THE CONTAINER IS STILL RUNNING. The stderr bytes are preserved
    identically to the blocking path (full stream, then the caller's
    256 KiB ``truncate_stderr`` contract — see :func:`truncate_stderr`),
    and the timeout path still returns the 124 sentinel with the same
    kill+rm cleanup, so the ``timeout`` / ``oom`` classification rows are
    unchanged. ``on_marker=None`` (the default) preserves the legacy
    blocking ``subprocess.run`` behaviour exactly — every existing caller
    and its tests are unaffected.
    """
    if on_marker is None:
        try:
            return subprocess.run(
                argv, timeout=timeout_s, capture_output=True, check=False
            )
        except subprocess.TimeoutExpired:
            name = _argv_container_name(argv)
            if name:
                _cleanup_container(name)
            return subprocess.CompletedProcess(
                args=argv, returncode=124, stdout=b"", stderr=b""
            )

    reader = _StderrReader(pipe=None, on_marker=on_marker)
    proc = None
    try:
        proc = subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )
        reader.pipe = proc.stderr
        reader.start()
        try:
            proc.wait(timeout=timeout_s)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
            name = _argv_container_name(argv)
            if name:
                _cleanup_container(name)
            reader.join()
            return subprocess.CompletedProcess(
                args=argv, returncode=124, stdout=b"", stderr=b""
            )
        reader.join()
        return subprocess.CompletedProcess(
            args=argv,
            returncode=proc.returncode or 0,
            stdout=b"",
            stderr=reader.bytes(),
        )
    except subprocess.TimeoutExpired:
        # Popen itself raised (e.g. the docker CLI refused to start under
        # the timeout budget): same kill+rm+124 sentinel as the legacy path.
        name = _argv_container_name(argv)
        if name:
            _cleanup_container(name)
        if reader._thread is not None:
            reader.join()
        return subprocess.CompletedProcess(
            args=argv, returncode=124, stdout=b"", stderr=b""
        )
    except (OSError, ValueError):
        if reader._thread is not None:
            reader.join()
        raise
    finally:
        # Close the pipe on every path so the drain thread cannot outlive
        # the call; the thread is daemon and joined on every path above,
        # but a leaked pipe would hold the reader's lock indefinitely.
        if reader.pipe is not None:
            try:
                reader.pipe.close()
            except OSError:
                pass


def _cleanup_container(name: str) -> None:
    """``docker kill`` then ``docker rm`` a named container, each bounded
    by ``_CLEANUP_TIMEOUT_S``.

    Both calls are best-effort: a hung daemon (correlated failure with the
    stuck render) or a failed removal must not propagate — the run still
    classifies as ``timeout`` either way. A hung or failed cleanup leaves
    the container leaked, so each failure path logs a warning naming the
    container so the leak is discoverable in logs.
    """
    for verb in ("kill", "rm"):
        argv = ["docker", verb, name]
        try:
            proc = subprocess.run(
                argv, timeout=_CLEANUP_TIMEOUT_S, capture_output=True, check=False
            )
        except subprocess.TimeoutExpired:
            print(
                f"[render_worker] WARNING: 'docker {verb} {name}' timed out after "
                f"{_CLEANUP_TIMEOUT_S}s; container may be leaked",
                file=sys.stderr,
            )
            return
        if proc.returncode != 0:
            print(
                f"[render_worker] WARNING: 'docker {verb} {name}' exited {proc.returncode}; "
                f"container may be leaked",
                file=sys.stderr,
            )
            return


def _argv_container_name(argv: list[str]) -> str | None:
    """The ``-n`` / ``--name`` value of a ``docker run`` argv, or ``None``."""
    for i, tok in enumerate(argv[:-1]):
        if tok in ("--name", "-n"):
            return argv[i + 1]
    return None


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
    empty geometry.

    Derived from :func:`classify` rather than re-implementing the ``ok``
    predicate by hand, so the two can never silently diverge — ``ok`` iff
    ``classify(...) == "ok"``.
    """
    return (
        classify(
            exit_code=exit_code,
            stl_path=stl_path,
            csg_path=csg_path,
            views=views,
            vertex_count=vertex_count,
            watertight=watertight,
            volume=volume,
        )
        == "ok"
    )


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
    render_artifact_dir: str | None = None

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


def _persist_render_artifacts(
    stl: Path, views: list[Path], base: Path | None, key: str
) -> str | None:
    """Copy the harvested ``model.stl`` + the 6 ``view_*.png`` into
    ``<base>/<key>/`` (issue #72's post-harvest persistence step).

    Must be called INSIDE the ``TemporaryDirectory`` with-block — the
    harvested paths die when the block exits. Copies are best-effort:
    any ``OSError`` mid-copy cleans up the partial directory (no
    orphaned half-written artifacts) and returns ``None``. Returns the
    durable directory path on success, ``None`` when persistence is
    skipped (no base) or failed."""
    if base is None:
        return None
    target = base / key
    try:
        target.mkdir(parents=True)
        (target / stl.name).write_bytes(stl.read_bytes())
        for v in views:
            (target / v.name).write_bytes(v.read_bytes())
    except OSError:
        try:
            shutil.rmtree(target)
        except OSError:
            pass
        return None
    return str(target)


def render_for_design_loop(
    scad_source: str,
    defines: dict[str, str],
    renders_dir: str | Path | None = None,
    on_progress: Any = None,
    repo_root: Path | None = None,
) -> RenderResult:
    """One render-worker run for the design loop (issue #4's pipeline).

    Ephemeral named Docker volume (no host bind mounts); the ``.scad``
    source is copied into the volume, the pinned OpenSCAD image compiles
    under the worker's container contract (``build_docker_argv``), and
    :func:`d33d.render_worker.classify` maps the run onto the closed
    7-class ``error_class`` enum. Any exception in the pipeline is a
    ``container_error`` — a loop render failure is a classified render
    outcome, never an unclassified raise.

    ``on_progress`` (issue #121): a sync callback ``(kind, payload)``
    invoked from the render container's stderr-drain thread (a worker
    thread, NOT the event loop) for each entrypoint marker line —
    ``kind`` is ``"view-start"`` or ``"view-done"`` (``view-failed`` is
    NOT delivered — a failed view must not report as complete),
    ``payload`` carries ``view`` (the view stem), ``index`` (the
    0-based view index for ``view_done``) and, when the hook carries a
    stamped ``_current`` int >= 1 (the design loop stamps it once per
    pass — see ``d33d.design_loop._stamp_on_progress_iteration``),
    ``iteration`` (the 1-based design-loop iteration index). The
    callback MUST be fast and side-effect-free; it fires while the
    container is still running. ``None`` (the default) preserves the
    legacy blocking behaviour.

    Post-harvest persistence (issue #72): on a fully ``ok`` render, the
    harvested ``model.stl`` + 6 ``view_*.png`` are copied into
    ``<persist base>/<uuid8>/`` INSIDE the ``TemporaryDirectory``
    with-block (the harvested paths die when the block exits) and
    ``RenderResult.render_artifact_dir`` carries the durable path to
    the SSE adapter. The base is ``renders_dir`` when given, else
    :func:`_render_persist_base` (``D33D_RENDER_PERSIST_DIR``, default
    ``~/.d33d/renders``). A non-``ok`` render persists nothing — no
    orphaned partial directories.
    """
    name = new_render_name()
    volume = f"d33d-render-{name}"
    start = time.monotonic()
    # Durable per-render key (issue #72): generated BEFORE the with-block
    # so the persistence copy inside it lands at a path the caller (the
    # SSE adapter) can learn from ``RenderResult.render_artifact_dir``.
    persist_base = Path(renders_dir) if renders_dir is not None else _render_persist_base()
    render_key = uuid.uuid4().hex[:8]
    try:
        # Pre-render staleness guard (issue #236): the render-worker image
        # must carry BUILD_HASH_LABEL == build_hash() before any expensive
        # work — a stale image (entrypoint.sh / Dockerfile changed since the
        # build) fails loudly with the canonical rebuild command instead of
        # rendering with baked-in code that no longer matches the source.
        # A docker-query failure (daemon down, docker binary missing) is NOT
        # staleness — the render proceeds and fails with its own docker
        # error. The guard is also monkeypatched by the design-loop test
        # harnesses (test_iteration_stamp, test_per_view_progress) which
        # stub subprocess.run without a docker-image-inspect branch.
        try:
            _verify_render_worker_image(RENDER_WORKER_IMAGE, repo_root=repo_root)
        except FileNotFoundError as exc:
            # Missing build input is a repo-state problem, not image
            # staleness — log and let the render proceed; it will fail with
            # its own error if the tree is truly broken.
            logger.warning("render-worker staleness check skipped: %s", exc)
        except OSError as exc:
            # Docker-query failure (daemon down, docker binary missing) is
            # not staleness — let the render proceed; it will fail with its
            # own docker error if docker is actually unavailable.
            logger.warning("render-worker staleness check skipped: %s", exc)
        except RuntimeError as staleness_exc:
            # Verified mismatch or absent label: fail LOUDLY before any
            # expensive work — no docker volume create, no seed helper, no
            # render. The resolved gate decision (option b, issue #236)
            # maps this to the existing closed enum: error_class
            # "container_error" with the actionable rebuild command in
            # stderr, which the design-loop SSE adapter surfaces as a
            # user-visible failure.
            return RenderResult(
                ok=False,
                exit_code=1,
                duration_ms=0,
                error_class="container_error",
                stderr=str(staleness_exc),
                stl=None,
                csg=None,
                views=(),
            )
        subprocess.run(
            ["docker", "volume", "create", volume],
            capture_output=True,
            check=False,
        )
        with tempfile.TemporaryDirectory(dir=_render_host_tmp_base()) as tmp:
            host_tmp = Path(tmp)
            src = host_tmp / "src"
            src.mkdir()
            (src / "model.scad").write_text(scad_source, encoding="utf-8")
            params = RenderParams(defines=dict(defines))
            (src / "params.json").write_text(
                json.dumps({"defines": params.defines}), encoding="utf-8"
            )
            # Copy the source into the named volume via a helper container
            # (named volumes are only writable from a container bound to
            # them — no host bind mounts).
            helper_argv = [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--volume",
                f"{volume}:/work",
                "--volume",
                f"{tmp}:/host:ro",
                "busybox:latest",
                "sh",
                "-c",
                (
                    "cp /host/src/model.scad /work/model.scad && "
                    "cp /host/src/params.json /work/params.json && "
                    "chown 1000:1000 /work"
                ),
            ]
            helper_proc = subprocess.run(
                helper_argv, capture_output=True, check=False
            )
            if helper_proc.returncode != 0:
                duration_ms = int((time.monotonic() - start) * 1000)
                return RenderResult(
                    ok=False,
                    exit_code=helper_proc.returncode,
                    duration_ms=duration_ms,
                    error_class="container_error",
                    stderr=(
                        "seed helper failed: "
                        + truncate_stderr(helper_proc.stderr)
                    ),
                    stl=None,
                    csg=None,
                    views=(),
                )
            # Post-seed verification: the source file must actually be in the
            # volume before the (much more expensive) render worker launches.
            verify_argv = [
                "docker",
                "run",
                "--rm",
                "--network",
                "none",
                "--volume",
                f"{volume}:/work",
                "busybox:latest",
                "test",
                "-f",
                "/work/model.scad",
            ]
            verify_proc = subprocess.run(
                verify_argv, capture_output=True, check=False
            )
            if verify_proc.returncode != 0:
                duration_ms = int((time.monotonic() - start) * 1000)
                return RenderResult(
                    ok=False,
                    exit_code=verify_proc.returncode,
                    duration_ms=duration_ms,
                    error_class="container_error",
                    stderr=(
                        "seed verification failed: /work/model.scad missing "
                        "in volume " + volume
                    ),
                    stl=None,
                    csg=None,
                    views=(),
                )
            argv = build_docker_argv(
                image=RENDER_WORKER_IMAGE,
                name=name,
                workdir_volume=volume,
                params=params,
            )
            if on_progress is not None:
                _view_index_by_stem = {
                    name[:-4]: i for i, (name, _c) in enumerate(VIEWS)
                }

                def _on_marker(marker: str, step: str) -> None:
                    if marker not in ("view-start", "view-done"):
                        return  # view-failed: never report a failed view as complete
                    payload: dict[str, Any] = {"view": step}
                    idx = _view_index_by_stem.get(step)
                    if idx is not None:
                        payload["index"] = idx
                    # issue #121: the payload carries the 1-based design-loop
                    # iteration index. The loop stamps it onto the hook
                    # (``on_progress._current``) once per iteration; the
                    # worker forwards it verbatim (absent — a non-loop
                    # caller or a hook that refuses attributes — the field
                    # is omitted, the adapter defaults to 0, and the client
                    # treats 0 as "unknown").
                    stamped = getattr(on_progress, "_current", None)
                    if isinstance(stamped, int) and not isinstance(stamped, bool) and stamped >= 1:
                        payload["iteration"] = stamped
                    on_progress(marker, payload)

                proc = run_container(argv, timeout_s=params.timeout_s, on_marker=_on_marker)
            else:
                proc = run_container(argv, timeout_s=params.timeout_s)
            duration_ms = int((time.monotonic() - start) * 1000)
            stderr = truncate_stderr(proc.stderr)

            def _harvest() -> tuple[Path, Path, list[Path]]:
                out = host_tmp / "out"
                out.mkdir()
                harvest_argv = [
                    "docker",
                    "run",
                    "--rm",
                    "--network",
                    "none",
                    "--volume",
                    f"{volume}:/work",
                    "--volume",
                    f"{out}:/host",
                    "busybox:latest",
                    "sh",
                    "-c",
                    (
                        "cp /work/model.stl /host/ 2>/dev/null; "
                        "cp /work/model.csg /host/ 2>/dev/null; "
                        "cp /work/view_*.png /host/ 2>/dev/null; true"
                    ),
                ]
                subprocess.run(harvest_argv, capture_output=True, check=False)
                stl = out / "model.stl"
                csg = out / "model.csg"
                views = sorted(out.glob("view_*.png"))
                return stl, csg, views

            if proc.returncode != 0:
                error_class = classify(
                    exit_code=proc.returncode,
                    stl_path=None,
                    stderr=stderr,
                    timed_out=proc.returncode == 124,
                )
                return RenderResult(
                    ok=False,
                    exit_code=proc.returncode,
                    duration_ms=duration_ms,
                    error_class=error_class,
                    stderr=stderr,
                    stl=None,
                    csg=None,
                    views=(),
                )

            stl, csg, views = _harvest()
            vertex_count = 0
            watertight = False
            volume_mm3 = 0.0
            if stl.is_file():
                try:
                    import trimesh

                    # force="mesh" (issue #86): trimesh.load returns a
                    # Trimesh for a single-body STL but a trimesh.Scene
                    # for a zero-facet STL (no solid block carries
                    # geometry) and for a multi-solid ASCII STL (one
                    # ``solid``/``endsolid`` block per body). Multi-solid
                    # output can come from other producers; OpenSCAD
                    # always merges top-level objects into a single solid
                    # block, so this case is defensive rather than
                    # OpenSCAD-specific. A Scene has no
                    # merge_vertices/vertices/is_watertight — reading them
                    # raises AttributeError, which escapes BOTH except
                    # tuples below (neither covers AttributeError) and
                    # crashes render_for_design_loop; the render then
                    # surfaces as an unclassified failure instead of being
                    # measured. force="mesh" concatenates a Scene's
                    # geometries into a single Trimesh, so the same code
                    # path handles every STL shape.
                    mesh = trimesh.load(str(stl), process=False, force="mesh")
                    # OpenSCAD's STL export emits per-facet DUPLICATED
                    # vertices, so with process=False the mesh must be
                    # merged before a watertightness check is meaningful
                    # (an unmerged valid cube reports watertight=False →
                    # bogus empty_model). merge_vertices() is deliberately
                    # narrower than process=True, which would also drop
                    # degenerate/duplicate faces. It is required for the
                    # concatenated multi-body mesh too — each component
                    # body carries its own per-facet duplicates.
                    mesh.merge_vertices()
                    vertex_count = len(mesh.vertices)
                    watertight = bool(mesh.is_watertight)
                    volume_mm3 = float(mesh.volume)
                except (OSError, ValueError, IndexError):
                    # mesh.volume raises IndexError on a malformed/
                    # degenerate STL (merge_vertices() does not — it
                    # returns None silently on a vertex-only mesh).
                    # Classify as empty_model, never crash the
                    # render pipeline.
                    pass
            views_ok = all(v.is_file() for v in views)
            error_class = classify(
                exit_code=proc.returncode,
                stl_path=str(stl) if stl.is_file() else None,
                csg_path=str(csg) if csg.is_file() else None,
                views=[str(v) for v in views] if views_ok else None,
                stderr=stderr,
                timed_out=proc.returncode == 124,
                vertex_count=vertex_count,
                watertight=watertight,
                volume=volume_mm3,
            )
            # Post-harvest persistence (issue #72): a fully ``ok`` render
            # copies the STL + 6 view PNGs to the durable per-render
            # directory INSIDE the with-block (the harvested paths die
            # when the block exits). A non-ok render persists nothing.
            render_artifact_dir: str | None = None
            if error_class == "ok" and stl.is_file():
                render_artifact_dir = _persist_render_artifacts(
                    stl, views, persist_base, render_key
                )
            # Durable path for .stl/.views (issue #87): the harvested
            # paths live in the TemporaryDirectory above, which is torn
            # down when this function returns — so a consumer that reads
            # RenderResult.stl afterwards (e.g. the bbox gate's
            # bbox_from_render) would see a dead path. When persistence
            # succeeded (render_artifact_dir is set), re-point .stl and
            # .views at the durable copies: _persist_render_artifacts
            # writes the STL under its fixed name "model.stl" (only the
            # DIRECTORY is uuid8-keyed) plus the 6 view PNGs. .csg stays
            # on the temp path because persistence does NOT copy it —
            # the csg is only consumed in-process (ticket #6's CSG
            # registry), never by a post-return reader.
            if render_artifact_dir is not None:
                artifact_dir = Path(render_artifact_dir)
                stl_path = str(artifact_dir / stl.name)
                views_paths = tuple(str(artifact_dir / v.name) for v in views)
            else:
                stl_path = str(stl) if stl.is_file() else None
                views_paths = tuple(str(v) for v in views) if views_ok else ()
            return RenderResult(
                ok=error_class == "ok",
                exit_code=proc.returncode,
                duration_ms=duration_ms,
                error_class=error_class,
                stderr=stderr,
                stl=stl_path,
                csg=str(csg) if csg.is_file() else None,
                views=views_paths,
                render_artifact_dir=render_artifact_dir,
            )
    except (OSError, RuntimeError, ValueError) as e:
        return RenderResult(
            ok=False,
            exit_code=1,
            duration_ms=0,
            error_class="container_error",
            stderr=f"render pipeline error: {e}",
            stl=None,
            csg=None,
            views=(),
        )
    finally:
        try:
            subprocess.run(
                ["docker", "volume", "rm", "-f", volume],
                capture_output=True,
                check=False,
            )
        except OSError:
            pass
