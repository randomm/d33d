"""SSE endpoint tests (issue #23, workstream task-b).

Covers:
- ``GET /api/stream/{project_id}`` returns ``text/event-stream`` content type.
- Event schema: ``{event, data}`` with event in {progress, token, done, error}.
- Event ordering: progress → token* → done (or error).
- Stream terminates on the terminal event (done or error).
- No event source registered → immediate done (empty stream contract).
- Non-existent project → error event.

All tests non-slow: no Docker, no network, no port binding. The event source
is an injectable async generator (``app.state.event_sources``) — no live LLM.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from d33d.app import create_app
from d33d.streaming import format_sse

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_async(app: Any, coro_factory) -> Any:
    """Drive an async app under a fresh event loop, running the lifespan."""

    async def _run():
        async with app.router.lifespan_context(app):
            client = AsyncClient(
                transport=ASGITransport(app=app), base_url="http://test"
            )
            async with client:
                return await coro_factory(client)

    return asyncio.run(_run())


def parse_sse_stream(raw: str) -> list[dict[str, Any]]:
    """Parse a raw SSE text stream into a list of (event, data) dicts.

    The SSE format is:
        event: <name>
        data: <json>
        (empty line)

    Multi-line data is reassembled (multiple ``data:`` lines are joined with \\n).
    """
    import json as _json

    events: list[dict[str, Any]] = []
    current_event: str | None = None
    data_lines: list[str] = []

    for line in raw.split("\n"):
        if line.startswith("event: "):
            current_event = line[7:].strip()
        elif line.startswith("data: "):
            data_lines.append(line[6:])
        elif line == "data:":
            data_lines.append("")
        elif line == "" and current_event is not None:
            # Empty line terminates the event
            data_text = "\n".join(data_lines)
            try:
                data = _json.loads(data_text) if data_text else {}
            except _json.JSONDecodeError:
                data = {"raw": data_text}
            events.append({"event": current_event, "data": data})
            current_event = None
            data_lines = []
        elif line == "":
            # Empty line with no event (keep-alive) — skip
            continue

    # If there's a trailing event without a final empty line
    if current_event is not None and data_lines:
        data_text = "\n".join(data_lines)
        try:
            data = _json.loads(data_text) if data_text else {}
        except _json.JSONDecodeError:
            data = {"raw": data_text}
        events.append({"event": current_event, "data": data})

    return events


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def app_paths(tmp_path: Path) -> dict[str, Path]:
    return {
        "db": tmp_path / "d33d.sqlite3",
        "key": tmp_path / "master.key",
        "cat": tmp_path / "models.yaml",
    }


@pytest.fixture
def app_with_streaming(app_paths: dict[str, Path]):
    """A ``create_app`` instance — the streaming router (and the empty
    ``event_sources`` dict on ``app.state``) are wired by the factory
    itself; no manual mounting in tests."""
    return create_app(
        app_paths["db"],
        master_key_path=app_paths["key"],
        catalogue_path=app_paths["cat"],
    )


# ---------------------------------------------------------------------------
# SSE framing
# ---------------------------------------------------------------------------


def test_format_sse_single_event():
    """format_sse produces valid SSE framing for a single event."""
    frame = format_sse("token", {"text": "hello"})
    assert "event: token" in frame
    assert "data: " in frame
    # Must end with double newline (empty line terminates the event)
    assert frame.endswith(("\n\n", "\n"))


def test_format_sse_data_is_json():
    """The data field is JSON-encoded."""
    import json

    frame = format_sse("progress", {"step": "rendering", "n": 42})
    lines = frame.strip().split("\n")
    data_lines = [l[6:] for l in lines if l.startswith("data: ")]
    data = json.loads("\n".join(data_lines))
    assert data == {"step": "rendering", "n": 42}


# ---------------------------------------------------------------------------
# SSE endpoint
# ---------------------------------------------------------------------------


def test_sse_content_type(app_with_streaming):
    """GET /api/stream/{id} returns Content-Type: text/event-stream."""

    async def _call(client):
        # Create a project first
        create_r = await client.post("/api/projects", json={"name": "SSE Test"})
        pid = create_r.json()["id"]
        return await client.get(f"/api/stream/{pid}")

    r = _run_async(app_with_streaming, _call)
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")


def test_sse_no_event_source_yields_done(app_with_streaming):
    """When no event source is registered for a project, the stream
    immediately emits a ``done`` event and terminates."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "No Source"})
        pid = create_r.json()["id"]
        async with client.stream("GET", f"/api/stream/{pid}") as response:
            chunks = []
            async for chunk in response.aiter_text():
                chunks.append(chunk)
            return "".join(chunks)

    raw = _run_async(app_with_streaming, _call)
    events = parse_sse_stream(raw)
    # Should have exactly one done event
    assert len(events) == 1
    assert events[0]["event"] == "done"
    assert "message" in events[0]["data"]


def test_sse_streams_progress_token_done(app_with_streaming):
    """With a fake event source, the stream emits progress → token* → done
    in the committed schema."""

    async def fake_source():
        yield "progress", {"step": "thinking", "n": 1}
        yield "token", {"text": "Hello"}
        yield "token", {"text": " world"}
        yield "progress", {"step": "rendering", "n": 2}
        yield "done", {}

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Stream Test"})
        pid = create_r.json()["id"]
        # Register the fake event source
        app_with_streaming.state.event_sources[pid] = fake_source()
        async with client.stream("GET", f"/api/stream/{pid}") as response:
            chunks = []
            async for chunk in response.aiter_text():
                chunks.append(chunk)
            return "".join(chunks)

    raw = _run_async(app_with_streaming, _call)
    events = parse_sse_stream(raw)

    # Verify the event sequence
    event_names = [e["event"] for e in events]
    assert event_names == ["progress", "token", "token", "progress", "done"]

    # Verify data schema
    assert events[0]["data"] == {"step": "thinking", "n": 1}
    assert events[1]["data"] == {"text": "Hello"}
    assert events[2]["data"] == {"text": " world"}
    assert events[3]["data"] == {"step": "rendering", "n": 2}
    assert events[4]["data"] == {}


def test_sse_error_event_terminates_stream(app_with_streaming):
    """An error event terminates the stream (no events after it)."""

    async def error_source():
        yield "progress", {"step": "thinking"}
        yield "error", {"message": "model timeout"}
        yield "done", {}  # should NOT be reached

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Error Test"})
        pid = create_r.json()["id"]
        app_with_streaming.state.event_sources[pid] = error_source()
        async with client.stream("GET", f"/api/stream/{pid}") as response:
            chunks = []
            async for chunk in response.aiter_text():
                chunks.append(chunk)
            return "".join(chunks)

    raw = _run_async(app_with_streaming, _call)
    events = parse_sse_stream(raw)

    # The stream should stop at the error event
    event_names = [e["event"] for e in events]
    assert "error" in event_names
    assert "done" not in event_names  # the done after error is not yielded


def test_sse_unknown_project_emits_error(app_with_streaming):
    """Streaming for a non-existent project ID emits an error event."""

    async def _call(client):
        return await client.get("/api/stream/99999")

    # Don't include the projects router — we just want to hit the stream endpoint
    # The project doesn't exist in the DB, so it should emit an error event.
    r = _run_async(app_with_streaming, _call)
    assert r.status_code == 200  # SSE stream always 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = parse_sse_stream(r.text)
    assert len(events) == 1
    assert events[0]["event"] == "error"


def test_sse_headers(app_with_streaming):
    """The SSE response includes proper caching headers."""

    async def _call(client):
        create_r = await client.post("/api/projects", json={"name": "Header Test"})
        pid = create_r.json()["id"]
        return await client.get(f"/api/stream/{pid}")

    r = _run_async(app_with_streaming, _call)
    assert r.headers.get("cache-control") == "no-cache"
    assert r.headers.get("x-accel-buffering") == "no"


def test_sse_event_schema_fields():
    """Verify the committed schema: event is a string in the allowed set,
    data is a dict with optional step/text fields."""

    # progress event
    frame = format_sse("progress", {"step": "rendering"})
    events = parse_sse_stream(frame)
    assert events[0]["event"] == "progress"
    assert isinstance(events[0]["data"], dict)
    assert "step" in events[0]["data"]

    # token event
    frame = format_sse("token", {"text": "hi"})
    events = parse_sse_stream(frame)
    assert events[0]["event"] == "token"
    assert events[0]["data"]["text"] == "hi"

    # done event
    frame = format_sse("done", {"message": "Design loop passed validation"})
    events = parse_sse_stream(frame)
    assert events[0]["event"] == "done"
    # A design-loop done frame carries a message string.
    assert isinstance(events[0]["data"].get("message"), str)

    # done event (issue #249, the answer path): ONE terminal frame whose
    # ``message`` is the answer text plus the additive ``kind: "answer"
    # discriminator (design-loop done frames carry no ``kind`` — the
    # SPA renders an answer done's message verbatim, no PassCard, no
    # refetch). The ``kind`` field is additive: a bare done frame
    # (``kind`` absent) still parses identically, and the answer done
    # frame's ``kind`` rides through the SSE wire unchanged.
    frame = format_sse("done", {"message": "It is 12 mm tall - you said that.", "kind": "answer"})
    events = parse_sse_stream(frame)
    assert events[0]["event"] == "done"
    assert events[0]["data"]["message"] == "It is 12 mm tall - you said that."
    assert events[0]["data"]["kind"] == "answer"

    # error event
    frame = format_sse("error", {"message": "boom"})
    events = parse_sse_stream(frame)
    assert events[0]["event"] == "error"
    assert events[0]["data"]["message"] == "boom"
