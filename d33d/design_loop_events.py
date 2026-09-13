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

    ``render_for_design_loop`` trimesh-loads the harvested ``model.stl``
    and computes ``vertex_count`` / ``watertight`` / ``volume_mm3`` from
    the same mesh — the extents come from that same on-volume STL (``
    render.stl``), re-loaded host-side here (the file lives in the
    caller's harvest dir and outlives the render; it is only removed
    when the volume is torn down, which the caller owns).

    Returns ``None`` (the bbox gate fails — the loop cannot score the
    bbox bit) when the render has no STL or it cannot be loaded; never
    raises.
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


async def _resolve_version_create(
    app: Any, project_id: int, result: Any, user_message: str
) -> int | None:
    """On a ``pass``: create the version (params from the best candidate,
    falling back to the latest version's snapshot or ``{}`` for a fresh
    project) and return its id. ``None`` when no version is created (no
    usable parameter set — an exhausted-with-no-pass or a contract
    violation)."""
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
    subprocess work, minutes) runs on the event loop via the loop's own
    ``await`` chain; the adapter itself is an async generator that yields
    frames as the loop progresses. Every exit path (pass, exhausted,
    exception) ends in a terminal ``done``|``error`` frame — the client
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
    # exhausted loop still archives to failures.jsonl).
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
            # The real loop is async: ``raw`` is a coroutine. The heavy
            # render work (``render_fn=render_for_design_loop`` — Docker
            # subprocess work, minutes) runs on the event loop via the
            # loop's own ``await`` chain; ``asyncio.to_thread`` would nest
            # an ``asyncio.run`` and raise ``RuntimeError`` (a thread has
            # no running loop to nest under — the production closure is
            # ``async`` precisely so it can await its render calls). The
            # loop's render calls are the only blocking I/O here and they
            # run synchronously inside the coroutine's own thread via
            # ``d33d.design_loop._call``'s sync-or-await dispatch.
            result = await raw
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
        version_id = await _resolve_version_create(app, project_id, result, user_message)
        if version_id is not None:
            yield (
                "progress",
                {"step": "version-created", "version_id": version_id},
            )
        best = getattr(result, "best", None)
        scad = getattr(best, "scad_source", None)
        yield ("token", {"text": scad if isinstance(scad, str) else ""})
        yield ("done", {"message": _result_message(result)})
    else:
        yield ("error", {"message": _result_message(result)})


__all__ = [
    "EMPTY_PHOTO_DATA_URI",
    "bbox_from_render",
    "photo_data_uri",
    "run_design_loop_with_events",
]
