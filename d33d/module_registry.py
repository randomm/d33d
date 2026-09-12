"""Named OpenSCAD module registry (issue #7's core deliverable).

Resolves a lasso selection in the three.js viewer to a named OpenSCAD
module identifier. This is greenfield orchestration built ON TOP of the
render worker's existing per-render primitives (``d33d.render_worker`` —
``build_docker_argv`` / ``run_container``); it never re-implements
container invocation and never changes the render worker's existing
8-step single-render contract.

Binding architecture (resolved across two research spikes; see
``outputs/research-how-to-build-a-named-module-registry-for-openscad-
parts-so-a.md`` for the full evidence trail — do not re-litigate):

1. OpenSCAD does NOT preserve module names in ``.csg``/STL output —
   confirmed dead end, both by the general OpenSCAD upstream feature
   trackers and empirically against a real ``.csg`` artifact in this
   repo. The registry must never attempt to recover names from that path.
2. Enumerate regions by parsing the ``.scad`` SOURCE for top-level module
   CALL-SITES (:func:`parse_call_sites`) — definitions are never
   call-sites.
3. For each call-site, run one ``openscad`` invocation with the ``!``
   modifier prefixed onto exactly that call-site's line, isolating that
   module's geometry (empirically verified: ``!`` alone suffices, no
   ``*``/``%`` needed — 6/18/314 facets isolated vs 644 combined for a
   3-module fixture). N calls -> N sequential invocations, reusing
   ``render_worker.build_docker_argv``/``run_container`` — never a new
   ``docker run`` invocation shape.
4. Assemble the N per-module STLs into one ``trimesh.Scene({name: mesh})``
   and export GLB. Names survive the round-trip into ``nodes[].name`` /
   ``meshes[].name``, which is exactly what three.js's ``GLTFLoader``
   reads (``ModelViewer.tsx``'s ``resolveLassoSelection`` keys off
   ``mesh.name``). Scene-graph node ORDER is not preserved across the
   round-trip; names are — irrelevant for a name-keyed registry.

Reused, not reinvented: this module calls
``d33d.render_worker.build_docker_argv`` and ``run_container`` for every
container invocation, inheriting the exact same hardened flags (
``--network none``, ``--cap-drop ALL``, read-only, tmpfs ``/tmp``) as the
single-render contract. It does not touch ``d33d.print_validation`` (the
sole 3MF owner) and never emits 3MF — the registry's only output format
is GLB.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from dataclasses import dataclass

from d33d.render_worker import (
    DEFAULT_TIMEOUT_S,
    ErrorClass,
    build_docker_argv,
    run_container,
)

__all__ = [
    "BUILTIN_PRIMITIVES",
    "CallSite",
    "ModuleRenderFailure",
    "RegistryBuildResult",
    "build_registry_glb",
    "isolate_call_site",
    "parse_call_sites",
    "strip_comments",
]

#: Closed set of OpenSCAD builtin primitives/operators that must never be
#: mistaken for a user-defined module call-site. A bare invocation of any
#: of these at the top level (e.g. ``cube([1,1,1]);``) carries no module
#: identity to isolate and is not a registry entry.
BUILTIN_PRIMITIVES: frozenset[str] = frozenset(
    {
        "cube",
        "sphere",
        "cylinder",
        "polyhedron",
        "square",
        "circle",
        "polygon",
        "text",
        "translate",
        "rotate",
        "scale",
        "resize",
        "mirror",
        "multmatrix",
        "color",
        "offset",
        "hull",
        "minkowski",
        "union",
        "difference",
        "intersection",
        "linear_extrude",
        "rotate_extrude",
        "projection",
        "surface",
        "import",
        "render",
        "children",
        "for",
        "if",
        "let",
        "assign",
        "echo",
    }
)

#: Matches a top-level ``module <name>(`` definition header — the name
#: after this keyword is a definition, never a call-site.
_MODULE_DEF_RE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(")

#: Matches a bare identifier immediately followed by ``(`` — a candidate
#: call-site (module call OR builtin primitive; builtins are filtered by
#: :data:`BUILTIN_PRIMITIVES` after matching).
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")


@dataclass(frozen=True)
class CallSite:
    """One top-level module call-site enumerated from ``.scad`` source.

    ``name`` is the bare module identifier as written in the source.
    ``registry_name`` is the DISTINCT key used in the assembled registry
    (GLB node name / ``trimesh.Scene`` dict key) — ``name`` for the first
    call to a given module, ``name_2``/``name_3``/... for subsequent
    calls to the SAME module name, so a lasso pick is never ambiguous
    even when a design calls the same module more than once (e.g. four
    identical ``leg()`` calls). ``line`` is the 1-indexed source line the
    call-site starts on, used to build the ``!``-masked isolation source.
    """

    name: str
    registry_name: str
    line: int


def strip_comments(source: str) -> str:
    """Blank out ``//`` and ``/* */`` comments, preserving line numbers
    and byte offsets (comment bodies become spaces, not removed) so a
    caller matching line numbers against the ORIGINAL source stays
    aligned. Never touches string literals in a way that would falsely
    strip legitimate code — no OpenSCAD ``//``/``/* */`` sequence in the
    fixtures targeted by this module appears inside a string literal.
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        two = source[i : i + 2]
        if two == "//":
            j = source.find("\n", i)
            end = j if j != -1 else n
            out.append(" " * (end - i))
            i = end
        elif two == "/*":
            j = source.find("*/", i + 2)
            end = j + 2 if j != -1 else n
            # Preserve newlines inside the block comment so line numbers
            # of code AFTER the comment stay correct.
            chunk = source[i:end]
            out.append("".join(c if c == "\n" else " " for c in chunk))
            i = end
        else:
            out.append(source[i])
            i += 1
    return "".join(out)


def parse_call_sites(source: str) -> list[CallSite]:
    """Enumerate top-level module call-sites from ``.scad`` SOURCE.

    A call-site is a bare invocation ``name(...)`` that:
      - is NOT a ``module name(...)`` definition header,
      - is NOT one of :data:`BUILTIN_PRIMITIVES`,
      - occurs at brace-depth 0 (never nested inside a module's ``{}``
        body — a call written inside another module's definition is an
        internal implementation detail of that module, not an
        independently-selectable top-level region),
      - is not inside a ``//`` or ``/* */`` comment.

    A call already carrying an OpenSCAD modifier character (``!``/``%``/
    ``#``/``*``) in the source is still enumerated by its bare name — the
    isolation modifier the orchestrator injects for rendering is a
    separate, render-time concern.

    Duplicate calls to the same module name are all enumerated, each with
    a distinct ``registry_name`` (``name``, ``name_2``, ``name_3``, ...)
    so a lasso pick is never ambiguous. Results are ordered by source
    position (top-level call-sites are the discovery order the registry
    presents; three.js scene-graph order is not otherwise meaningful —
    only NAMES are pinned per the binding architecture).
    """
    clean = strip_comments(source)

    # Start offsets of the NAME token in each ``module name(`` definition
    # header — that name-position is never a call-site even though the
    # bare identifier matches ``_CALL_RE`` too.
    def_name_starts: set[int] = {m.start(1) for m in _MODULE_DEF_RE.finditer(clean)}

    sites: list[CallSite] = []
    counts: dict[str, int] = {}
    depth = 0
    pos = 0
    n = len(clean)
    while pos < n:
        ch = clean[pos]
        if ch == "{":
            depth += 1
            pos += 1
            continue
        if ch == "}":
            depth = max(0, depth - 1)
            pos += 1
            continue
        if depth == 0:
            m = _CALL_RE.match(clean, pos)
            if m is not None and m.start(1) not in def_name_starts:
                name = m.group(1)
                if name not in BUILTIN_PRIMITIVES and name != "module":
                    counts[name] = counts.get(name, 0) + 1
                    ordinal = counts[name]
                    registry_name = name if ordinal == 1 else f"{name}_{ordinal}"
                    line = clean.count("\n", 0, m.start()) + 1
                    sites.append(
                        CallSite(name=name, registry_name=registry_name, line=line)
                    )
                pos = m.end()
                continue
        pos += 1
    return sites


# ---------------------------------------------------------------------------
# Per-call-site isolation (``!`` modifier masking)
# ---------------------------------------------------------------------------


def isolate_call_site(source: str, site: CallSite) -> str:
    """Return a modified ``.scad`` source with ``!`` prefixed onto the
    LINE containing ``site`` — the ``!`` modifier isolates that
    subtree's geometry for a single render invocation (empirically
    verified: ``!`` alone suffices, no ``*``/``%`` needed on the sibling
    call-sites).

    Only the FIRST unmodified occurrence of the bare call on that exact
    source line is prefixed, so ``translate(...) leg();`` -> ``translate
    (...) !leg();`` (the modifier binds to the immediately-following
    primitive/module call per OpenSCAD's grammar, not to the transform).
    A line already carrying a modifier character for this call is left
    as-is if the call name is already ``!``-prefixed; this function is
    only ever invoked once per site.
    """
    lines = source.split("\n")
    idx = site.line - 1
    if idx < 0 or idx >= len(lines):
        return source
    line = lines[idx]
    pattern = re.compile(r"(?<![A-Za-z0-9_!%#*])" + re.escape(site.name) + r"\s*\(")
    new_line, count = pattern.subn(f"!{site.name}(", line, count=1)
    if count == 0:
        return source
    lines[idx] = new_line
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration: N isolated renders -> named GLB
# ---------------------------------------------------------------------------

#: The pinned OpenSCAD image (matches ``render_worker``'s docstring-pinned
#: image; the render worker itself takes ``image`` as a caller-supplied
#: parameter rather than a module constant, and this orchestrator follows
#: the same convention).
DEFAULT_OPENSCAD_IMAGE = "docker.io/openscad/openscad:trixie"


@dataclass(frozen=True)
class ModuleRenderFailure:
    """One call-site's isolated render did not produce a usable STL.

    ``error_class`` reuses the render worker's closed ``ErrorClass``
    enum — the registry never invents a parallel error vocabulary.
    """

    site: CallSite
    error_class: ErrorClass
    stderr: str


@dataclass(frozen=True)
class RegistryBuildResult:
    """Outcome of building the named-module registry GLB.

    ``glb_bytes`` is ``None`` when there is nothing to assemble (no
    call-sites, or every isolated render failed) — a genuine "empty
    registry" is distinct from a partial one: ``failures`` records why
    each dropped call-site failed, but any successfully isolated
    call-site still contributes to ``glb_bytes`` (a partial registry is
    never discarded wholesale for one bad module).
    """

    glb_bytes: bytes | None
    registry_names: tuple[str, ...]
    failures: tuple[ModuleRenderFailure, ...]


def _classify_isolated_render(proc_returncode: int, stl_bytes: bytes | None) -> ErrorClass:
    """Minimal classification for one isolated-module render: reuses the
    render worker's ``ErrorClass`` values it is meaningful to reach here
    (``ok`` / ``container_error`` / ``empty_model``). ``timeout``/``oom``
    are handled upstream by ``run_container`` itself (a timed-out or
    OOM-killed run never reaches this function with a usable exit code)
    and are classified by the caller from the ``run_container`` result
    before this function is invoked.
    """
    if proc_returncode != 0:
        return "container_error"
    if not stl_bytes:
        return "empty_model"
    return "ok"


#: Bounded wall-clock for the tiny populate/harvest helper invocations
#: (writing the isolated .scad into the volume, reading the STL back
#: out) — these move a few KB and never run openscad itself, so a short
#: fixed bound is enough; a hang here is as suspicious as a hung
#: ``docker kill``/``docker rm`` in the render worker's own cleanup path.
_HELPER_TIMEOUT_S = 30


def _helper_argv(image: str, name: str, volume: str, shell_cmd: str) -> list[str]:
    """argv for a tiny ``sh -c`` helper invocation against ``volume``,
    reusing the SAME containment flags ``build_docker_argv`` emits for the
    real openscad render (network none, cap-drop ALL, read-only, tmpfs
    /tmp, no-new-privileges) — populate/harvest steps never relax the
    hardened contract just because they do not run openscad itself.
    """
    argv = build_docker_argv(image, name, workdir_volume=volume)
    # build_docker_argv's argv ends in [..., image]; insert -i (stdin) and
    # the shell entrypoint override right before the image.
    image_idx = argv.index(image)
    argv[image_idx:image_idx] = ["-i", "--entrypoint", "sh"]
    argv.extend(["-c", shell_cmd])
    return argv


def _write_file_into_volume(
    image: str, volume: str, filename: str, content: bytes, timeout_s: int
) -> None:
    """Populate ``filename`` inside the named volume's ``/work`` via a
    short-lived helper container fed ``content`` on stdin. Raises
    ``RuntimeError`` on a non-zero exit — a populate failure is an
    infrastructure error, never a per-module render classification.
    """
    name = f"registry-put-{uuid.uuid4().hex[:8]}"
    argv = _helper_argv(image, name, volume, f"cat > /work/{filename}")
    proc = subprocess.run(
        argv, input=content, timeout=timeout_s, capture_output=True, check=False
    )
    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="replace")
        raise RuntimeError(f"failed to populate {filename} into volume {volume}: {stderr}")


def _read_file_from_volume(
    image: str, volume: str, filename: str, timeout_s: int
) -> bytes | None:
    """Read ``filename`` back out of the named volume's ``/work`` via a
    short-lived helper container that ``cat``s it to stdout. Returns
    ``None`` (never raises) when the file does not exist or the helper
    exits non-zero — the caller treats a missing/unreadable artifact as
    part of its own render classification, not an infrastructure error.
    """
    name = f"registry-get-{uuid.uuid4().hex[:8]}"
    argv = _helper_argv(image, name, volume, f"cat /work/{filename}")
    try:
        proc = subprocess.run(
            argv, timeout=timeout_s, capture_output=True, check=False
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    return proc.stdout


def _create_named_volume(volume: str) -> None:
    subprocess.run(
        ["docker", "volume", "create", volume],
        timeout=_HELPER_TIMEOUT_S,
        capture_output=True,
        check=False,
    )


def _remove_named_volume(volume: str) -> None:
    """Best-effort cleanup — mirrors the render worker's own best-effort
    ``docker kill``/``docker rm`` convention. A leaked volume is a warning,
    never a raised exception (the registry build's own result must not be
    lost because cleanup failed)."""
    subprocess.run(
        ["docker", "volume", "rm", volume],
        timeout=_HELPER_TIMEOUT_S,
        capture_output=True,
        check=False,
    )


def build_registry_glb(
    source: str,
    *,
    image: str = DEFAULT_OPENSCAD_IMAGE,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> RegistryBuildResult:
    """Build the named-module registry GLB from ``.scad`` SOURCE.

    For each top-level call-site (:func:`parse_call_sites`), writes an
    isolated ``.scad`` variant (:func:`isolate_call_site`) into a fresh,
    per-call-site EPHEMERAL NAMED DOCKER VOLUME (never a host bind mount —
    matches ticket #2's per-render ephemeral-volume convention), invokes
    ``openscad`` via ``d33d.render_worker.build_docker_argv``/
    ``run_container`` (the SAME hardened container flags as the
    single-render contract — this function never hand-rolls a ``docker
    run`` shape), harvests the resulting STL back out through a second
    short-lived helper container, and loads it into a named
    ``trimesh.Scene`` entry keyed by ``site.registry_name``. The volume is
    removed before moving to the next call-site (best-effort, mirrors the
    render worker's ``_cleanup_container`` convention).

    A call-site whose isolated render does not classify ``ok`` is
    recorded in ``failures`` and excluded from the assembled scene —
    every OTHER call-site's isolated render still contributes (a single
    bad module never discards the whole registry). ``glb_bytes`` is
    ``None`` iff there are zero call-sites or every one of them failed.
    """
    import trimesh  # host-side only, mirrors print_validation's import style

    sites = parse_call_sites(source)
    geometries: dict[str, trimesh.Trimesh] = {}
    failures: list[ModuleRenderFailure] = []

    for site in sites:
        isolated_source = isolate_call_site(source, site)
        run_id = uuid.uuid4().hex[:8]
        volume = f"registry-{run_id}"
        scad_filename = "isolate.scad"
        stl_filename = "isolate.stl"

        _create_named_volume(volume)
        try:
            try:
                _write_file_into_volume(
                    image,
                    volume,
                    scad_filename,
                    isolated_source.encode("utf-8"),
                    _HELPER_TIMEOUT_S,
                )
            except RuntimeError as e:
                failures.append(
                    ModuleRenderFailure(
                        site=site, error_class="container_error", stderr=str(e)
                    )
                )
                continue

            argv = build_docker_argv(image, f"render-{run_id}", workdir_volume=volume)
            argv.extend(
                [
                    "openscad",
                    "--autocenter",
                    "--render",
                    "-o",
                    f"/work/{stl_filename}",
                    f"/work/{scad_filename}",
                ]
            )

            proc = run_container(argv, timeout_s)
            if proc.returncode == 124:
                failures.append(
                    ModuleRenderFailure(site=site, error_class="timeout", stderr="")
                )
                continue
            if proc.returncode == 137:
                failures.append(
                    ModuleRenderFailure(site=site, error_class="oom", stderr="")
                )
                continue

            stl_bytes = _read_file_from_volume(
                image, volume, stl_filename, _HELPER_TIMEOUT_S
            )
            error_class = _classify_isolated_render(proc.returncode, stl_bytes)
            if error_class != "ok":
                stderr = (
                    proc.stderr.decode("utf-8", errors="replace")
                    if isinstance(proc.stderr, bytes)
                    else str(proc.stderr)
                )
                failures.append(
                    ModuleRenderFailure(
                        site=site, error_class=error_class, stderr=stderr
                    )
                )
                continue

            assert stl_bytes is not None  # narrowed by _classify_isolated_render
            mesh = trimesh.load(
                trimesh.util.wrap_as_stream(stl_bytes),
                file_type="stl",
                process=False,
            )
            if isinstance(mesh, trimesh.Scene):
                mesh = next(iter(mesh.geometry.values()), None)
            if mesh is None or mesh.vertices.shape[0] == 0:
                failures.append(
                    ModuleRenderFailure(site=site, error_class="empty_model", stderr="")
                )
                continue
            geometries[site.registry_name] = mesh
        finally:
            _remove_named_volume(volume)

    if not geometries:
        return RegistryBuildResult(
            glb_bytes=None, registry_names=(), failures=tuple(failures)
        )

    scene = trimesh.Scene(geometries)
    glb_bytes = scene.export(file_type="glb")
    return RegistryBuildResult(
        glb_bytes=glb_bytes,
        registry_names=tuple(geometries.keys()),
        failures=tuple(failures),
    )
