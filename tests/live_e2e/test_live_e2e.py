"""Live e2e suite (issue #108).

Drives requests through the REAL path end to end — the real HTTP routes
(``POST /api/projects`` / ``POST /api/projects/{id}/chat``), the real
design loop (``app.state.run_design_loop`` exactly as ``create_app``
wires it — NO override of the DI seam), the real LLM (the endpoint in
models.yaml, key ``TRAIL_OPENERS_LLM_KEY``), the real Docker render
worker (``render_for_design_loop`` via the production design-loop
closure), and the real SSE event stream — CONSUMING the stream
(``app.state.event_sources[pid]``) rather than intercepting it.

Each invariant names the historical defect it would have caught
(#80, #84, #87, #89, #91, #93, #97) — in THIS file, so it survives
squash-merge. The vision judge is NOT part of this suite: the
deterministic gates are the pass/fail authority; the loop's judge is
reported inside the design loop, never blocking here.

Invoked by a single documented command (AGENTS.md quality-gates
table): ``uv run pytest -m live``. NOT a PR gate: no repository secret
exists for the key, the suite is non-deterministic, and it costs
tokens + real renders (tens of seconds per case). The ``live`` marker
keeps the fast CI job (``pytest -m "not slow and not live"``) from ever
calling the real LLM; see .github/workflows/ci.yml.
"""

from __future__ import annotations

import asyncio
import base64
import os
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import trimesh
from fastapi import FastAPI

# conftest.py is the sibling conftest of THIS directory (pytest loads it
# automatically; for direct imports resolve it by path so the module is
# importable without a package __init__).
sys.path.insert(0, str(Path(__file__).resolve().parent))

from conftest import REPO_ROOT, case_timer

from d33d import db as db_mod
from d33d import print_validation as _pv
from d33d.app import create_app
from d33d.versions import VersionService, migrate

pytestmark = pytest.mark.live


# ---------------------------------------------------------------------------
# App + HTTP harness (the REAL path, no stubs)
# ---------------------------------------------------------------------------


def _build_live_app(tmp_path: Path) -> FastAPI:
    """Build the app the same way the Playwright webServer does —
    ``create_app`` with NO override of ``app.state.run_design_loop``
    (the production wiring is what the suite is measuring). The
    catalogue is the REAL models.yaml at the repo root (the model is
    configured, never hardcoded); the key resolves from the
    ``${TRAIL_OPENERS_LLM_KEY}`` environment reference at load time.

    The render temp dir is under $HOME (``D33D_RENDER_TMP``), set BEFORE
    the app is built: Docker on macOS cannot see /tmp — that exact
    mistake caused a 26-minute silent hang (documented in the ticket
    and in ``d33d.render_worker._render_host_tmp_base``).
    """
    render_tmp = Path.home() / ".d33d-live-e2e" / "render-tmp"
    render_tmp.mkdir(parents=True, exist_ok=True)
    os.environ["D33D_RENDER_TMP"] = str(render_tmp)

    app = create_app(
        db_path=tmp_path / "live.db",
        master_key_path=tmp_path / "master.key",
        catalogue_path=_catalogue_path(),
    )
    return app


def _catalogue_path() -> Path:
    """The real catalogue (models.yaml). The operator maintains it at
    the main checkout (untracked — no secret, only the
    ``${TRAIL_OPENERS_LLM_KEY}`` reference); worktrees cut from
    origin/main do not carry it, so resolve the main-checkout path
    first, falling back to the worktree root for a non-worktree
    checkout."""
    main = Path("/Users/janni/projects/d33d/models.yaml")
    return main if main.is_file() else REPO_ROOT / "models.yaml"


def _start_lifespan(app: FastAPI) -> FastAPI:
    """Enter the app's lifespan wiring directly (the suite drives routes
    over httpx's ASGITransport, which does not run uvicorn's lifespan
    hook): DB connection + migration + version service — the same
    objects ``d33d.app._lifespan`` installs."""
    conn = db_mod.connect(app.state.db_path)
    migrate(conn)
    app.state.conn = conn
    app.state.versions = VersionService(conn)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://live"
    )


async def _create_project(client: httpx.AsyncClient, name: str) -> int:
    """The real project route (per-project git repo created on disk)."""
    resp = await client.post("/api/projects", json={"name": name})
    assert resp.status_code == 201, (
        f"project create failed: {resp.status_code} {resp.text}"
    )
    return int(resp.json()["id"])


async def _post_chat(
    client: httpx.AsyncClient, pid: int, message: str
) -> httpx.Response:
    """The real chat route (202 + the design loop registered on
    ``app.state.event_sources``)."""
    resp = await client.post(f"/api/projects/{pid}/chat", json={"message": message})
    return resp


def _wait_for_terminal(
    app: FastAPI, pid: int, timeout_s: float
) -> list[tuple[str, dict[str, Any]]]:
    """Consume ``app.state.event_sources[pid]`` (the REAL design-loop
    event generator — the same generator the SSE endpoint would stream)
    until a terminal ``done``/``error`` frame, under a hard wall-clock
    timeout.

    CONSUMES the stream rather than intercepting it: no
    ``page.route``/``route.fulfill`` (the Playwright pattern the ticket
    calls out as hand-built frames), no injected stub loop. The
    in-flight flag (normally cleared by the SSE endpoint's finally) is
    cleared here on every exit path.
    """
    source = app.state.event_sources.get(pid)
    if source is None:
        raise AssertionError(
            f"no event source registered for project {pid}: the chat route "
            "registers it synchronously before the 202 — a missing source "
            "means the real design loop did not run"
        )

    frames: list[tuple[str, dict[str, Any]]] = []

    async def _drive() -> None:
        deadline = time.monotonic() + timeout_s
        while True:
            try:
                item = await asyncio.wait_for(
                    source.__anext__(),
                    timeout=max(0.001, deadline - time.monotonic()),
                )
            except StopAsyncIteration:
                return
            except TimeoutError:
                raise AssertionError(
                    f"live case exceeded its {timeout_s:.0f}s wall-clock budget "
                    f"— a hung render or an unresponsive endpoint (a silent "
                    f"hang is the defect this suite exists to catch; frames "
                    f"so far: {frames!r})"
                )
            frames.append((item[0], dict(item[1])))
            if item[0] in ("done", "error"):
                return

    loop = asyncio.new_event_loop()
    try:
        loop.run_until_complete(_drive())
    finally:
        inflight = getattr(app.state, "design_loop_inflight", None)
        if inflight is not None:
            inflight.discard(pid)
        loop.close()
    return frames


# ---------------------------------------------------------------------------
# The #97 seam — a RECORDING WRAPPER around the real send (a recorder,
# never a replacement: it records the outgoing design-role messages and
# DELEGATES to ``d33d.design_llm.send``). The production design-loop
# closure builds its ``llm_fn`` via ``make_llm_fn`` and calls ``send`` at
# CALL time, so patching the module attribute wraps every design call.
# ---------------------------------------------------------------------------


class RecordedPrompts:
    """The outgoing design-role message lists, one entry per LLM call."""

    def __init__(self) -> None:
        self.calls: list[list[dict[str, Any]]] = []


def _install_recording_send(recorder: RecordedPrompts) -> None:
    """Wrap the real send with a recorder that DELEGATES to it (a
    recorder, never a replacement — the real LLM is still called, the
    key still stays in the factory). Records only the design-role
    outgoing messages (the #97 invariant's surface).

    The production design-loop closure builds its ``llm_fn`` via
    ``make_llm_fn`` and calls ``send`` at CALL time — but it does so
    through ``d33d.design_loop``'s OWN module-level import (``from
    d33d.design_llm import send``), so the patch must target BOTH the
    owning module (``d33d.design_llm.send``) and the caller's binding
    (``d33d.design_loop.send``). Patching only the owner would leave the
    loop's closure bound to the unrecorded original — the recorder would
    capture nothing and the #97 invariant would fail for the wrong
    reason (no messages recorded)."""
    from d33d import design_llm, design_loop

    real_send = design_llm.send

    async def _recording_send(*args: Any, **kwargs: Any) -> Any:
        if kwargs.get("role") == "design":
            messages = kwargs.get("messages")
            if messages is not None:
                recorder.calls.append([dict(m) for m in messages])
        return await real_send(*args, **kwargs)

    _recording_send.__real_send__ = real_send  # type: ignore[attr-defined]
    design_llm.send = _recording_send  # type: ignore[assignment]
    design_loop.send = _recording_send  # type: ignore[assignment]


def _uninstall_recording_send() -> None:
    from d33d import design_llm, design_loop

    real = getattr(design_llm.send, "__real_send__", None)
    if real is not None:
        design_llm.send = real  # type: ignore[assignment]
        design_loop.send = real  # type: ignore[assignment]


def _user_text_of(message: dict[str, Any]) -> str:
    """The text part(s) of an OpenAI-shaped message (string or parts)."""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") == "text"
        )
    return ""


def _all_sent_text(recorder: RecordedPrompts) -> str:
    """The concatenation of every text part of every recorded design
    message (the surface the #97 invariant checks)."""
    return "\n".join(
        _user_text_of(msg) for messages in recorder.calls for msg in messages
    )


# ---------------------------------------------------------------------------
# Frame helpers
# ---------------------------------------------------------------------------


def _b64_payload(data_uri: str) -> bytes:
    """The raw bytes from a data URI."""
    assert data_uri.startswith("data:"), f"not a data URI: {data_uri[:40]!r}"
    _, b64 = data_uri.split(",", 1)
    return base64.b64decode(b64)


def _version_created_frames(
    frames: list[tuple[str, dict[str, Any]]],
) -> list[dict[str, Any]]:
    return [
        data
        for event, data in frames
        if event == "progress" and data.get("step") == "version-created"
    ]


def _require_terminal(frames: list[tuple[str, dict[str, Any]]]) -> None:
    """Assert the stream ended in a terminal frame; on an ``error``
    frame, fail with the loop's OWN structured reason verbatim (a real
    model/render failure is information, reported — not a suite defect
    to tune away)."""
    terminal = [event for event, _ in frames]
    assert "done" in terminal or "error" in terminal, (
        f"no terminal frame in {len(frames)} frames: {frames!r} — the "
        "stream must end in done or error (the terminal-frame contract "
        "the SPA relies on)"
    )
    if "error" in terminal:
        err = next(data for event, data in frames if event == "error")
        raise AssertionError(
            f"design loop exhausted (real model/render failure — reported "
            f"verbatim, NOT a suite defect): reason={err.get('reason')!r} "
            f"message={err.get('message')!r}"
        )


def _load_mesh_from_frame(frames: list[tuple[str, dict[str, Any]]]) -> trimesh.Trimesh:
    """Load the version-created frame's STL (decodable, the real
    render's bytes)."""
    vc_frames = _version_created_frames(frames)
    assert vc_frames, f"no version-created frame: {frames!r}"
    stl_uri = vc_frames[0].get("stl_data_uri")
    assert stl_uri, (
        f"version-created frame carries no stl_data_uri: {vc_frames[0]!r} — "
        "the frame must carry the best candidate's rendered STL (the #84/"
        "#89 invariant: the frame's STL is the real render's bytes, not a "
        "hand-built copy of a fixture and not a dead tempdir path)"
    )
    payload = _b64_payload(stl_uri)
    assert payload, "the STL payload is empty (dead path — the #89 invariant)"
    import io

    mesh = trimesh.load(io.BytesIO(payload), file_type="stl", process=False)
    # merge_vertices() (narrower than process=True): OpenSCAD's STL export
    # emits per-facet duplicated vertices; the render worker uses the same
    # load+merge pair (see d33d.render_worker.render_for_design_loop).
    mesh.merge_vertices()
    return mesh


# ---------------------------------------------------------------------------
# The live cases
# ---------------------------------------------------------------------------


def test_live_single_part_20mm_cube(tmp_path: Path) -> None:
    """The decisive case: a single-part request through the real path
    end to end (real route, real loop, real LLM, real Docker worker,
    real SSE stream consumed). Invariants on the geometry the model
    ACTUALLY produced:

    - a version row is created in the ``versions`` table, and the frame
      and the DB agree on it — #93 (a pass must materialise a version;
      the version row must exist for a frame that claims one).
    - a ``version-created`` progress frame carries a DECODABLE STL —
      #84/#89 (the frame's STL is the real render's bytes, not a
      hand-built base64 copy of a fixture and not a dead tempdir path).
    - the mesh is watertight and its bbox is 20x20x20 within the
      existing tolerance (``max(1%, 0.5mm)``) — #80 (the MODEL's output
      is measured; it used to be the reference fixture) and #91 (a
      stated dimension is measured, not fabricated/abstained-away).
    """
    with case_timer("live_cube_20mm"):
        app = _start_lifespan(_build_live_app(tmp_path))

        async def _drive() -> tuple[int, httpx.Response]:
            async with _client(app) as client:
                new_pid = await _create_project(client, "live-cube-20mm")
                chat = await _post_chat(
                    client,
                    new_pid,
                    "Create a 20mm cube. Width 20mm, depth 20mm, height 20mm.",
                )
                return new_pid, chat

        pid, chat_resp = asyncio.new_event_loop().run_until_complete(_drive())
        try:
            assert chat_resp.status_code == 202, (
                f"chat route returned {chat_resp.status_code}: {chat_resp.text} "
                "— the real chat route accepts the message and returns 202"
            )
            frames = _wait_for_terminal(app, pid, 600.0)
            _require_terminal(frames)

            vc_frames = _version_created_frames(frames)
            version_id = int(vc_frames[0]["version_id"])
            # A version row exists in the versions table — the #93 invariant
            # (read BEFORE the connection is closed in the finally).
            row = app.state.versions.get_version(pid, version_id)
            latest = app.state.versions.latest_version(pid)
            assert row is not None, (
                f"version row {version_id} does not exist in the versions table — "
                "the frame claims a version the DB does not have (#93: a "
                "version row must exist for a passing loop)"
            )
            assert latest is not None and int(latest["id"]) == version_id, (
                f"latest version row {latest!r} != frame version {version_id} — "
                "the DB and the frame disagree (#93)"
            )

            mesh = _load_mesh_from_frame(frames)
            assert bool(mesh.is_watertight), (
                f"the model's rendered STL is not watertight "
                f"(is_watertight={mesh.is_watertight}) — the deterministic "
                "gate is the authority on printability (#80 class: the "
                "geometry is measured, never assumed)"
            )
            extents = tuple(float(x) for x in mesh.extents)
            for axis, actual in zip("xyz", extents):
                assert _pv.dimension_error_ok(actual, 20.0), (
                    f"bbox axis {axis}={actual:.3f}mm is outside the existing "
                    f"tolerance of 20mm (max(1%, 0.5mm)={max(0.2, 0.5):.2f}mm) — "
                    "the model's stated dimension was not produced (#80: the "
                    "model's output is measured; #91: the stated dimension is "
                    "measured, not fabricated)"
                )
        finally:
            if app.state.conn is not None:
                app.state.conn.close()


def test_live_multi_part_bbox_x_extent(tmp_path: Path) -> None:
    """The multi-part case ("a 20mm cube with a 10mm sphere next to it")
    — the case that was broken until #100 and that no committed test
    covered.

    THE DECISIVE ASSERTION IS ON THE BOUNDING BOX, not the component
    count. "Next to" is ambiguous between touching and separated, and a
    touching pair may merge into one connected component after
    merge_vertices; ``len(components) >= 1`` is satisfied by any
    non-empty mesh — including a lone cube with no sphere at all —
    which is exactly the failure this ticket exists to catch. Instead:
    a 20mm cube with a 10mm sphere beside it has an x-extent of ~30mm
    (cube x∈[0,20], sphere reaching to x≈30); a lone cube is 20mm. The
    x-extent assertion proves BOTH bodies are present regardless of
    whether they touch, merge, or stand apart — the #100 gap review's
    conclusion (bbox-based, not component-count-based).

    The component count is REPORTED (informational), never asserted —
    a count assertion may be added only after the separation is observed
    holding across ≥3 live runs (never assert something unobserved).
    """
    with case_timer("live_multipart_bbox"):
        app = _start_lifespan(_build_live_app(tmp_path))

        async def _drive() -> tuple[int, httpx.Response]:
            async with _client(app) as client:
                new_pid = await _create_project(client, "live-multipart")
                chat = await _post_chat(
                    client,
                    new_pid,
                    "Create a 20mm cube with a 10mm sphere next to it. The "
                    "cube is 20mm wide, 20mm deep and 20mm tall; the sphere "
                    "has a 10mm diameter and sits beside the cube along the "
                    "x-axis.",
                )
                return new_pid, chat

        pid, chat_resp = asyncio.new_event_loop().run_until_complete(_drive())
        try:
            assert chat_resp.status_code == 202, (
                f"chat route returned {chat_resp.status_code}: {chat_resp.text}"
            )
            frames = _wait_for_terminal(app, pid, 600.0)
        finally:
            if app.state.conn is not None:
                app.state.conn.close()

        _require_terminal(frames)
        mesh = _load_mesh_from_frame(frames)
        extents = tuple(float(x) for x in mesh.extents)
        x_extent = extents[0]

        stated_x = 30.0
        tol_ok_30 = _pv.dimension_error_ok(x_extent, stated_x)
        # A lone cube's x-extent is 20mm; a sphere beside it pushes the
        # x-extent at least the sphere's radius (5mm) beyond the cube —
        # well outside the 20mm tolerance band. This branch catches the
        # "sphere vanished" failure even if the model places the sphere
        # with a gap (x-extent > 30mm).
        clearly_beyond_cube = x_extent > 20.0 + max(0.02 * 20.0, 0.5)
        assert tol_ok_30 or clearly_beyond_cube, (
            f"multi-part x-extent={x_extent:.3f}mm does not prove BOTH "
            f"bodies are present: it must be within tolerance of 30mm "
            f"(touching sphere) OR clearly beyond the lone-cube 20mm "
            f"tolerance band ({20.0 + max(0.02 * 20.0, 0.5):.2f}mm). A lone "
            f"cube (x=20mm) or a vanished sphere is exactly this failure "
            f"(#100: bbox-based, not component-count-based)"
        )

        # REPORT (not assert) the component count — see docstring.
        n_components = len(mesh.split(append=False))
        print(
            f"[live multipart] x_extent={x_extent:.3f}mm "
            f"y_extent={extents[1]:.3f}mm z_extent={extents[2]:.3f}mm "
            f"components={n_components} (reported, not asserted — #100 "
            f"bbox-based rationale in docstring)"
        )


def test_live_request_words_reach_the_prompt(tmp_path: Path) -> None:
    """The #97 invariant: the request's OWN WORDS appear in the prompt
    actually SENT to the model.

    #97 closed the defect where the user's current message was forwarded
    only to the failures.jsonl archive and never rendered in the design
    prompt — the model never saw what was asked and invented unrelated
    geometry. The check is a RECORDING WRAPPER around
    ``d33d.design_llm.send``: it records the outgoing design-role
    messages and DELEGATES to the real send (a recorder, never a
    replacement — the real LLM is still called, the key still stays in
    the factory).
    """
    with case_timer("live_request_words_97"):
        recorder = RecordedPrompts()
        _install_recording_send(recorder)
        try:
            app = _start_lifespan(_build_live_app(tmp_path))

            async def _drive() -> tuple[int, httpx.Response]:
                async with _client(app) as client:
                    new_pid = await _create_project(client, "live-97")
                    chat = await _post_chat(
                        client,
                        new_pid,
                        "A 25mm wide by 15mm deep by 10mm tall rectangular "
                        "block — exactly those dimensions.",
                    )
                    return new_pid, chat

            pid, chat_resp = asyncio.new_event_loop().run_until_complete(_drive())
            try:
                assert chat_resp.status_code == 202, (
                    f"chat route returned {chat_resp.status_code}: {chat_resp.text}"
                )
                frames = _wait_for_terminal(app, pid, 600.0)
            finally:
                if app.state.conn is not None:
                    app.state.conn.close()
        finally:
            _uninstall_recording_send()

        _require_terminal(frames)

        sent_text = _all_sent_text(recorder)
        assert sent_text, (
            "the recording wrapper captured no design-role messages — the "
            "real LLM call path was not exercised (a silent stub would "
            "exactly reproduce the #97 defect: the model never saw the "
            "request)"
        )
        # The #97 invariant: the request's own words must reach the prompt
        # actually sent. The request line is rendered VERBATIM by
        # _design_messages ("Request: <message>"), so the full request
        # string must be present (case-sensitive) in the recorded text.
        assert "A 25mm wide by 15mm deep by 10mm tall rectangular block" in sent_text, (
            f"the request's own words do not appear verbatim in the prompt "
            f"actually sent to the model (#97: the user's request must be "
            f"rendered in the design prompt, not just archived to "
            f"failures.jsonl). Recorded text (truncated): {sent_text[:400]!r}"
        )
        # The dimensions must also be individually present (belt: a
        # summarised/paraphrased request would drop at least one).
        for word in ("25mm", "15mm", "10mm"):
            assert word in sent_text, (
                f"the request dimension {word!r} is not in the prompt "
                f"actually sent (#97). Recorded text (truncated): "
                f"{sent_text[:400]!r}"
            )


def test_live_version_frame_carries_real_stl(tmp_path: Path) -> None:
    """The #84/#89 invariant isolated: the ``version-created`` frame's
    STL is the REAL render's bytes — decodable, non-empty, a valid mesh.
    Not a hand-built base64 copy of a static fixture (the #84 pattern
    the mocked Playwright specs use) and not a dead tempdir path (the
    #89 bug: the harvested STL lived in a torn-down TemporaryDirectory).
    """
    with case_timer("live_version_stl_84_89"):
        app = _start_lifespan(_build_live_app(tmp_path))

        async def _drive() -> tuple[int, httpx.Response]:
            async with _client(app) as client:
                new_pid = await _create_project(client, "live-stl")
                chat = await _post_chat(
                    client,
                    new_pid,
                    "Create a 15mm cube. Width 15mm, depth 15mm, height 15mm.",
                )
                return new_pid, chat

        pid, chat_resp = asyncio.new_event_loop().run_until_complete(_drive())
        try:
            assert chat_resp.status_code == 202, (
                f"chat route returned {chat_resp.status_code}: {chat_resp.text}"
            )
            frames = _wait_for_terminal(app, pid, 600.0)
        finally:
            if app.state.conn is not None:
                app.state.conn.close()

        _require_terminal(frames)
        mesh = _load_mesh_from_frame(frames)
        assert len(mesh.vertices) > 0, (
            "the frame's STL decodes to an empty mesh (#89: the frame's "
            "STL must be the real render's bytes, non-empty)"
        )
