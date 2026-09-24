"""Design-loop background-task adapter for the chat wire (issue #54).

The single place that runs ``app.state.run_design_loop`` for a chat
message and adapts its outcome onto the SSE frame contract that
``d33d.streaming._stream_events`` (``GET /api/stream/{project_id}``) and
the SPA's ``ApiClient.streamEvents`` expect:

- ``("progress", {step, iteration?})`` for each design-loop stage
- ``("token", {text})`` — exactly ONE token frame per completed loop
- ``("progress", {step: "version-created", version_id})`` on a pass
- ``("done", {message})`` on completion
- ``("error", {message, reason?})`` on exhaustion (``reason`` = the
  structured ``failure_reason``, omitted when absent) or infra failure
  (no ``reason`` — there is no ``DesignResult`` to read one from)

The adapter is a plain async generator (never a coroutine):
``event_sources[project_id]`` stores the generator object, so the SSE
endpoint's ``async for`` iterates it directly. It catches broadly
(:func:`run_design_loop_with_events`) — an unhandled exception here would
escape the generator, kill the SSE stream mid-turn, and (with
``streaming.py``'s historical narrow catch) leave the client hanging with
no terminal frame. Every exit path therefore ends in a terminal
``done``|``error`` frame, and the in-flight flag (``app.state
.design_loop_inflight``) is released in a ``finally`` that covers pass,
exhausted, and exception alike.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

from d33d.design_loop import BboxInfo, best_match_component, scad_title
from d33d.render_worker import VIEWS, RenderResult

logger = logging.getLogger(__name__)

#: Fixed 1x1 transparent-PNG data URI — the fallback when a project has no
#: stored photo or the stored file is missing out-of-band. A design-loop
#: ``photo`` must be a data URI/URL string (``None`` would crash the LLM
#: message builder), so the constant is never replaced with ``None``.
#: (A 1x1 fully transparent PNG has exactly one distinct colour, which is
#: why the base64 decodes to a valid, non-empty image.)
EMPTY_PHOTO_DATA_URI = (
    "data:image/png;base64,"
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNkYAAAAAYAAjCB0C8AAAAA"
    "SUVORK5CYII="
)

#: Total wall-clock deadline for ONE design-loop run, in seconds
#: (issue #221). Measured from the start of the adapter generator (the
#: start of the ``to_thread`` task), NOT per-frame or idle time: the
#: guarantee is "the loop does not complete within a bounded time",
#: matching the render worker's own bounded-subprocess model (``
#: render_worker.run_container``'s 120s timeout) one level up. A stream
#: that keeps emitting liveness frames (per-view progress, LLM tokens)
#: while the loop itself never terminates MUST still be cut off, so the
#: deadline is total, not idle. Must be read as a module-level constant
#: inside the wait loop (so tests can ``monkeypatch.setattr`` it to a
#: small value — the same pattern as ``versions_routes
#: ._DRAIN_TIMEOUT_SECONDS``), never inlined.
#: The client-side ``STREAM_TOTAL_TIMEOUT_MS`` (``web/src/lib/api.ts``)
#: must exceed this with margin (240s > 180s) so the server's structured
#: ``design_loop_timed_out`` frame normally arrives first.
DESIGN_LOOP_TIMEOUT_SECONDS = 180.0

#: The distinct structured reason code for a deadline-triggered terminal
#: error frame. Deliberately NOT the render-worker's ``"timeout"``
#: ``ErrorClass`` (``render_worker.py``'s 120s subprocess timeout) — the
#: SPA maps each to its own ``copy.failure.reasons`` entry, and reusing
#: ``"timeout"`` would show render-worker copy for a whole-loop stall
#: (issue #221).
DESIGN_LOOP_TIMED_OUT_REASON = "design_loop_timed_out"

#: MIME by file extension — the upload route (``d33d.projects.upload_photo``)
#: constrains photos to image/png and image/jpeg, so the mapping is total.
_PHOTO_MIME_BY_SUFFIX: dict[str, str] = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
}


def photo_data_uri(source_photo_path: str | None) -> str:
    """The project's stored photo as a data URI (MIME from the extension).

    ``None`` / missing file → :data:`EMPTY_PHOTO_DATA_URI` (never ``None`` —
    the design loop's LLM message builder requires a data URI/URL string).
    """
    if not source_photo_path:
        return EMPTY_PHOTO_DATA_URI
    p = Path(source_photo_path)
    if not p.is_file():
        return EMPTY_PHOTO_DATA_URI
    mime = _PHOTO_MIME_BY_SUFFIX.get(p.suffix.lower(), "image/png")
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{b64}"


def latest_version_stated_dims(
    versions_service: Any, project_id: int
) -> tuple[float, float, float] | None:
    """The latest version's (W, D, H) as a fully-positive triple, or
    ``None`` — ticket #91's fallback source (mirrors
    ``create_region_edit``'s latest-version W/D/H read, but returns ``None``
    instead of a zero triple when the dimensions are not KNOWN).

    ``None`` is returned when there is no version, or when any of W/D/H is
    missing/null or ``<= 0`` — an all-zero (or partial) triple must never
    re-enter the bbox gate as a ``target <= 0`` hard-fail target (the
    original #91 bug); the caller treats ``None`` as "no dimensions known"
    and the gate abstains (``Score.bbox_abstained``).

    ``create_region_edit`` does NOT use this helper — it builds its own
    ``float(params.get(axis, 0.0))`` triple, which on a fresh project
    (or a version whose W/D/H are missing/null/zero) is ``(0.0, 0.0, 0.0)``.
    Note: the bbox gate NOW ABSTAINS on such a triple (``_bbox_within_tolerance``
    returns True on a ``target <= 0`` axis, ticket #91) and records the
    abstention in ``Score.bbox_abstained`` — where that route previously
    hard-FAILED every candidate. The abstain is the correct semantics for
    region edits: it is the behavior that route's own comment ("the
    dimension gate measures rather than fabricates") was always intended
    to describe.
    """
    latest = versions_service.latest_version(project_id)
    if latest is None:
        return None
    params = latest.get("params") or {}
    triple: list[float] = []
    for axis in ("W", "D", "H"):
        try:
            value = float(params.get(axis, 0.0))
        except (TypeError, ValueError):
            return None
        triple.append(value)
    if any(value <= 0 for value in triple):
        return None
    return (triple[0], triple[1], triple[2])


def _component_extent(component: Any) -> tuple[float, float, float, float, float, float, float]:
    """``(x_extent, y_extent, z_extent, volume, min_x, min_y, min_z)`` from
    one trimesh component (issue #100). All values are plain ``float``
    (numpy scalars are converted). Volume defaults to 0.0 for a
    non-watertight component (trimesh raises on ``.volume`` for
    non-watertight meshes — the gate only consumes volume as a best-match
    tie-breaker, never as a conformance signal, so a missing volume is
    harmless)."""
    b = component.bounds
    extents = (
        float(b[1, 0] - b[0, 0]),
        float(b[1, 1] - b[0, 1]),
        float(b[1, 2] - b[0, 2]),
    )
    try:
        volume = float(getattr(component, "volume", 0.0) or 0.0)
    except (ValueError, TypeError, RuntimeError):  # non-watertight component
        volume = 0.0
    mins = (
        float(b[0, 0]),
        float(b[0, 1]),
        float(b[0, 2]),
    )
    return (*extents, volume, *mins)


def bbox_from_render(render: RenderResult) -> BboxInfo | None:
    """Per-axis extents (mm) for the design-loop bbox gate, from a render.

    The intended design: ``render_for_design_loop`` trimesh-loads the
    harvested ``model.stl`` and computes ``vertex_count`` / ``watertight`` /
    ``volume_mm3`` from that mesh, so the extents derive from the same
    on-volume STL — re-loaded host-side here from ``render.stl``.

    ``render.stl`` is re-pointed at the durable artifact directory when
    the render worker's issue #72 persistence succeeds, so a live path is
    the normal production case: a successful load yields real extents. A
    ``None`` return is the edge case — persistence disabled or failed, or
    the file externally deleted — where ``path.is_file()`` is ``False`` →
    the bbox gate cannot score (the gate then fails and no candidate can
    score the bbox bit).

    Issue #100 — multi-part meshes: the bbox gate needs the mesh's
    per-component extents (a stated single-body triple must match the
    best-matching component, not the whole-assembly bbox), so this seam
    runs the merge+split internally and carries the breakdown on
    ``BboxInfo.components`` (``x``/``y``/``z``/``volume`` keep their
    whole-assembly meaning):

    **CRITICAL: ``merge_vertices()`` MUST run before
    ``split(only_watertight=True)`` — do NOT "optimise" it away.**
    The production STLs OpenSCAD emits are FACE-DISCONNECTED (triangles
    share no vertices), so on an unmerged load ``split()`` finds ZERO
    watertight connected components (measured on
    ``tests/fixtures/stl/box_20mm.stl``: unmerged → 0 components; merged
    → exactly 1 for the single 20mm body). Without the merge, every
    multi-part render would split to zero and fail the gate on a broken
    mesh that renders fine — the exact silent-corruption class issue #84
    already cost days to remove. Merging is NOT a no-op for bounds (the
    old "bounds are merge-invariant" note below is true for BOUNDS and
    false for SPLIT, which is why it is now corrected) — it is what makes
    ``split`` see a shell as one connected component.

    A component breakdown the split cannot produce (empty) still returns
    the whole-assembly ``BboxInfo`` (empty ``components`` → the gate's
    legacy whole-part path). A zero-component split on a non-empty mesh is
    a genuinely broken mesh: the gate fails it (``bbox_out_of_tolerance``)
    — it is never a vacuous pass (issue #100, PM decision 2).

    Returns ``None`` (the bbox gate fails) when the render has no STL or it
    cannot be loaded; never raises.
    """
    stl = render.stl
    if not isinstance(stl, str) or not stl:
        return None
    path = Path(stl)
    if not path.is_file():
        return None
    try:
        import trimesh

        mesh = trimesh.load(str(path), process=False)
        if not hasattr(mesh, "bounds") or mesh.bounds is None:
            return None
        b = mesh.bounds
        x = float(b[1, 0] - b[0, 0])
        y = float(b[1, 1] - b[0, 1])
        z = float(b[1, 2] - b[0, 2])
        volume = float(getattr(mesh, "volume", 0.0) or 0.0)

        # Issue #100: per-component breakdown for the multi-part gate.
        # merge_vertices() BEFORE split() — see the CRITICAL note above;
        # without it the production face-disconnected STL splits to ZERO
        # components (the whole-part ``components=()`` path would then
        # compare the stated triple against the whole-assembly bbox and
        # every multi-part request would fail). ``merge_vertices()`` is
        # destructive (mutates in place and returns self), so it is
        # called on the loaded mesh, not assigned to a throwaway. It is
        # INSIDE the load try: a failure here is a load failure (the
        # outer ``except`` → None), not a split failure.
        mesh.merge_vertices()
        # Initialize before the try so the except branch can reference it.
        components: tuple[tuple[float, float, float, float, float, float, float], ...] = ()
        try:
            split_result = mesh.split(only_watertight=True)
            # ``split()`` returns a LIST of submeshes (one per watertight
            # connected component); on trimesh 5.1.0 a mesh with zero
            # watertight components yields an empty list (the zero-component
            # case the gate must fail, not pass). Defensive: an unexpected
            # non-list shape degrades to an empty breakdown (the legacy
            # whole-part path), never a raise.
            if isinstance(split_result, list):
                if not split_result:
                    # Zero components: the mesh is genuinely broken
                    # (non-watertight even after merge). Return None so
                    # the gate FAILS — a vacuous pass here would repeat
                    # the exact defect class issue #84 removed.
                    #
                    # This None is deliberately the SAME outcome as the
                    # outer load-failure None (the gate bit is False either
                    # way — a broken mesh never passes), but the two log
                    # lines below are the ONLY place the conditions are
                    # told apart: "mesh loaded OK but split found 0
                    # watertight components" (healthy geometry, broken
                    # topology — a split-level problem, greppable) vs the
                    # "failed to load STL" line (the file/mesh itself
                    # would not load). Do not merge the messages: this
                    # codebase has been bitten by two distinct conditions
                    # collapsing into one indistinguishable signal.
                    logger.error(
                        "bbox_fn: mesh loaded OK but split found 0 watertight "
                        "components for %r — the bbox gate will fail "
                        "(bbox_out_of_tolerance), not pass",
                        stl,
                    )
                    return None
                components = tuple(
                    _component_extent(comp)
                    for comp in split_result
                )
        except (AttributeError, ValueError, TypeError, RuntimeError):
            # A split failure on a mesh that LOADED and whose bounds were
            # measured is a healthy-geometry anomaly, NOT a load failure —
            # degrading to an empty breakdown (the legacy whole-part path)
            # would silently re-create the exact issue #100 defect for
            # every multi-part mesh (the stated triple compared against
            # the whole-assembly bbox). Returning None fails the gate
            # loudly, indistinguishable-from-a-broken-mesh at the wire
            # level but greppable via this log line (which names the
            # consequence and the trimesh version, so a trimesh upgrade
            # that changes split() behaviour is diagnosable in one grep).
            # The tuple is the known trimesh failure surface (attribute
            # error on a changed API, value/type error from a non-list
            # shape, runtime error from the geometry itself); MemoryError
            # (BaseException subclass — a killed process is the right
            # fate for OOM) and KeyboardInterrupt/SystemExit are not
            # caught, deliberately.
            logger.error(
                "bbox_fn: component split failed for %r (trimesh %s) — "
                "degrading to no breakdown means the bbox gate will fail; "
                "refusing to fall back to the whole-part comparison that "
                "issue #100 removed",
                stl,
                getattr(trimesh, "__version__", "unknown"),
            )
            return None
        return BboxInfo(x=x, y=y, z=z, volume=volume, components=components)
    except Exception:  # any load failure → gate fails (None), never a raise
        logger.exception("bbox_fn: failed to load STL %r", stl)
        return None


def _data_uri_from_bytes(raw: bytes, mime: str) -> str:
    """A base64 data URI from raw bytes and a MIME type."""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _artifact_bytes_from_path(render: RenderResult) -> tuple[bytes | None, dict[str, bytes]]:
    """Read the best render's STL + view bytes from its DURABLE artifact
    directory — ``render.render_artifact_dir`` (issue #72: the per-render
    directory the worker persists to INSIDE its
    ``tempfile.TemporaryDirectory`` with-block before the block exits;
    the directory survives past the tempdir teardown because the copy
    happened inside it).

    The durable path rides on the render object as a real attribute: the
    declared ``RenderResult`` field ``render_artifact_dir`` (the reader
    used to ``getattr`` a non-existent attribute name, which
    silently returned ``None`` on every real render — the bug this fix
    resolved). ``None`` for the field means "no durable source" — the
    frame omits the fields (never a bogus path, never a null, never a
    raise).

    The filenames are joined explicitly from the fixed VIEWS contract —
    never globbed: a directory that yields fewer than the 6 fixed VIEWS
    view PNGs (a failed copy, a missing file) is treated as "no views"
    (empty dict) — a consumer cannot distinguish a 5-of-6 map from a
    complete one, so the frame omits the field entirely rather than emit
    fewer than 6.
    """
    artifact_path = render.render_artifact_dir
    if not isinstance(artifact_path, str) or not artifact_path:
        return None, {}
    artifact_dir = Path(artifact_path)
    if not artifact_dir.is_dir():
        return None, {}
    stl_bytes = _read_artifact_bytes(str(artifact_dir / "model.stl"))
    view_bytes: dict[str, bytes] = {}
    for name, _cam in VIEWS:
        b = _read_artifact_bytes(str(artifact_dir / name))
        if b is not None:
            view_bytes[name] = b
    if len(view_bytes) != len(VIEWS):
        # Partial views → "no views" (the frame omits the field entirely).
        view_bytes = {}
    return stl_bytes, view_bytes


def _read_artifact_bytes(path: Any) -> bytes | None:
    """Raw bytes for a render artifact path, or ``None`` when unreadable
    (missing file, non-string path, or an ``OSError`` mid-read).
    """
    if not isinstance(path, str) or not path:
        return None
    p = Path(path)
    if not p.is_file():
        return None
    try:
        return p.read_bytes()
    except OSError:
        logger.exception("render artifact unreadable: %r", path)
        return None


def _suppress_cancellation(task: Any) -> None:
    """Best-effort cancel of a background task, swallowing the outcome.

    Deadline-triggered teardown (issue #221) cancels the ``to_thread``
    render task and the frame-queue consumer task; neither may leak an
    unhandled cancellation (a ``CancelledError`` that escapes ``.cancel()``
    would surface as an ``Exception ignored in Task`` warning — the client
    has already received the terminal frame, so the background work is
    unobservable by design: asyncio CANNOT kill a ``to_thread`` worker
    thread mid-flight, the terminal frame is the user-facing guarantee).
    """
    task.cancel()

    def _swallow_outcome(_t: Any) -> None:
        # ``to_thread`` tasks cannot be cancelled mid-flight — the worker
        # thread runs to completion (or until its own ``Event`` is
        # released). Swallow the eventual outcome so no unhandled
        # exception surfaces on the (closing) loop.
        #
        # On a task that was ``cancel()``-ed and has since reported
        # ``cancelled()`` True, ``.exception()`` is never reached — there
        # is no outcome to swallow. ``.exception()`` is only called when
        # ``cancelled()`` is False, i.e. the task RAN and raised: a
        # deadline-cancelled ``to_thread`` task that had already begun
        # propagating its cancellation can report ``cancelled()`` False
        # while still carrying a pending exception, and ``.exception()``
        # then RE-RAISES that exception from the done callback. That is
        # the only path that can raise here, so it is caught and logged
        # at debug level (the client already received the terminal frame;
        # the background outcome is unobservable by design — asyncio
        # CANNOT kill a ``to_thread`` worker thread mid-flight). Catching
        # the base exception type is deliberate: a ``CancelledError`` that
        # escapes a done callback would surface as an "Exception in
        # callback" traceback against a half-closed loop (measured: the
        # reproduction hangs the interpreter for the full ``asyncio.run``
        # shutdown timeout), so every outcome class must be swallowed.
        try:
            if not _t.cancelled():
                _t.exception()
        except BaseException:  # noqa: BLE001 — documented above
            logger.debug(
                "design-loop deadline teardown: swallowed a background "
                "task outcome that must not surface post-terminal-frame: %r",
                _t,
            )

    task.add_done_callback(_swallow_outcome)


def _result_message(result: Any) -> str:
    """The terminal ``done``/``error`` frame text for a loop result."""
    if getattr(result, "status", None) == "pass":
        return "Design loop passed validation"
    reason = getattr(result, "failure_reason", None)
    if isinstance(reason, str) and reason:
        return f"Design loop exhausted: {reason}"
    return "Design loop exhausted"


def _structured_reason(result: Any) -> str | None:
    """The loop's ``DesignResult.failure_reason`` as a plain string, or
    ``None`` — the SPA maps it to plain-language copy WITHOUT string-matching
    the free-text message (issue #82). The value is one of the four
    ``GATE_REASON_BITS`` or a render-worker ``ErrorClass``; ``None`` (absent
    from the frame) is the "no reason" case.
    """
    reason = getattr(result, "failure_reason", None)
    if isinstance(reason, str) and reason:
        return reason
    return None


def _loop_takes_app(run_loop: Any) -> bool:
    """The injected design-loop seam's signature check (production
    ``_build_production_design_loop`` takes ``(app, **kwargs)``; test
    stubs may take a subset — but the kwargs are ALWAYS forwarded,
    so a stub that consumes any of them (``on_progress`` in particular)
    must accept ``**kwargs`` or an ``app`` parameter; the
    app-parameter check only selects WHICH calling convention — see the
    call site in :func:`run_design_loop_with_events`)."""
    try:
        sig = inspect.signature(run_loop)
    except (TypeError, ValueError):
        return False
    return "app" in sig.parameters


def _run_in_loop(coro: Any) -> Any:
    """Drive ``coro`` to completion on a worker thread (``asyncio.run``).

    ``asyncio.to_thread`` schedules this function on the default executor (a
    worker thread with NO running event loop), so it ``asyncio.run``-s
    ``coro`` on a FRESH event loop. ``asyncio.run`` is required (not a bare
    ``await``) because the worker thread has no running loop to await
    against; ``asyncio.run`` installs that fresh loop and drives ``coro``.

    This is the spec's ``asyncio.to_thread`` requirement for the sync render:
    the multi-minute ``render_for_design_loop`` (Docker ``subprocess.run``)
    runs on the worker thread, NOT the app's event loop, so one slow render
    cannot stall other SSE streams or API handlers. The LLM's awaits run on
    the fresh loop (they still yield — the LLM is async) and do not block
    the app's event loop either, because the entire design loop runs here,
    off the app loop.

    ``coro`` MUST be the result of calling the injected loop (the adapter
    calls ``run_loop(...)`` before invoking this, so the loop's body runs
    here, not on the event loop). Exceptions from ``coro`` propagate out of
    ``asyncio.run`` and are caught by :func:`run_design_loop_with_events`'
    terminal-frame handler.
    """
    return asyncio.run(coro)


def _version_bbox_extents(result: Any) -> tuple[float, float, float] | None:
    """The best candidate's per-axis measured extents for persistence
    (issue #137), or ``None``.

    The version OWNS its measurement: the render that becomes the version
    is the BEST candidate (``result.best`` — the loop's best-scoring
    candidate, whose STL is the version's geometry), and the measurement
    comes from the ``BboxInfo`` that ``bbox_fn`` produced for THAT render
    (read from the best candidate's DECLARED ``IterationRecord.bbox``
    field — issue #93's precedent, the value ``bbox_fn`` returned for
    that render — never re-derived from the STL file, which may be gone
    by the time the version row is written).

    Multi-part (issue #100, same rule the bbox gate applies): when
    ``BboxInfo.components`` is non-empty, the gate matched the stated
    triple against the BEST-MATCHING COMPONENT, so the version persists
    THAT component's extents — the assembly extents would describe a
    body the user did not ask for. When the component is not identifiable
    (no breakdown, an empty split, or no stated triple to match against —
    the stated dims are read from the version's own params snapshot, and
    a zero/absent axis means "unknown", never a target), the measurement
    is NOT persisted (``None`` → the row stores NULL): abstaining is
    correct, guessing is not. A zero extent inside the extents abstains
    the same way (a zero is the encoded absence, issue #91).
    """
    best = getattr(result, "best", None)
    if best is None:
        return None
    bbox = getattr(best, "bbox", None)
    if not isinstance(bbox, BboxInfo):
        return None
    if bbox.components:
        params = best.params if isinstance(best.params, dict) else {}
        try:
            stated = (
                float(params.get("W", 0.0)),
                float(params.get("D", 0.0)),
                float(params.get("H", 0.0)),
            )
        except (TypeError, ValueError):
            return None
        if any(t <= 0 for t in stated):
            # An unknown stated axis: best_match_component has no defined
            # selection without a full triple (the gate abstains) — the
            # component is not identifiable, persist nothing.
            return None
        matched = best_match_component(bbox, stated)
        if matched is None:
            return None
        if any(e <= 0 for e in matched):
            return None
        return matched
    if any(e <= 0 for e in (bbox.x, bbox.y, bbox.z)):
        return None
    return (bbox.x, bbox.y, bbox.z)


def _version_render_artifact_dir(result: Any) -> str | None:
    """The durable on-disk path of the render that produced the version
    (issue #163), or ``None``.

    The version OWNS its render reference: the render that becomes the
    version is the BEST candidate (``result.best`` — the loop's
    best-scoring candidate, whose ``model.stl`` is the version's geometry),
    and the reference is that render's DECLARED
    ``RenderResult.render_artifact_dir`` field (issue #72's durable
    per-render directory, ``<renders_dir>/<uuid8>``) — the same value the
    SSE adapter reads for the artifact bytes. Renders land under a fresh
    per-render uuid unrelated to version ids, so the link is PERSISTED at
    version-creation time and never re-derived (the 3MF download route
    cannot infer it from mtime — a "probably right" directory is exactly
    what issue #163's spec rejects). ``None`` (a stub loop result without
    the field, or a render without a durable directory) stores a NULL:
    an absent render record degrades honestly at the route (a clear
    non-2xx), never a guess.
    """
    best = getattr(result, "best", None)
    if best is None:
        return None
    render = getattr(best, "render", None)
    if render is None:
        return None
    artifact_path = getattr(render, "render_artifact_dir", None)
    if not isinstance(artifact_path, str) or not artifact_path:
        return None
    return artifact_path


async def _resolve_version_create(
    app: Any, project_id: int, result: Any, user_message: str
) -> int | None:
    """On a ``pass``: create the version and return its id — a passing
    loop ALWAYS materialises a version (issue #93), with whatever params
    are known (possibly none).

    The version OWNS its geometry (issue #105): the passing best
    candidate's ``scad_source`` (a declared ``IterationRecord`` field —
    the value the loop's prompt builder already carried in the
    ``design_source`` prompt section on the loop's OWN iterations) is
    persisted as ``versions/{id}/design.scad`` inside
    ``create_version`` (same commit as the params snapshot). The loop's
    best candidate is authoritative for BOTH params and source — a stub
    without the declared field (or with an empty source) still versions
    params only (no spurious empty source file).
    Params, in strict precedence:

    1. the best candidate's OWN render parameters — read as a DECLARED
       ``IterationRecord`` field (issue #93: a duck-typed read of an
       attribute that was not a declared field used to return ``None`` for
       every real candidate, so a fresh project never got a version). A
       declared empty dict ({}) means "a dimensionless pass with no known
       parameters" and IS used as-is — it is the loop's authoritative
       answer, and an empty params set is legal for ``create_version``
       (``validate_params`` accepts it);
    2. else the latest version's params snapshot (a mid-project pass whose
       candidate somehow carries a non-dict param set — the fallback is
       unchanged in meaning from before the fix; ``latest_version`` is
       read once above for the name derivation and reused here);
    3. else ``{}`` — the version is still created: a pass with unknown
       dimensions is a legitimate state and the user must still get their
       model (the old guard returned ``None`` here, suppressing the
       version entirely).

    The version ``name`` (issue #245) is derived from WHAT CHANGED, never
    from the raw user message (a question like "how tall is it now" used
    to become the version name). Name source, in strict precedence:

    1. the model's ``// title:`` leading comment in the candidate's own
       SCAD — extracted like params (``d33d.design_loop.scad_title``),
       cleaned via ``d33d.versions.clean_name`` (the loop's design prompt
       now asks the model for exactly this comment line);
    2. a deterministic phrase from the param diff vs the previous
       version's params (``d33d.versions.param_diff_name``):
       "First design" / "<name> <old> → <new>" / "<n> parameters
       changed" / "Revised geometry".

    The raw user message is still passed as the ``message`` field
    (provenance — the "triggering message excerpt" the timeline renders)
    and NEVER becomes the name. ``None`` is returned only when ``best``
    itself is missing (a loop result that does not carry a candidate at
    all — a contract violation that must not fabricate a version), never
    when the parameter set is merely empty. The version ``message`` is the
    user's chat text truncated to 200 characters (Python string slicing is
    code-point-safe — no multi-byte split, unlike a raw byte slice)."""
    best = getattr(result, "best", None)
    if best is None:
        return None
    # The previous version, read ONCE up front (a single-row query): it is
    # both the param-diff baseline for the name (issue #245) and the
    # params fallback for the non-dict-params case below.
    latest = app.state.versions.latest_version(project_id)
    named = best.params  # a declared IterationRecord field (issue #93)
    if not isinstance(named, dict):
        # The only reachable case for a real record: a defensive fallback
        # that cannot actually fire — kept because the adapter is typed
        # ``Any`` and a corrupt record (non-dict param set) must degrade
        # to the latest-version snapshot rather than fabricate one.
        named = dict(latest["params"]) if latest is not None else {}
    # The candidate's OWN source (a declared field — ``best.scad_source``
    # is verified against the real ``IterationRecord``, not a stub's
    # assumed shape; an empty string / non-str yields no source file).
    candidate_source = getattr(best, "scad_source", None)
    if not isinstance(candidate_source, str) or not candidate_source.strip():
        candidate_source = None
    # The version name (issue #245): WHAT CHANGED, never the raw message.
    # The model's ``// title:`` comment in the candidate's own SCAD wins
    # when present; otherwise a deterministic phrase from the param diff
    # vs the previous version's params. (``named`` is a dict at this
    # point — the non-dict fallback just ran.)
    prev_params: dict | None = dict(latest["params"]) if latest is not None else None
    # Lazy import: ``d33d.versions`` imports ``d33d.projects`` (which imports
    # this module), so the name helpers are pulled in at call time.
    from d33d.versions import clean_name, param_diff_name

    if candidate_source is not None:
        title = scad_title(candidate_source)
        if title is not None:
            version_name = clean_name(title)
        else:
            version_name = param_diff_name(prev_params, dict(named))
    else:
        version_name = param_diff_name(prev_params, dict(named))
    # The thumbnail is the best render's iso view (the only view guaranteed
    # to frame the whole object — side views can be cropped per the
    # separately-tracked camera-fit issue). ``_artifact_bytes_from_path``
    # returns an empty dict for ``view_bytes`` when fewer than the full
    # 6-view set is present, so the pick degrades to ``None`` (a NULL row)
    # rather than substituting a different view or a placeholder. The name
    # IS passed (issue #245): the raw user message is the ``message``
    # field (provenance) and never becomes the name.
    render = getattr(best, "render", None)
    thumbnail = None
    if render is not None:
        _stl, view_bytes = _artifact_bytes_from_path(render)
        iso = view_bytes.get("view_05_iso.png")
        if iso is not None:
            thumbnail = _data_uri_from_bytes(iso, "image/png")
    version = await app.state.versions.create_version(
        project_id,
        dict(named),
        name=version_name,
        message=user_message[:200],
        scad_source=candidate_source,
        thumbnail=thumbnail,
        bbox=_version_bbox_extents(result),
        render_artifact_dir=_version_render_artifact_dir(result),
    )
    return int(version["id"])


async def run_design_loop_with_events(
    app: Any,
    project_id: int,
    *,
    user_message: str,
    stated_dims: tuple[float, float, float] | None,
    chat_history: tuple[str, ...],
    photo: str,
    request_text: str,
) -> AsyncIterator[tuple[str, dict[str, Any]]]:
    """Run the injected design loop (``app.state.run_design_loop``) for
    one chat message and yield the SSE frame contract.

    The heavy render work (``render_fn=render_for_design_loop`` — Docker
    subprocess work, minutes) is run off the event loop per the spec's
    ``asyncio.to_thread`` requirement — see :func:`_run_in_loop` — so a
    slow render does not stall other SSE streams or API handlers. The
    adapter itself is an async generator that yields frames as the loop
    progresses. Every exit path (pass, exhausted, exception) ends in a
    terminal ``done``|``error`` frame — the client
    (``ApiClient.streamEvents``) resolves only on a terminal frame, so an
    unhandled exception here would leave the stream hanging.

    ``photo`` is the project's stored photo as a data URI (computed by the
    caller — the route — synchronously before the 202 response; the
    background task runs via asyncio and the DB may be closed by the time
    the loop starts).

    ``stated_dims`` may be ``None`` ("no dimensions known" — the /chat
    caller path, ticket #91). The adapter no longer substitutes
    ``(0.0, 0.0, 0.0)`` here: a fabricated zero triple is never a gate
    target.

    The bbox gate itself changed for ALL callers (ticket #91): it now
    ABSTAINS on an unknown (``<= 0``) target instead of hard-failing it,
    and records the abstention distinctly in ``d33d.design_loop.score``'s
    ``bbox_abstained`` field. This includes ``create_region_edit``, which
    still deliberately passes a ``(0.0, 0.0, 0.0)`` triple on a fresh
    project — its bbox bit flipped from FAIL to ABSTAIN (recorded as
    ``Score.bbox_abstained``). The abstain is the correct semantics for
    that route: its own comment says "the dimension gate measures rather
    than fabricates", which IS abstain semantics — the old hard-fail
    contradicted it.
    """
    #: The per-view progress frames (``render-view-*``) land on this queue
    #: as the drain thread enqueues them. The generator is the sole
    #: consumer, so an ``asyncio.Queue`` needs no locking; frames are
    #: yielded while the render is still running (see the ``asyncio.wait``
    #: below), not after it completes.
    _frame_queue: "asyncio.Queue[tuple[str, dict[str, Any]] | None]" = asyncio.Queue()

    run_loop = getattr(app.state, "run_design_loop", None)
    if run_loop is None:
        yield ("error", {"message": "design loop not wired"})
        return

    # Issue #121: the per-view progress frames (``render-view-start`` /
    # ``render-view-done``) are produced by the render container's
    # stderr-drain thread (a worker thread, no event loop). The adapter
    # captures the current event loop HERE (before the loop is started —
    # the loop runs on a worker thread via ``asyncio.run`` and has no
    # loop to capture) and builds an ``on_progress`` hook that enqueues
    # a ``loop.call_soon_threadsafe`` schedule onto that loop via
    # ``run_coroutine_threadsafe``. The hook is sync (the drain thread
    # calls it directly); the async work (yielding the frame) happens on
    # the app's loop via the future the hook schedules. The stream
    # (this generator) is the sole consumer; no shared state is needed
    # because the generator is a single async consumer of one stream.
    #
    # The loop is started on ``asyncio.to_thread(_run_in_loop, raw)`` —
    # ``_run_in_loop`` runs ``asyncio.run(coro)`` on a worker thread, so
    # the render's ``subprocess.run`` (Docker) runs off the app's loop.
    # The drain thread is a child of that worker thread's ``subprocess
    # .run``; it must therefore schedule onto the APP's loop (captured
    # here), not the worker thread's fresh loop.
    def _on_progress(kind: str, payload: dict[str, Any]) -> None:
        """The drain thread calls this sync hook for each marker.

        ``kind`` is ``"view-start"`` or ``"view-done"`` (``view-failed``
        is filtered out — a failed view must not report progress).
        ``payload`` carries ``view`` (the view stem) and ``iteration``
        (the design-loop iteration index, 1-based).

        The hook schedules a ``put_nowait`` onto ``_frame_queue`` via
        ``_loop.call_soon_threadsafe`` (the hook runs on the drain's
        worker thread, so it must not touch the loop's queue from that
        thread); the generator yields the frame while the render is STILL
        running (see the ``await asyncio.wait`` below) — live, not
        batched at render completion.
        """
        if kind not in ("view-start", "view-done"):
            return
        view = payload.get("view", "")
        iteration = payload.get("iteration", 0)
        if not view:
            return
        step_name = (
            "render-view-start" if kind == "view-start" else "render-view-done"
        )
        _loop.call_soon_threadsafe(
            _frame_queue.put_nowait,
            ("progress", {"step": step_name, "view": view, "iteration": iteration}),
        )

    yield ("progress", {"step": "design-loop-start"})

    # The full design-loop kwargs contract (the same shape the finalize
    # seam's ``_finalize_loop_kwargs`` builds — photo as a data URI,
    # stated_dims from the caller — ``None`` when no dimensions are known,
    # never a fabricated ``(0.0, 0.0, 0.0)``; the loop's bbox gate abstains
    # on an unknown triple and records it in ``Score.bbox_abstained`` —
    # ticket #91), a real bbox_fn, and the ``request`` guaranteed
    # non-empty so an exhausted loop still archives to failures.jsonl.
    # ``render_fn`` and ``llm_fn`` are ``None`` by contract: the production
    # closure (``_build_production_design_loop``) builds its OWN
    # ``render_fn`` (``render_for_design_loop``) and ``llm_fn`` (from the
    # live catalogue) and does not consume these kwargs; a future seam
    # variant that DOES consume them would need to supply real callables
    # (the ``None`` placeholders are not a fallback — see the production
    # seam's ``render_fn=render_for_design_loop`` hardcode).
    #
    # ``request`` carries the CURRENT user's message (``user_message`` —
    # issue #97: the message used to be forwarded only as the
    # failures.jsonl hook's archive field and was never rendered in the
    # design prompt, so the model never saw what was asked). It rides
    # the single ``request`` kwarg end to end: the hook pops a copy for
    # archiving AND forwards the value to the loop, where
    # ``_design_messages`` renders it as the first ``Request:`` line.
    # ``request_text`` is the caller's alias for the same value (the
    # ``user_message`` parameter is authoritative — the version row's
    # message field reads it directly); a blank ``user_message`` degrades
    # to ``request_text`` rather than rendering an empty request line.
    # The app's event loop (this generator runs on it) — ``_on_progress``
    # uses it to schedule the thread-safe ``put_nowait``. Captured before
    # the loop starts: the design loop itself runs on a fresh loop on a
    # worker thread and has no loop to capture from there.
    _loop = asyncio.get_running_loop()
    kwargs: dict[str, Any] = {
        "photo": photo,
        "chat_history": chat_history,
        "stated_dims": stated_dims,
        "render_fn": None,  # the production closure supplies render_for_design_loop
        "llm_fn": None,
        "bbox_fn": bbox_from_render,
        "request": (user_message or request_text or "").strip() or request_text,
        "on_progress": _on_progress,
    }

    # The production closure (``_build_production_design_loop``) takes
    # ``(app, **kwargs)`` and forwards everything through the hook; the
    # kwargs (including ``on_progress``) are ALWAYS forwarded — a stub
    # that consumes any of them must take ``app`` / ``**kwargs`` like the
    # production closure (the app-parameter check only selects the
    # calling convention, never whether the kwargs arrive — issue #221
    # adversarial round 1: the conditional ``run_loop()`` call silently
    # dropped ``on_progress`` from no-``app`` stubs, breaking the
    # liveness-frame test's acceptance criterion).
    # The carried source (issue #105) is captured here — BEFORE the loop
    # runs — so the loop's prompt renders the current design (turn 1
    # renders the explicit clean-slate wording instead) and the value is
    # the one this turn's prompt actually carried. ``None`` when no
    # version owns a source yet; omitted from the kwargs then (a stub
    # seam without the kwarg is still called cleanly — the production
    # closure forwards **kwargs through the hook to the real loop, and
    # the real loop's ``design_source`` parameter defaults to ``None``
    # anyway, so the kwarg is only added when there IS a source to
    # carry).
    design_source: str | None = None
    row = app.state.conn.get_project(project_id)
    if row is not None:
        from d33d.design_source import current_version_source

        design_source = current_version_source(row, app.state.versions)
    if design_source is not None:
        kwargs["design_source"] = design_source
    try:
        if _loop_takes_app(run_loop):
            raw = run_loop(app=app, **kwargs)
        else:
            raw = run_loop(**kwargs)
        if inspect.isawaitable(raw):
            # The real loop is async: ``raw`` is a coroutine. The spec's
            # acceptance criterion — "the background task wraps the sync
            # render_for_design_loop in asyncio.to_thread (it does Docker
            # subprocess work, multi-minute)" — is implemented here. A bare
            # ``await raw`` would run the sync ``render_for_design_loop``
            # (Docker ``subprocess.run``, minutes) on the event loop and
            # stall every other SSE stream and API handler on the app, not
            # just the in-flight one.
            #
            # ``asyncio.to_thread`` cannot ``await`` a coroutine directly (a
            # worker thread has no running event loop to nest an
            # ``asyncio.run`` under — it would raise ``RuntimeError``), so
            # the work is run via ``to_thread(_run_in_loop, raw)``:
            # ``_run_in_loop`` drives the coroutine with ``asyncio.run`` on
            # a fresh loop, in a worker thread, so the entire design loop —
            # the multi-minute sync render AND the LLM's awaits — runs off
            # the app's event loop. The event loop's role is to pump the
            # frame queue while the render task runs: the ``to_thread``
            # work is wrapped in a ``Task`` and the generator
            # ``asyncio.wait``s it against a ``_frame_queue`` ``get``, so
            # the per-view frames are yielded WHILE the render is still
            # running (live progress — the old ``await
            # asyncio.to_thread(...)`` hoarded them and delivered the whole
            # batch in a single instant at render completion).
            render_task = asyncio.ensure_future(asyncio.to_thread(_run_in_loop, raw))
            # The total wall-clock deadline (issue #221) — measured from
            # HERE (generator start, before the first drain), never
            # reset by incoming frames. The deadline must race against
            # BOTH the ``to_thread`` render task AND the frame-queue
            # consumer: a loop that keeps emitting liveness frames
            # (per-view progress, LLM tokens) while it never terminates
            # would evade a timeout placed only around ``render_task``.
            _deadline = _loop.time() + DESIGN_LOOP_TIMEOUT_SECONDS
            while True:
                if render_task.done():
                    break
                try:
                    _f = _frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    _f = None
                if _f is not None:
                    yield _f
                    continue
                _remaining = _deadline - _loop.time()
                if _remaining <= 0:
                    # Deadline fired: cut off the stream with a terminal
                    # structured error frame. No further frames may be
                    # yielded after this one (the deadline frame is
                    # terminal by contract).
                    logger.warning(
                        "design loop for project %s exceeded the %ss "
                        "total deadline — emitting terminal "
                        "design_loop_timed_out frame",
                        project_id,
                        DESIGN_LOOP_TIMEOUT_SECONDS,
                    )
                    yield (
                        "error",
                        {
                            "message": (
                                "Design loop timed out after "
                                f"{int(DESIGN_LOOP_TIMEOUT_SECONDS)}s"
                            ),
                            "reason": DESIGN_LOOP_TIMED_OUT_REASON,
                        },
                    )
                    # Cancel the ``to_thread`` render task AFTER the frame
                    # has been yielded. CANCELLING IT BEFORE the yield
                    # deadlocks the executor thread-pool: the ``to_thread
                    # `` future's cancellation hooks run on the loop and
                    # wait for the executor's future to report the
                    # cancellation, but the executor thread is still
                    # running the (stalled) loop — a ``CancelledError``
                    # raised in that future blocks the pool's thread
                    # (measured: the test executor hangs for the full
                    # 300s shutdown join). Yielding first means the
                    # generator (and any caller's ``break``) has already
                    # completed the user-visible guarantee; the cancel
                    # then runs on a free loop. The ``to_thread`` worker
                    # thread cannot be killed mid-flight regardless (the
                    # terminal frame is the user-facing guarantee, not
                    # the thread's death), and the best-effort cancel
                    # suppresses the ``CancelledError`` so it does not
                    # surface as an unhandled exception when the loop
                    # closes.
                    _suppress_cancellation(render_task)
                    return
                # Empty queue, render still running: sleep until the next
                # frame is enqueued, the render task completes, or the
                # deadline fires — the ``asyncio.wait`` is a real await
                # (no busy-wait) that wakes on whichever happens first.
                _get_task = asyncio.ensure_future(_frame_queue.get())
                _done, _ = await asyncio.wait(
                    {_get_task, render_task},
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=_remaining,
                )
                if _get_task in _done:
                    _f = _get_task.result()
                    if _f is None:
                        # Sentinel: the render finished (the drain thread
                        # enqueued the ``None`` before the loop exited).
                        break
                    yield _f
                    continue
                _get_task.cancel()
                # Render task completed, or the deadline fired (timeout).
                # The deadline check at the top of the loop handles the
                # timeout; otherwise the loop re-checks ``render_task``
                # and the remainder is drained below.
            # The render finished: yield every remaining frame in FIFO
            # order (all frames were enqueued before the drain thread
            # joined, so nothing is lost) and take the render's result
            # (an exception here propagates to the wide catches below,
            # unchanged).
            while True:
                try:
                    _f = _frame_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break
                if _f is None:
                    # Sentinel: stop the drain, take the result.
                    break
                yield _f
            result = render_task.result()
        else:
            result = raw
    except (LookupError, KeyError) as e:
        # Project (or a loop prerequisite) deleted mid-flight — the
        # finalize pattern's ``assert row is not None`` raises
        # AssertionError/LookupError after deletion. Emit a terminal
        # error frame + release the flag, not a 500.
        logger.exception("design loop infra error for project %s", project_id)
        yield ("error", {"message": f"design loop infra failure: {e}"})
        return
    except (OSError, RuntimeError, TypeError, ValueError) as e:
        logger.exception("design loop failed for project %s", project_id)
        yield ("error", {"message": f"design loop failed: {e}"})
        return
    except Exception as e:  # broad by contract — every exit is a terminal frame
        logger.exception("design loop unexpected error for project %s", project_id)
        yield ("error", {"message": f"design loop unexpected error: {e}"})
        return
    if getattr(result, "status", None) == "pass":
        yield ("progress", {"step": "design-loop-pass"})
        best = getattr(result, "best", None)
        # The best candidate's OWN render artifacts (the best iteration's
        # STL + 6 views — never a later iteration's) are read from the
        # durable artifact directory the worker persisted to inside its
        # ``tempfile.TemporaryDirectory`` with-block (issue #72 — the
        # bytes survive past the tempdir teardown, so the adapter reads
        # them from the persistent path, not the dead tempdir paths).
        # A missing/dead render → fields omitted entirely (never a
        # bogus path, never a null), keeping the frame JSON-safe.
        render = getattr(best, "render", None)
        frame_fields: dict[str, Any] = {}
        if render is not None:
            stl_bytes, view_bytes = _artifact_bytes_from_path(render)
            if stl_bytes is not None:
                frame_fields["stl_data_uri"] = _data_uri_from_bytes(
                    stl_bytes, "application/octet-stream"
                )
            if view_bytes:
                frame_fields["views"] = {
                    name: _data_uri_from_bytes(raw, "image/png")
                    for name, raw in view_bytes.items()
                }
        # Ticket #91: propagate the bbox abstention to the wire so a
        # consumer can never mistake an abstained pass for a verified
        # one — ``Score.bbox_abstained`` exists on the score, but a
        # flag that stops at the Score object is not a safeguard: every
        # pass frame carries the flag (False for a fully measured pass,
        # True when any stated axis was unknown), in both the
        # version-created progress frame and the terminal done frame.
        # (The failures.jsonl archive needs no field: it fires ONLY on
        # ``exhausted`` results, and an abstained bbox bit is always
        # True, so an abstained gate never lands there as a failure —
        # it can only make a loop more pass-prone.)
        score = getattr(best, "score", None)
        abstained = bool(getattr(score, "bbox_abstained", False))
        version_id = await _resolve_version_create(
            app, project_id, result, user_message
        )
        if version_id is not None:
            vc_frame: dict[str, Any] = {
                "step": "version-created",
                "version_id": version_id,
            }
            vc_frame.update(frame_fields)
            vc_frame["bbox_abstained"] = abstained
            yield ("progress", vc_frame)
        scad = getattr(best, "scad_source", None)
        yield ("token", {"text": scad if isinstance(scad, str) else ""})
        yield ("done", {"message": _result_message(result), "bbox_abstained": abstained})
    else:
        # Exhausted (or otherwise non-pass): the terminal error frame gains
        # the STRUCTURED failure reason (issue #82) so the SPA can map it
        # to plain-language copy without string-matching the free-text
        # ``message`` (which is preserved verbatim for backward
        # compatibility). A ``None`` reason → the field is OMITTED (never a
        # null), matching the adapter's omit-not-null frame policy.
        error_data: dict[str, Any] = {"message": _result_message(result)}
        reason = _structured_reason(result)
        if reason is not None:
            error_data["reason"] = reason
        yield ("error", error_data)


__all__ = [
    "EMPTY_PHOTO_DATA_URI",
    "bbox_from_render",
    "latest_version_stated_dims",
    "photo_data_uri",
    "run_design_loop_with_events",
]
