"""Design-loop background-task adapter for the chat wire (issue #54).

The single place that runs ``app.state.run_design_loop`` for a chat
message and adapts its outcome onto the SSE frame contract that
``d33d.streaming._stream_events`` (``GET /api/stream/{project_id}``) and
the SPA's ``ApiClient.streamEvents`` expect:

- ``("progress", {step, iteration?})`` for each design-loop stage
- ``("token", {text})`` — exactly ONE token frame per completed loop
- ``("progress", {step: "version-created", version_id})`` on a pass
- ``("done", {message})`` on completion
- ``("error", {message})`` on exhaustion or infra failure

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

from d33d.design_loop import BboxInfo
from d33d.render_worker import RenderResult

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

    No ``merge_vertices()`` is needed here: bounds are derived from
    vertex coordinates and are invariant under duplicate-vertex merging,
    so (unlike the issue #84 watertight fix, which required merging before
    ``is_watertight``) the unmerged load is correct for this read.

    Side effect: also opportunistically caches artifact bytes — see
    :func:`cache_render_artifact_bytes`.

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
        return BboxInfo(x=x, y=y, z=z, volume=volume)
    except Exception:  # any load failure → gate fails (None), never a raise
        logger.exception("bbox_fn: failed to load STL %r", stl)
        return None


def _maybe_cache(render: Any, attr: str, value: Any) -> None:
    """Cache ``value`` on ``render`` under ``attr`` (a no-op when the
    object refuses the attribute — a frozen dataclass, e.g. the
    production ``RenderResult`` — the frame then omits the field).
    """
    try:
        setattr(render, attr, value)
    except (AttributeError, TypeError) as e:
        # Frozen dataclass — the frame omits the field. Log at debug so an
        # unexpected __setattr__ rejection (not a frozen dataclass) is
        # diagnosable instead of silently dropping the field.
        logger.debug("_maybe_cache: setattr(%r, %r) failed: %s", render, attr, e)


def _data_uri_from_bytes(raw: bytes, mime: str) -> str:
    """A base64 data URI from raw bytes and a MIME type."""
    b64 = base64.b64encode(raw).decode("ascii")
    return f"data:{mime};base64,{b64}"


def _artifact_bytes_from_path(render: Any) -> tuple[bytes | None, dict[str, bytes]]:
    """Read the best render's STL + view bytes from its DURABLE artifact
    directory (the per-render directory the worker persists to INSIDE its
    ``tempfile.TemporaryDirectory`` with-block before the block exits —
    issue #72; the directory survives past the tempdir teardown because the
    copy happened inside it).

    The durable path rides on the render object as a duck-typed
    ``render_artifact_path`` attribute — never a change to the frozen
    ``RenderResult`` contract (the worker's workstream attaches it; a test
    stub or a future worker may set it). ``None`` for either field means
    "no usable durable source" — the caller then falls back to the
    in-memory byte cache (``stl_bytes``/``view_bytes``).

    Views follow the partial-omit rule: a directory that yields fewer than
    the 6 fixed VIEWS filenames (a failed copy, a missing file) is treated
    as "no views" (empty dict) — a consumer cannot distinguish a 5-of-6
    map from a complete one, so the frame omits the field entirely rather
    than emit fewer than 6.
    """
    artifact_path = getattr(render, "render_artifact_path", None)
    if not isinstance(artifact_path, str) or not artifact_path:
        return None, {}
    artifact_dir = Path(artifact_path)
    if not artifact_dir.is_dir():
        return None, {}
    stl_bytes = _read_artifact_bytes(str(artifact_dir / "model.stl"))
    view_bytes: dict[str, bytes] = {}
    for v in artifact_dir.iterdir():
        if v.is_file() and v.suffix == ".png":
            b = _read_artifact_bytes(str(v))
            if b is not None:
                view_bytes[v.name] = b
    if len(view_bytes) != 6:
        # Partial views → "no views" (the frame omits the field entirely).
        view_bytes = {}
    return stl_bytes, view_bytes


def cache_render_artifact_bytes(render: Any) -> None:
    """Cache the best render's STL + views bytes on the render object so
    the SSE adapter (which runs AFTER the render worker's tempdir is torn
    down) can still emit them.

    The render worker's ``tempfile.TemporaryDirectory`` dies when the
    render returns — *before* the design loop yields the result to the
    chat adapter, so a read inside the adapter's generator (before the
    ``yield``) sees dead paths in production. The ONE place the artifacts
    are still reachable when the bytes are still on disk is the sync
    render path, so the caller (the production loop seam) runs this as an
    explicit post-render cache step on the result's render object (a
    duck-typed attribute — never a change to the ``RenderResult``
    contract) and the adapter reads them from memory.

    A no-op when the bytes are already dead (the production seam today —
    the frame omits the fields) and a pure win when they survive (tests,
    or a future worker that keeps the artifacts). Never raises.
    """
    stl = getattr(render, "stl", None)
    _maybe_cache(render, "stl_bytes", _read_artifact_bytes(stl))
    views = getattr(render, "views", ())
    if isinstance(views, (tuple, list)) and views:
        view_bytes: dict[str, bytes] = {}
        for v in views:
            b = _read_artifact_bytes(v)
            if b is not None:
                # Bare filenames per the VIEWS contract; a Path object is a
                # realistic input, so derive its name explicitly.
                key = Path(v).name if isinstance(v, (str, Path)) else str(v)
                view_bytes[key] = b
        if len(view_bytes) != len(views):
            # A partial views map (some artifact unreadable/dead) is
            # treated as "no views" — the frame omits the field entirely
            # rather than emitting fewer than the fixed 6, which a consumer
            # cannot distinguish from a complete map.
            view_bytes = {}
        if view_bytes:
            _maybe_cache(render, "view_bytes", view_bytes)


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


def _result_message(result: Any) -> str:
    """The terminal ``done``/``error`` frame text for a loop result."""
    if getattr(result, "status", None) == "pass":
        return "Design loop passed validation"
    reason = getattr(result, "failure_reason", None)
    if isinstance(reason, str) and reason:
        return f"Design loop exhausted: {reason}"
    return "Design loop exhausted"


def _loop_takes_app(run_loop: Any) -> bool:
    """The injected design-loop seam's signature check (production
    ``_build_production_design_loop`` takes ``(app, **kwargs)``; test
    stubs may take none or a subset)."""
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


async def _resolve_version_create(
    app: Any, project_id: int, result: Any, user_message: str
) -> int | None:
    """On a ``pass``: create the version (params from the best candidate,
    falling back to the latest version's snapshot or ``{}`` for a fresh
    project) and return its id. ``None`` when no version is created (no
    usable parameter set — an exhausted-with-no-pass or a contract
    violation). The version ``message`` is the user's chat text truncated
    to 200 characters (Python string slicing is code-point-safe — no
    multi-byte split, unlike a raw byte slice)."""
    best = getattr(result, "best", None)
    named = getattr(best, "params", None)
    if not (isinstance(named, dict) and named):
        latest = app.state.versions.latest_version(project_id)
        named = dict(latest["params"]) if latest is not None else {}
    if not named:
        return None
    version = await app.state.versions.create_version(
        project_id,
        named,
        name="design",
        message=user_message[:200],
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
    """
    run_loop = getattr(app.state, "run_design_loop", None)
    if run_loop is None:
        yield ("error", {"message": "design loop not wired"})
        return

    yield ("progress", {"step": "design-loop-start"})

    # The full design-loop kwargs contract (the same shape the finalize
    # seam's ``_finalize_loop_kwargs`` builds — photo as a data URI,
    # stated_dims from the request or the latest version's W/D/H, a real
    # bbox_fn, and the hook's ``request`` guaranteed non-empty so an
    # exhausted loop still archives to failures.jsonl). ``render_fn`` and
    # ``llm_fn`` are ``None`` by contract: the production closure
    # (``_build_production_design_loop``) builds its OWN ``render_fn``
    # (``render_for_design_loop``) and ``llm_fn`` (from the live catalogue)
    # and does not consume these kwargs; a future seam variant that DOES
    # consume them would need to supply real callables (the ``None``
    # placeholders are not a fallback — see the production seam's
    # ``render_fn=render_for_design_loop`` hardcode).
    kwargs: dict[str, Any] = {
        "photo": photo,
        "chat_history": chat_history,
        "stated_dims": stated_dims or (0.0, 0.0, 0.0),
        "render_fn": None,  # the production closure supplies render_for_design_loop
        "llm_fn": None,
        "bbox_fn": bbox_from_render,
        "request": request_text,
    }

    # The production closure (``_build_production_design_loop``) builds its
    # own ``render_fn`` / ``llm_fn`` from the live catalogue and pops the
    # hook's ``model``/``prompt_version``/``request`` kwargs before
    # forwarding to the real loop; a test stub (no ``app`` kwarg) is called
    # with none.
    try:
        if _loop_takes_app(run_loop):
            raw = run_loop(app=app, **kwargs)
        else:
            raw = run_loop()
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
            # the app's event loop. The event loop's only role is the short
            # ``await asyncio.to_thread(...)`` below.
            result = await asyncio.to_thread(_run_in_loop, raw)
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
        # Durable artifact directory the worker persisted to inside its
        # ``tempfile.TemporaryDirectory`` with-block (issue #72 — the
        # bytes survive past the tempdir teardown, so the adapter reads
        # them from the persistent path, not the dead tempdir paths) with
        # the in-memory byte cache (``stl_bytes``/``view_bytes``, attached
        # via ``cache_render_artifact_bytes`` on the sync render path) as
        # the fallback for stubs/tests that carry no durable path.
        # A missing/dead render → fields omitted entirely (never a
        # bogus path, never a null), keeping the frame JSON-safe.
        render = getattr(best, "render", None)
        frame_fields: dict[str, Any] = {}
        if render is not None:
            # Durable source first (persisted files take precedence), then
            # the setattr byte-cache fallback (tests without a durable dir).
            stl_bytes, view_bytes = _artifact_bytes_from_path(render)
            if stl_bytes is None:
                stl_bytes = getattr(render, "stl_bytes", None)
            if not view_bytes:
                view_bytes = getattr(render, "view_bytes", None) or {}
            if stl_bytes is not None:
                frame_fields["stl_data_uri"] = _data_uri_from_bytes(
                    stl_bytes, "application/octet-stream"
                )
            if view_bytes:
                frame_fields["views"] = {
                    name: _data_uri_from_bytes(raw, "image/png")
                    for name, raw in view_bytes.items()
                }
        version_id = await _resolve_version_create(
            app, project_id, result, user_message
        )
        if version_id is not None:
            vc_frame: dict[str, Any] = {
                "step": "version-created",
                "version_id": version_id,
            }
            vc_frame.update(frame_fields)
            yield ("progress", vc_frame)
        scad = getattr(best, "scad_source", None)
        yield ("token", {"text": scad if isinstance(scad, str) else ""})
        yield ("done", {"message": _result_message(result)})
    else:
        yield ("error", {"message": _result_message(result)})


__all__ = [
    "EMPTY_PHOTO_DATA_URI",
    "bbox_from_render",
    "cache_render_artifact_bytes",
    "photo_data_uri",
    "run_design_loop_with_events",
]
