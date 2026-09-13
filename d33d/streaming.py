"""SSE streaming endpoint (issue #23, workstream task-b).

Endpoint: ``GET /api/stream/{project_id}``

Streams Server-Sent Events in the committed contract schema::

    {event: "progress" | "token" | "done" | "error", data: {step?, text?, ...}}

The event source is **injectable** via ``app.state.event_sources`` — a
dict mapping ``project_id -> async iterator of (event, data) tuples``. This
allows tests to drive the stream with a fake/async-generator without any
live LLM, while the production wiring (design-loop token streaming, render
worker progress) will be wired in by a future ticket.

The stream terminates on the terminal event (``done`` or ``error``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

from d33d import db as db_mod

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSE formatting
# ---------------------------------------------------------------------------


def format_sse(event: str, data: dict[str, Any]) -> str:
    """Format one SSE frame per the WHATWG SSE spec."""
    lines = [f"event: {event}"]
    payload = json.dumps(data, ensure_ascii=False)
    # Split into SSE-compatible data lines (handle multi-line JSON)
    for line in payload.split("\n"):
        lines.append(f"data: {line}")
    lines.append("")  # empty line terminates the event
    lines.append("")
    return "\n".join(lines)


async def _stream_events(
    project_id: int,
    event_sources: dict[int, AsyncIterator[tuple[str, dict[str, Any]]]] | None,
    conn: db_mod.Connection | None,
):
    """Yield SSE frames from the injectable event source for ``project_id``.

    If no event source is registered for the project, the stream immediately
    emits a ``done`` event and terminates (the "empty stream" contract).
    """
    # Validate the project exists (404 if not)
    if conn is not None:
        row = conn.get_project(project_id)
        if row is None:
            # We can't raise HTTPException inside a streaming response body;
            # instead emit an error event and terminate.
            yield format_sse("error", {"message": "project not found"})
            return

    if event_sources is None or project_id not in event_sources:
        # No event source → immediate done (the stream is empty but valid)
        yield format_sse("done", {"message": "no active stream"})
        return

    source = event_sources[project_id]
    try:
        async for event, data in source:
            yield format_sse(event, data)
            if event in ("done", "error"):
                break  # terminal event
    except Exception:  # broad by contract: the stream MUST end with a terminal frame (see below)
        # Broad catch (not the historical OSError/ValueError/
        # StopAsyncIteration trio): the committed contract is that the
        # stream ALWAYS terminates with a terminal ``done`` or ``error``
        # frame — the client (``ApiClient.streamEvents``) resolves only on
        # a terminal frame, so an escaped exception (e.g. a KeyError from
        # the event-source adapter) would close the stream with no frame
        # and the client would hang. The event-source adapter
        # (``d33d.design_loop_events``) is written to catch broadly and
        # always yield a terminal frame itself; this clause is the
        # backstop for an adapter that itself raised.
        logger.exception("SSE event source for project %s raised", project_id)
        yield format_sse(
            "error", {"message": "stream error"}
        )


# ---------------------------------------------------------------------------
# Router factory
# ---------------------------------------------------------------------------


def create_streaming_router() -> APIRouter:
    """Build and return the streaming router.

    The router reads ``request.app.state.event_sources`` (a dict mapping
    ``project_id -> AsyncIterator[(event, data)]``) at request time.
    Tests register fake async generators on ``app.state.event_sources``
    before making the request.
    """
    router = APIRouter(prefix="/api/stream", tags=["streaming"])

    @router.get("/{project_id}")
    async def stream_project(request: Request, project_id: int) -> StreamingResponse:
        conn: db_mod.Connection | None = request.app.state.conn
        event_sources: dict[int, AsyncIterator[tuple[str, dict[str, Any]]]] | None = (
            getattr(request.app.state, "event_sources", None)
        )

        return StreamingResponse(
            _stream_events(project_id, event_sources, conn),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router


__all__ = [
    "create_streaming_router",
    "format_sse",
]
