"""Named OpenSCAD module registry (issue #7's core deliverable).

Resolves a region pick in the three.js viewer to a named OpenSCAD
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
   reads (``ModelViewer.tsx``'s ``resolvePointPick`` keys off
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
from typing import TYPE_CHECKING

from trimesh.exchange.stl import HeaderError as _StlHeaderError

if TYPE_CHECKING:
    import trimesh

from d33d.render_worker import (
    DEFAULT_TIMEOUT_S,
    ErrorClass,
    _cleanup_container,
    build_docker_argv,
    run_container,
)

__all__ = [
    "BUILTIN_PRIMITIVES",
    "MAX_CALL_SITES",
    "CallSite",
    "ModuleRenderFailure",
    "RegistryBuildResult",
    "TooManyCallSitesError",
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
    calls to the SAME module name, so a region pick is never ambiguous
    even when a design calls the same module more than once (e.g. four
    identical ``leg()`` calls). Ordinal suffixes are chosen disjoint from
    every LITERAL module name found anywhere else in the source (not just
    previously-assigned registry_names), so a module named e.g. ``leg_2``
    can never collide with the second ``leg()`` call's auto-generated
    name. ``line`` is the 1-indexed source line the call-site starts on.
    ``col`` is the 0-indexed character offset of the call's name token
    WITHIN that line, so :func:`isolate_call_site` can mask the exact
    occurrence intended even when two calls to the same module share one
    source line (e.g. ``leg(); leg();``).
    """

    name: str
    registry_name: str
    line: int
    col: int = 0


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
    so a region pick is never ambiguous. The ordinal suffix is chosen
    disjoint from every LITERAL module identifier that appears anywhere
    else in the source (both other call-sites' bare ``name`` and
    ``module name(...)`` definition headers) — never just the set of
    registry_names already assigned — so an auto-generated ``leg_2``
    can never silently collide with a REAL module also named ``leg_2``.
    Results are ordered by source position (top-level call-sites are the
    discovery order the registry presents; three.js scene-graph order is
    not otherwise meaningful — only NAMES are pinned per the binding
    architecture).
    """
    clean = strip_comments(source)

    # Start offsets of the NAME token in each ``module name(`` definition
    # header — that name-position is never a call-site even though the
    # bare identifier matches ``_CALL_RE`` too.
    def_name_starts: set[int] = {m.start(1) for m in _MODULE_DEF_RE.finditer(clean)}

    # Every literal identifier that could plausibly appear as a
    # registry_name collision target: all ``module name(...)`` header
    # names, plus every bare call-site name (builtins excluded — they
    # never become registry entries so they can never collide with one).
    literal_names: set[str] = {m.group(1) for m in _MODULE_DEF_RE.finditer(clean)}
    for m in _CALL_RE.finditer(clean):
        if m.start(1) in def_name_starts:
            continue
        candidate = m.group(1)
        if candidate not in BUILTIN_PRIMITIVES and candidate != "module":
            literal_names.add(candidate)

    # Per-base-name "next ordinal to try" cursor. Regression: adversarial
    # review round 2. A naive disambiguation loop that rescans forward
    # one ordinal at a time from EACH call's own count is O(N^2) when an
    # attacker supplies N call-sites to one module name plus N sacrificial
    # ``module m_k(){}`` definitions that force every ordinal to collide
    # (~4000 colliding call-sites took >1s of pure CPU; ~40000, reachable
    # within the route's 1MB body cap, would take minutes — all BEFORE
    # MAX_CALL_SITES is ever checked in build_registry_glb). The cursor
    # below is monotonically non-decreasing per base name across the
    # whole parse, so each call-site's search resumes where the LAST
    # collision for that name left off rather than restarting from
    # ``ordinal`` every time — amortized O(1) per call-site regardless of
    # how many literal names collide.
    next_ordinal: dict[str, int] = {}

    def _next_registry_name(name: str) -> str:
        ordinal = next_ordinal.get(name, 1)
        registry_name = name if ordinal == 1 else f"{name}_{ordinal}"
        while (registry_name in literal_names and registry_name != name) or (
            registry_name in used_registry_names
        ):
            ordinal += 1
            registry_name = f"{name}_{ordinal}"
        next_ordinal[name] = ordinal + 1
        return registry_name

    sites: list[CallSite] = []
    used_registry_names: set[str] = set()
    depth = 0
    pos = 0
    n = len(clean)
    # Running (1-indexed line, start-offset-of-current-line) cursor,
    # advanced incrementally as the scan crosses each ``\n`` — never
    # recomputed from position 0. Regression: adversarial-review round 2.
    # ``clean.count("\n", 0, m.start())``/``clean.rfind("\n", 0, ...)``
    # each re-scan the ENTIRE prefix of the source for every call-site,
    # which is O(N) per site and therefore O(N^2) total for N call-sites
    # — this was the actual dominant cost behind the quadratic-parse
    # finding (profiled: >90% of wall-clock at N=15000), not the ordinal
    # disambiguation loop above (which the cursor-based
    # ``_next_registry_name`` already made O(1) amortized). Tracking the
    # line/line-start incrementally makes the whole scan O(N) total.
    line = 1
    line_start = 0
    scan_pos = 0
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
                    registry_name = _next_registry_name(name)
                    used_registry_names.add(registry_name)
                    call_start = m.start(1)
                    while scan_pos < call_start:
                        if clean[scan_pos] == "\n":
                            line += 1
                            line_start = scan_pos + 1
                        scan_pos += 1
                    col = call_start - line_start
                    sites.append(
                        CallSite(
                            name=name,
                            registry_name=registry_name,
                            line=line,
                            col=col,
                        )
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
    EXACT call-site identified by ``site.line``/``site.col`` — the ``!``
    modifier isolates that subtree's geometry for a single render
    invocation (empirically verified: ``!`` alone suffices, no ``*``/``%``
    needed on the sibling call-sites).

    Targeting is POSITIONAL (``site.col``, the 0-indexed offset of the
    call's name token within its line), not "first occurrence of this
    name on the line" — required so that two calls to the SAME module on
    one source line (``leg(); leg();``) each isolate their own distinct
    occurrence rather than both masking the first one. A line already
    carrying a modifier character immediately before the targeted name is
    left as-is (defensive; ``parse_call_sites`` does not enumerate
    already-``!``-prefixed calls as fresh sites in practice, but a
    direct caller passing a mismatched ``site`` must not corrupt the
    source under a different call).
    """
    lines = source.split("\n")
    idx = site.line - 1
    if idx < 0 or idx >= len(lines):
        return source
    line = lines[idx]
    col = site.col
    if col < 0 or col > len(line):
        return source
    if line[col : col + len(site.name)] != site.name:
        # The recorded column no longer matches this exact source line
        # (stale/mismatched CallSite) — refuse to guess at a substitute
        # occurrence, since masking the wrong call silently produces
        # wrong geometry under the caller's intended registry_name.
        return source
    if col > 0 and line[col - 1] in "!%#*":
        return source
    lines[idx] = line[:col] + "!" + line[col:]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Orchestration: N isolated renders -> named GLB
# ---------------------------------------------------------------------------

#: The pinned OpenSCAD image (matches ``render_worker``'s docstring-pinned
#: image; the render worker itself takes ``image`` as a caller-supplied
#: parameter rather than a module constant, and this orchestrator follows
#: the same convention).
DEFAULT_OPENSCAD_IMAGE = "docker.io/openscad/openscad:trixie"

#: Hard cap on the number of call-sites one ``build_registry_glb`` call
#: will orchestrate. Each call-site spawns a real sequential Docker
#: volume-create + populate-container + openscad-container + harvest-
#: container + volume-rm lifecycle; without a cap a small ``.scad``
#: SOURCE built from a repeated call pattern (e.g. ``"m();\n" * 100000``)
#: can enqueue an effectively unbounded amount of sequential Docker work
#: on the single render host. 64 comfortably covers any real assembly
#: (the golden-set fixtures never exceed a few dozen top-level modules)
#: while keeping worst-case wall-clock bounded. Per call-site, the worst
#: case is FIVE sequential subprocess round-trips, not one:
#: ``_create_named_volume`` (``_HELPER_TIMEOUT_S``) + ``_write_file_
#: into_volume`` (``_HELPER_TIMEOUT_S``) + the openscad render itself
#: (``DEFAULT_TIMEOUT_S``) + ``_read_file_from_volume`` (``_HELPER_
#: TIMEOUT_S``) + ``_remove_named_volume`` (``_HELPER_TIMEOUT_S``), i.e.
#: ``4 * _HELPER_TIMEOUT_S + DEFAULT_TIMEOUT_S`` per site, not just
#: ``DEFAULT_TIMEOUT_S``. So the true worst-case bound is
#: ``MAX_CALL_SITES * (4 * _HELPER_TIMEOUT_S + DEFAULT_TIMEOUT_S)``
#: (with the defaults below, ~4.3 hours), not the smaller
#: ``MAX_CALL_SITES * DEFAULT_TIMEOUT_S`` a per-site-single-timeout
#: reading would suggest. Because that bound is real wall-clock, not
#: just render-host load, the HTTP route
#: (``d33d.app.create_module_registry``) runs ``build_registry_glb`` via
#: ``asyncio.to_thread`` rather than awaiting it directly, so a slow
#: build cannot stall the process's event loop for other requests. A
#: source with more call-sites than this cap is rejected via
#: ``TooManyCallSitesError`` before any Docker invocation — never
#: silently truncated, so an over-long registry is surfaced to the
#: caller instead of quietly dropping modules.
MAX_CALL_SITES = 64


class TooManyCallSitesError(ValueError):
    """Raised by :func:`build_registry_glb` when ``parse_call_sites``
    enumerates more than :data:`MAX_CALL_SITES` call-sites, before any
    Docker volume/container is created. Callers (the HTTP route) map
    this to a 413/422 response rather than letting the request tie up
    the render host for ``O(N * timeout_s)``.
    """

    def __init__(self, count: int) -> None:
        self.count = count
        super().__init__(
            f"{count} call-sites exceeds the {MAX_CALL_SITES} limit per registry build"
        )


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


class HelperTimeoutError(RuntimeError):
    """Raised by :func:`_write_file_into_volume` when the populate helper
    container does not exit within its bounded timeout. Distinct from the
    generic ``RuntimeError`` a non-zero exit raises so the caller can
    classify this as ``timeout`` rather than ``container_error``.
    """


def _write_file_into_volume(
    image: str, volume: str, filename: str, content: bytes, timeout_s: int
) -> None:
    """Populate ``filename`` inside the named volume's ``/work`` via a
    short-lived helper container fed ``content`` on stdin. Raises
    ``RuntimeError`` on a non-zero exit, or :class:`HelperTimeoutError` if
    the helper container does not exit within ``timeout_s`` — a populate
    failure is an infrastructure error, never a per-module render
    classification.

    On timeout, mirrors ``render_worker.run_container``'s own cleanup
    contract: the helper container was started without ``--rm`` (by
    ``build_docker_argv``, so ``run_container`` can ``docker inspect`` an
    OOM-killed run elsewhere), so nothing else would ever kill/remove a
    hung populate container — this reuses ``render_worker._cleanup_container``
    directly rather than leaving it leaked on the Docker host.
    """
    name = f"registry-put-{uuid.uuid4().hex[:8]}"
    argv = _helper_argv(image, name, volume, f"cat > /work/{filename}")
    try:
        proc = subprocess.run(
            argv, input=content, timeout=timeout_s, capture_output=True, check=False
        )
    except subprocess.TimeoutExpired:
        _cleanup_container(name)
        raise HelperTimeoutError(
            f"populating {filename} into volume {volume} timed out after {timeout_s}s"
        ) from None
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
    """Best-effort volume create. A hang here is lower-severity than a
    hung populate/harvest CONTAINER (no long-lived container is started
    by ``docker volume create``), but a bare ``subprocess.TimeoutExpired``
    would still propagate uncaught and abort the whole registry build on
    a stalled Docker daemon — caught here for the same reason ``_write_
    file_into_volume``/``_read_file_from_volume`` catch it: a hung helper
    invocation must never discard every already-succeeded call-site.
    """
    try:
        subprocess.run(
            ["docker", "volume", "create", volume],
            timeout=_HELPER_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass


def _remove_named_volume(volume: str) -> None:
    """Best-effort cleanup — mirrors the render worker's own best-effort
    ``docker kill``/``docker rm`` convention. A leaked volume is a warning,
    never a raised exception (the registry build's own result must not be
    lost because cleanup failed). Runs from a ``finally`` block in
    ``build_registry_glb``, so a bare ``subprocess.TimeoutExpired`` here
    must not propagate either — it would replace whatever exception (or
    successful return) was already in flight."""
    try:
        subprocess.run(
            ["docker", "volume", "rm", volume],
            timeout=_HELPER_TIMEOUT_S,
            capture_output=True,
            check=False,
        )
    except subprocess.TimeoutExpired:
        pass


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

    Image staleness (issue #236): the render-worker image's pre-launch
    ``_verify_render_worker_image`` guard is NOT applied here yet — this
    function takes ``image`` as a caller parameter (default
    ``DEFAULT_OPENSCAD_IMAGE``, not ``RENDER_WORKER_IMAGE``) and launches
    N sequential containers per call-site, so there is no single clean
    pre-launch gate point whose failure maps onto the per-site
    ``ModuleRenderFailure`` path. Follow-up: gate the loop entry with the
    same semantics (verified-mismatch RuntimeError -> loud failure with
    the canonical rebuild command; docker-query failure -> warn and
    proceed).

    A call-site whose isolated render does not classify ``ok`` is
    recorded in ``failures`` and excluded from the assembled scene —
    every OTHER call-site's isolated render still contributes (a single
    bad module never discards the whole registry). ``glb_bytes`` is
    ``None`` iff there are zero call-sites or every one of them failed.

    Raises :class:`TooManyCallSitesError` if ``parse_call_sites`` finds
    more than :data:`MAX_CALL_SITES` entries — checked BEFORE any Docker
    volume/container is created, so a hostile ``source`` cannot enqueue
    unbounded sequential container work.
    """
    import trimesh  # host-side only, mirrors print_validation's import style

    sites = parse_call_sites(source)
    if len(sites) > MAX_CALL_SITES:
        raise TooManyCallSitesError(len(sites))
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
            except HelperTimeoutError as e:
                failures.append(
                    ModuleRenderFailure(
                        site=site, error_class="timeout", stderr=str(e)
                    )
                )
                continue
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
            try:
                mesh = trimesh.load(
                    trimesh.util.wrap_as_stream(stl_bytes),
                    file_type="stl",
                    process=False,
                )
            except (ValueError, OSError, _StlHeaderError) as e:
                # Mirrors d33d.print_validation's own trimesh.load catch
                # (ValueError for parse failures / "not a file", OSError
                # for read failures) plus trimesh's own STL-specific
                # HeaderError (a bare Exception subclass, so ValueError
                # alone would miss it) — the full surface trimesh.load
                # itself is documented and observed to raise for
                # malformed/truncated binary STL. Any of these is a
                # per-module artifact problem, never a whole-registry
                # abort. The harvested bytes are attacker-influenced (an
                # OpenSCAD render driven by untrusted .scad source), so a
                # malformed/unparseable STL must classify and let every
                # OTHER call-site's already-succeeded render still stand.
                failures.append(
                    ModuleRenderFailure(
                        site=site, error_class="artifact_error", stderr=str(e)
                    )
                )
                continue
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

    site_by_registry_name = {site.registry_name: site for site in sites}
    glb_bytes, exported_names, export_failures = _export_scene_isolating_bad_meshes(
        geometries, site_by_registry_name
    )
    failures.extend(export_failures)
    return RegistryBuildResult(
        glb_bytes=glb_bytes,
        registry_names=exported_names,
        failures=tuple(failures),
    )


def _export_scene_isolating_bad_meshes(
    geometries: dict[str, trimesh.Trimesh],
    site_by_registry_name: dict[str, CallSite],
) -> tuple[bytes | None, tuple[str, ...], list[ModuleRenderFailure]]:
    """Export ``geometries`` as one named GLB scene, isolating and
    excluding whichever individual mesh (if any) trips ``scene.export``.

    Regression: adversarial-review round 2. Every harvested mesh here
    came from an OpenSCAD render of untrusted, LLM-generated ``.scad``
    source — the exact same attacker-influenced-data threat model that
    ``trimesh.load`` is explicitly guarded against a few lines earlier in
    ``build_registry_glb``. ``trimesh.Scene.export`` operates on that
    same data and previously had no equivalent protection: an export
    failure propagated unhandled, discarding every OTHER call-site's
    already-succeeded render and surfacing as an unclassified 500 from
    the route — contradicting this module's own "a single bad module
    never discards the whole registry" guarantee.

    On a whole-scene export failure, isolates the poisoned mesh by
    exporting each geometry individually (the same ``(ValueError, OSError,
    _StlHeaderError)`` classes already trusted for ``trimesh.load``),
    classifies every mesh that fails alone as ``artifact_error``, and
    retries the scene export with only the survivors. This never
    recurses arbitrarily deep — one bisection pass is enough since each
    remaining failure, if any, is isolated by the same per-mesh probe.
    """
    import trimesh  # host-side only, matches build_registry_glb's import style

    try:
        glb_bytes = trimesh.Scene(geometries).export(file_type="glb")
        return bytes(glb_bytes), tuple(geometries.keys()), []
    except (ValueError, OSError, _StlHeaderError) as e:
        whole_scene_error = str(e)

    failures: list[ModuleRenderFailure] = []
    survivors: dict[str, trimesh.Trimesh] = {}
    for registry_name, mesh in geometries.items():
        try:
            trimesh.Scene({registry_name: mesh}).export(file_type="glb")
        except (ValueError, OSError, _StlHeaderError) as e:
            site = site_by_registry_name.get(registry_name)
            if site is not None:
                failures.append(
                    ModuleRenderFailure(
                        site=site, error_class="artifact_error", stderr=str(e)
                    )
                )
            continue
        survivors[registry_name] = mesh

    if not survivors:
        return None, (), failures

    # The per-mesh probe above already proved every survivor exports
    # alone; re-raising here would mean the ORIGINAL whole-scene failure
    # was caused by something other than any single mesh (e.g. a
    # combined-scene-only edge case) — surface that distinctly rather
    # than silently returning an empty registry.
    try:
        glb_bytes = trimesh.Scene(survivors).export(file_type="glb")
    except (ValueError, OSError, _StlHeaderError) as e:
        raise RuntimeError(
            "scene export failed even after isolating every individually-"
            f"exportable mesh (original error: {whole_scene_error}): {e}"
        ) from e
    return bytes(glb_bytes), tuple(survivors.keys()), failures
