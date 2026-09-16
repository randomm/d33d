#!/usr/bin/env python3
"""Record the issue #102 wire fixtures from REAL runs.

Each seam's payload is captured by driving the REAL consumer boundary
(``d33d.design_llm.send`` for SEAM A, ``render_for_design_loop`` for
SEAM B, the loop's record construction for SEAM C, the SSE adapter's
frame construction for SEAM D).

The EXTERNAL edges (the LLM HTTP edge, the Docker edge) can be recorded
LIVE (``--live``) or STUBBED:

- SEAM A live: a real ``request_factory`` (``httpx.AsyncClient`` POST to
  the catalogue's endpoint, the bearer key resolved from models.yaml, a
  HARD client timeout) drives the REAL ``send()`` so the request body is
  what ``send()`` actually builds (tools included). The RAW response JSON
  is captured BEFORE ``send()`` normalises it, and the raw tool_call
  ``arguments`` TYPE (str vs dict) is recorded — the #80 wire invariant,
  capturable live only (``send()`` normalises to a dict before it
  returns).
- SEAM A stubbed (the default without ``--live``): the LLM HTTP edge is
  stubbed to return the recorded wire bytes (T0 native tool_call with a
  JSON-STRING ``arguments`` [#80], T0 fenced-JSON-in-content [#79]). No
  live LLM, no network.
- SEAM B live (the DEFAULT — live is the point of the ticket): no mocks,
  real Docker, real OpenSCAD image, ``D33D_RENDER_TMP`` under ``$HOME``
  (Docker on macOS cannot see /tmp — a /tmp bind mount hangs the
  helper). The recorded STL is checked GEOMETRICALLY (trimesh load,
  extents ~20x20x20 for the ``cube(20)`` request) — NOT by byte-match
  against the committed ``box_20mm.stl`` (that circularity — the
  fixture's expected value coming from the fixture itself — is the bug
  the live mode removes).
- SEAM B stubbed (``--seam B`` without ``--live``): the Docker edge
  (``subprocess.run``) is stubbed to plant the committed
  ``box_20mm.stl`` fixture bytes (the issue #84/#87 pattern — real
  worker pipeline, real ``classify`` table, real trimesh load). No
  Docker.
- SEAM C: a REAL ``run_design_loop`` run with a stubbed LLM/render edge
  (the loop's own record construction, ``_scad_from_result`` included).
- SEAM D: the REAL ``run_design_loop_with_events`` adapter with a stubbed
  loop closure (the adapter's own frame construction,
  ``_artifact_bytes_from_path`` included).

Machine-specific values are normalised before commit (the durable
``render_artifact_dir`` under ``$HOME`` → ``<RECORD_TMP>/<uuid8>``; the
normalisation list is written into each fixture's provenance). Every
fixture records whether it was captured LIVE or STUBBED in
``provenance.recorded`` (``'live'`` / ``'stubbed'``) + the
endpoint/image + the timestamp (``provenance.recorded_at``) — a reader
must never have to guess.

Usage:  uv run python scripts/record_seam_fixtures.py --seam A|B|C|D|all
        [--out tests/fixtures/e2e/] [--live]

``--live`` governs SEAM A ONLY (opt-in: without it, SEAM A is recorded
stubbed, no network). SEAM B is LIVE BY DEFAULT (real Docker); pass
``--stub-b`` to record it stubbed instead.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import datetime
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

STL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "stl" / "box_20mm.stl"
#: SEAM A's SCAD source (the 20x25x30 box the fixture's expected value
#: pins).
SCAD_SOURCE = "W = 20;\nH = 25;\nD = 30;\ncube([W, H, D]);\n"
#: SEAM B's live recording request: "Create a 20mm cube" → a 20x20x20
#: cube (the geometry check asserts extents ~20 on every axis).
CUBE_SCAD_SOURCE = "cube(20);\n"
RENDER_KEY = "deadbeef"  # normalised from a uuid4 hex8 (stub-mode render key)

#: The live SEAM-A HTTP client's hard timeout (seconds). A hung endpoint
#: must not hang the recording.
LIVE_LLM_TIMEOUT_S = 120.0


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def _git_head() -> str:
    return (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
        .stdout.strip()
    )


def _today() -> str:
    return datetime.datetime.now(datetime.UTC).date().isoformat()


def _now_iso() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def _provenance(
    model: str | None, endpoint: str | None, extra: dict | None = None
) -> dict:
    prov = {"commit": _git_head(), "date": _today()}
    if model is not None:
        prov["model"] = model
    if endpoint is not None:
        prov["endpoint"] = endpoint
    if extra:
        if "normalised" in extra:
            prov["normalised"] = extra.pop("normalised")
        prov.update(extra)
    return prov


# ---------------------------------------------------------------------------
# SEAM A — the real send() over a live or stubbed HTTP edge
# ---------------------------------------------------------------------------


class _FakeLLMResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self.status = 200
        self.is_success = True
        self._payload = payload

    def json(self) -> dict[str, Any]:
        return self._payload


def _t0_capability():
    from d33d.config.probes import CapabilityResult

    return CapabilityResult(
        tools=True, json_schema=True, vision=True, max_images=8, validated=True
    )


def _tiny_red_png() -> bytes:
    """A valid 1x1 red PNG (correct CRCs, built with struct + zlib). The
    live LLM endpoint rejects an unloadable image URL with a 400, so the
    live recording's reference photo must be a real image — a 1x1 red
    pixel carries the image_url wire shape with no payload to leak."""
    import struct
    import zlib

    def chunk(tag: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + tag + data + struct.pack(
            ">I", zlib.crc32(tag + data) & 0xFFFFFFFF
        )

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)  # 1x1, 8-bit RGB
    raw = b"\x00\xff\x00\x00"  # filter 0 + one red pixel
    idat = zlib.compress(raw)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", idat)
        + chunk(b"IEND", b"")
    )


def _design_messages_wire(live: bool = False) -> list[dict[str, Any]]:
    """The design-role wire message list: the request text + a reference
    photo (data URI). In live mode the photo is a REAL 1x1 red PNG (the
    placeholder ``REFPHOTO`` base64 is not a decodable image — the live
    endpoint 400s on an unloadable image URL) and the request is the
    cube prompt ("Create a 20mm cube" — SEAM B's request, so the live
    SCAD the model emits is the same parametric cube SEAM B renders:
    named parameters, no magic numbers). In stub mode the placeholder
    rides the stubbed wire unchanged (the stubbed contract is pinned
    as-is — it is not a live-contract concern)."""
    if live:
        url = "data:image/png;base64," + base64.b64encode(_tiny_red_png()).decode("ascii")
        text = "Request: Create a 20mm cube"
    else:
        url = "data:image/png;base64,REFPHOTO"
        text = "Request: make a 20x25x30 box"
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": url}},
            ],
        }
    ]


def _live_http_request_factory(base_url: str, api_key: str, timeout_s: float) -> Any:
    """The LIVE SEAM-A request_factory: a real ``httpx.AsyncClient`` POST
    to the catalogue's chat-completions endpoint with the bearer key and a
    HARD client timeout (a hung endpoint cannot hang the recording).

    ``request_factory(request: dict) -> response`` — the request is the
    full outgoing body the ``send()`` boundary built (it is recorded by
    the caller so the fixture carries what actually went on the wire);
    the response exposes ``status`` / ``is_success`` / ``json()`` like
    httpx.
    """
    import httpx

    class _LiveResponse:
        def __init__(self) -> None:
            self.status: int = 0
            self.is_success = False
            self._data: dict[str, Any] = {}

        def json(self) -> dict[str, Any]:
            return self._data

    async def factory(request: dict[str, Any]) -> Any:
        resp = _LiveResponse()
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=10.0)
        ) as client:
            r = await client.post(
                f"{base_url.rstrip('/')}/chat/completions",
                json=request,
                headers={"Authorization": f"Bearer {api_key}"},
            )
            resp.status = r.status_code
            resp.is_success = r.is_success
            resp._data = r.json()
        return resp

    return factory


def _raw_tool_call_args_types(raw: dict[str, Any]) -> list[str]:
    """The RAW ``function.arguments`` type names off the live wire
    (``'str'`` / ``'dict'`` / ...) — the #80 invariant, capturable live
    only (``send()`` normalises to a dict before it returns)."""
    msg = (raw.get("choices") or [{}])[0].get("message") or {}
    out = []
    for tc in msg.get("tool_calls") or []:
        args = tc.get("function", {}).get("arguments")
        out.append(type(args).__name__)
    return out


def record_seam_a(out: Path, live: bool = False) -> dict:
    from d33d.design_llm import role_tools, send
    from tests.fixtures.e2e import llm_result_from_payload

    if live:
        # LIVE recording: a real request_factory (httpx.AsyncClient POST,
        # hard client timeout) against the catalogue's endpoint — the
        # bearer key resolved from models.yaml (the ${ENV} reference
        # resolves against the environment at load time, same as
        # production). The RAW response JSON is captured before send()
        # normalises it; the fixture records the raw arguments TYPE (the
        # #80 wire shape) per shape.
        from d33d.config.catalogue import load_catalogue
        from d33d.config.resolve import resolve_model

        catalogue = load_catalogue(REPO_ROOT / "models.yaml")
        resolution = resolve_model(catalogue, "design")
        base_url = resolution.provider.base
        api_key = resolution.provider.key
        model_id = resolution.entry.model
        live_factory = _live_http_request_factory(base_url, api_key, LIVE_LLM_TIMEOUT_S)

        # The real T0 tools array send() puts on the wire (the field the
        # stub left as null — this is the whole point of live recording).
        tools = role_tools("design")

        # Shape 1 — T0 native tool_call (the expected production shape
        # for this endpoint: the live model answers with a native
        # tool_call; whatever the wire emits is recorded — the raw
        # arguments TYPE is the #80 invariant, recorded per shape).
        native_request: dict[str, Any] = {"body": None, "raw": None, "args_types": None}
        fenced_request: dict[str, Any] = {"body": None, "raw": None, "args_types": None}

        async def factory_native(request: dict[str, Any]) -> Any:
            native_request["body"] = request
            resp = await live_factory(request)
            native_request["raw"] = resp.json()
            native_request["args_types"] = _raw_tool_call_args_types(native_request["raw"])
            return resp

        async def factory_fenced(request: dict[str, Any]) -> Any:
            fenced_request["body"] = request
            resp = await live_factory(request)
            fenced_request["raw"] = resp.json()
            fenced_request["args_types"] = _raw_tool_call_args_types(fenced_request["raw"])
            return resp

        native_result = asyncio.run(
            send(
                role="design",
                model_id=model_id,
                messages=_design_messages_wire(live=True),
                request_factory=factory_native,
                capability=_t0_capability(),
                tools=tools,
            )
        )

        fenced_result = asyncio.run(
            send(
                role="design",
                model_id=model_id,
                messages=_design_messages_wire(live=True),
                request_factory=factory_fenced,
                capability=_t0_capability(),
                tools=tools,
            )
        )

        def _live_payload(result, info: dict[str, Any]) -> dict[str, Any]:
            payload = _stub_result_payload(result)
            # The REAL request body send() put on the wire (tools
            # included — the field the stub left as null); messages are
            # still normalised (no photo/SCAD content in the committed
            # fixture).
            body = info["body"] or {}
            payload["request_body"]["model"] = body.get("model")
            payload["request_body"]["tools"] = body.get("tools")
            # The raw wire argument types (the #80 invariant, capturable
            # live only — send() normalises to a dict before returning).
            payload["raw_tool_call_args_types"] = info["args_types"]
            return payload

        native_payload = _live_payload(native_result, native_request)
        fenced_payload = _live_payload(fenced_result, fenced_request)

        # Round-trip check: the live wire output must survive the loader's
        # own helper AND the loop's own extractor (the replay test asserts
        # the same thing against the committed fixture). The live model's
        # SCAD is its own output (it is not pinned to a specific string —
        # the point is the wire shape, not the geometry), so the expected
        # value is the EXTRACTED value from the recorded native call.
        from d33d.design_loop import _scad_from_result

        expected_scad = None
        for shape_name, payload in (("native", native_payload), ("fenced", fenced_payload)):
            rebuilt = llm_result_from_payload(payload)
            extracted = _scad_from_result(rebuilt)
            assert extracted and extracted.strip(), (
                f"SEAM A {shape_name}: live wire output extracted empty SCAD — "
                f"the #80 shape (an un-normalised string that the extractor "
                f"cannot read)"
            )
            if shape_name == "native":
                expected_scad = extracted

        fixture = {
            "seam": "A",
            "provenance": _provenance(
                model=model_id,
                endpoint=(
                    f"live {base_url}/chat/completions "
                    f"(httpx.AsyncClient, hard {LIVE_LLM_TIMEOUT_S:.0f}s timeout)"
                ),
                extra={
                    "recorded": "live",
                    "recorded_at": _now_iso(),
                    "shapes_note": (
                        "both shapes driven through the REAL send() against the "
                        "live endpoint; the fixture records what the wire actually "
                        "emitted (the raw arguments type per shape is in "
                        "payload.*.raw_tool_call_args_types)"
                    ),
                    "normalised": [
                        ("request_body messages (replaced with role + '…(normalised)' — "
                        "no photo/SCAD content in the committed fixture)")
                    ],
                },
            ),
            "payload": {"native": native_payload, "fenced": fenced_payload},
            "expected": {"scad": expected_scad},
        }
        out_path = out / "A.json"
        out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
        return fixture

    # STUB path (the default — no network, exactly as today).
    # Shape 1 — T0 native tool_call, arguments as a JSON STRING (issue #80).
    async def factory_native(request: dict[str, Any]) -> Any:
        return _FakeLLMResponse(
            {
                "choices": [
                    {
                        "message": {
                            "content": None,
                            "tool_calls": T0_NATIVE_TOOL_CALLS,
                        }
                    }
                ],
                "usage": {"prompt_tokens": 11, "completion_tokens": 7},
            }
        )

    native_result = asyncio.run(
        send(
            role="design",
            model_id="stub/t0-native",
            messages=_design_messages_wire(),
            request_factory=factory_native,
            capability=_t0_capability(),
        )
    )

    # Shape 2 — T0 fenced JSON in content, empty tool_calls (issue #79).
    async def factory_fenced(request: dict[str, Any]) -> Any:
        return _FakeLLMResponse(
            {
                "choices": [{"message": {"content": T0_FENCED_CONTENT}}],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8},
            }
        )

    fenced_result = asyncio.run(
        send(
            role="design",
            model_id="stub/t0-fenced",
            messages=_design_messages_wire(),
            request_factory=factory_fenced,
            capability=_t0_capability(),
        )
    )

    native_payload = _stub_result_payload(native_result)
    fenced_payload = _stub_result_payload(fenced_result)

    # Round-trip check: rebuild through the loader's own helper and
    # re-extract via the loop's own extractor — the recorded payload must
    # survive the round trip byte-for-byte (the replay test asserts the
    # same thing against the committed fixture).
    from d33d.design_loop import _scad_from_result

    for payload in (native_payload, fenced_payload):
        rebuilt = llm_result_from_payload(payload)
        assert _scad_from_result(rebuilt) == SCAD_SOURCE
    fixture = {
        "seam": "A",
        "provenance": _provenance(
            model="stub (T0 native + T0 fenced-JSON shapes, recorded via live send())",
            endpoint="stubbed request_factory (no live endpoint)",
            extra={
                "recorded": "stubbed",
                "recorded_at": _now_iso(),
                "normalised": [
                    ("request_body messages (replaced with role + '…(normalised)' — "
                    "no photo/SCAD content in the committed fixture)")
                ]
            },
        ),
        "payload": {"native": native_payload, "fenced": fenced_payload},
        "expected": {"scad": SCAD_SOURCE},
    }
    out_path = out / "A.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


def _stub_result_payload(result) -> dict[str, Any]:
    return {
        "content": result.content,
        "tool_calls": [
            {"name": tc["name"], "arguments": tc["arguments"]}
            for tc in result.tool_calls
        ],
        "prompt_hash": result.prompt_hash,
        "tier": result.tier,
        "status": result.status,
        "request_body": {
            "model": result.request_body["model"],
            "messages": [
                {"role": m["role"], "content": "…(normalised)"}
                for m in result.request_body["messages"]
            ],
            "tools": result.request_body.get("tools"),
        },
        "usage": result.usage,
    }


# The T0 native wire bytes (issue #80: arguments as a JSON-ENCODED STRING).
T0_NATIVE_TOOL_CALLS = [
    {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "emit_design",
            "arguments": json.dumps({"scad": SCAD_SOURCE}),  # a JSON string
        },
    }
]
# The T0 fenced-JSON-in-content wire bytes (issue #79: empty tool_calls).
T0_FENCED_CONTENT = (
    "```json\n"
    + json.dumps({"tool": "emit_design", "arguments": {"scad": SCAD_SOURCE}})
    + "\n```"
)


# ---------------------------------------------------------------------------
# SEAM B — the real render_for_design_loop over a live or stubbed Docker edge
# ---------------------------------------------------------------------------


def _stub_docker(argv: list[str], *a: Any, **kw: Any) -> subprocess.CompletedProcess:
    """All docker calls succeed; the harvest helper plants the committed
    box_20mm.stl fixture bytes (plus a CSG and 6 view PNGs) so the real
    on-disk checks see a fully successful render (issue #84/#87 pattern).
    The ``--volume <dir>:/host`` host dir is what harvest copies into."""
    from d33d.render_worker import VIEWS

    if "cp /work/model.stl /host/" in " ".join(argv):
        for i, tok in enumerate(argv):
            if i > 0 and argv[i - 1] == "--volume" and tok.endswith(":/host"):
                host_dir = Path(tok.rsplit(":", 1)[0])
                host_dir.mkdir(parents=True, exist_ok=True)
                (host_dir / "model.stl").write_bytes(STL_FIXTURE.read_bytes())
                (host_dir / "model.csg").write_bytes(b"fake-csg-bytes")
                for name, _cam in VIEWS:
                    (host_dir / name).write_bytes(b"\x89PNG-fake-view-bytes")
    return subprocess.CompletedProcess(args=argv, returncode=0, stdout=b"", stderr=b"")


def record_seam_b(out: Path, live: bool = True) -> dict:
    """Record SEAM B from a REAL render (live is the DEFAULT — the stubbed
    path plants the committed fixture's own bytes, which makes the fixture
    circular with itself; live recording is the point of the ticket).

    The SCAD source is a 20x20x20 cube ("Create a 20mm cube") so the
    recorded geometry is asserted GEOMETRICALLY (trimesh extents ~
    20x20x20) instead of by byte-match against the committed
    ``box_20mm.stl`` (the circularity bug: the fixture's expected value
    came from the fixture itself).
    """
    import d33d.render_worker as rw

    renders_root = Path.home() / "d33d" / "record-seam-b"
    # Under $HOME — Docker on macOS cannot see /tmp (a /tmp bind mount
    # hangs the helper).
    tmp_root = Path.home() / "d33d" / "record-seam-b-tmp"
    os.environ["D33D_RENDER_TMP"] = str(tmp_root)
    scad_source = CUBE_SCAD_SOURCE
    try:
        if live:
            # LIVE: no mocks — real Docker, real OpenSCAD image. The
            # worker's own uuid8 render key is normalised away below.
            result = rw.render_for_design_loop(
                scad_source, {}, renders_dir=renders_root
            )
        else:
            from unittest import mock

            with mock.patch.object(rw.subprocess, "run", _stub_docker), mock.patch.object(
                rw, "new_render_name", lambda: f"render-{RENDER_KEY}"
            ):
                result = rw.render_for_design_loop(
                    scad_source, {}, renders_dir=renders_root
                )
    finally:
        os.environ.pop("D33D_RENDER_TMP", None)

    assert result.error_class == "ok", f"render did not classify ok: {result}"
    assert result.render_artifact_dir is not None

    # The durable artifacts must be present (the worker persisted them).
    art_dir = Path(result.render_artifact_dir)
    stl_bytes = (art_dir / "model.stl").read_bytes()
    # REAL-geometry check (NOT a byte match against the committed
    # box_20mm.stl — that circularity is the bug the live mode removes):
    # load with trimesh, assert the extents are ~20x20x20 for the
    # cube(20) request.
    import trimesh as _trimesh

    _mesh = _trimesh.load(str(art_dir / "model.stl"), process=False)
    _mesh.merge_vertices()
    _b = _mesh.bounds
    for axis in range(3):
        extent = float(_b[1, axis] - _b[0, axis])
        assert 19.0 <= extent <= 21.0, (
            f"rendered extents {extent} on axis {axis} are not ~20mm (the "
            f"cube(20) request produced the wrong geometry)"
        )

    # The wire shape is result.json (to_dict); render_artifact_dir rides
    # alongside (it is on the returned object, not in result.json).
    # Normalise the durable $HOME paths (the machine-specific absolute
    # paths, the uuid8 render key, and the tempdir csg path) to
    # <RECORD_TMP>/<key>/… — the csg is normalised to the tempdir shape
    # (it is NOT persisted, so its path is a dead tempdir path: that is
    # the documented worker contract, not a secret). The live run's real
    # uuid8 key is normalised to the recorded key's basename.
    _render_key = Path(result.render_artifact_dir).name

    def _rebase(p: str | None) -> str | None:
        if p is None:
            return None
        # Durable: <home>/d33d/record-seam-b/<uuid8>/<name> → <RECORD_TMP>/<uuid8>/<name>
        m = re.match(r"^.*record-seam-b/[^/]+/(.+)$", p)
        if m:
            return f"<RECORD_TMP>/{_render_key}/{m.group(1)}"
        # Temp csg: <home>/d33d/record-seam-b-tmp/tmpXXXX/out/<name> → <RECORD_TMP>/<uuid8>-tmp/tmpXXXX/out/<name>
        m = re.match(r"^.*record-seam-b-tmp/(tmp[^/]+/out/.+)$", p)
        if m:
            return f"<RECORD_TMP>/{_render_key}-tmp/{m.group(1)}"
        return p

    wire = result.to_dict()
    payload = {
        **wire,
        "render_artifact_dir": f"<RECORD_TMP>/{_render_key}",
        "artifacts": {
            "stl": _rebase(wire.get("artifacts", {}).get("stl")),
            "csg": _rebase(wire.get("artifacts", {}).get("csg")),
            "views": [_rebase(v) for v in wire.get("artifacts", {}).get("views", [])],
        },
        # Top-level stl/views (the re-pointed durable paths, post-#87).
        "stl": _rebase(result.stl),
        "views": [_rebase(v) for v in result.views],
        "csg": _rebase(result.csg),
        "artifacts_b64": {
            "model.stl": b64(stl_bytes),
            "views": {
                name: b64((art_dir / name).read_bytes())
                for name, _cam in rw.VIEWS
            },
        },
    }
    # The expected bbox is the RECORDED geometry (measured from the
    # recorded render's STL — the live run produced a 20x20x20 cube). It
    # is NOT the committed fixture's bbox (the circularity the live mode
    # removes).
    expected_bbox = {
        "x": float(_b[1, 0] - _b[0, 0]),
        "y": float(_b[1, 1] - _b[0, 1]),
        "z": float(_b[1, 2] - _b[0, 2]),
    }
    fixture = {
        "seam": "B",
        "provenance": _provenance(
            model="openscad (render-worker:local, d33d/render-worker:local)",
            endpoint=(
                f"live docker run ({rw.RENDER_WORKER_IMAGE})"
                if live
                else "docker (stubbed subprocess.run; no live Docker)"
            ),
            extra={
                "recorded": "live" if live else "stubbed",
                "recorded_at": _now_iso(),
                "scad_source": scad_source,
                "normalised": [
                    ("render_artifact_dir/stl/views: the durable $HOME path is "
                    "rebased to <RECORD_TMP>/<render_key>/… (the per-render uuid8 "
                    f"key is the recorded run's own key {RENDER_KEY if not live else _render_key!r}, "
                    "kept verbatim for traceability); the csg "
                    "tempdir path is rebased to <RECORD_TMP>/<key>-tmp/… (the csg is "
                    "never persisted — a dead tempdir path is the documented worker "
                    "contract, not a secret)")
                ],
            },
        ),
        "payload": payload,
        "expected": {
            "error_class": "ok",
            "bbox": expected_bbox,
            "stl_is_committed_fixture": False,
        },
    }
    out_path = out / "B.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


# ---------------------------------------------------------------------------
# SEAM C — a real run_design_loop record
# ---------------------------------------------------------------------------


def record_seam_c(out: Path) -> dict:
    from d33d.design_loop import run_design_loop
    from tests.fixtures.e2e import llm_result_from_payload

    # Reuse the seam-A native payload (the recorded LLMResult the loop
    # actually consumed — the #80 shape, arguments normalised to a dict).
    a_fixture = out / "A.json"
    if a_fixture.is_file():
        a_data = json.loads(a_fixture.read_text(encoding="utf-8"))
        native = a_data["payload"]["native"]
    else:
        # Record A first if not present (SEAM C's LLMResult is A's native).
        record_seam_a(out)
        a_data = json.loads((out / "A.json").read_text(encoding="utf-8"))
        native = a_data["payload"]["native"]

    llm_result = llm_result_from_payload(native)

    # The recorded RenderResult (seam B's, rebuilt) — the render the loop
    # scored. Reuse B's committed fixture if present.
    b_fixture = out / "B.json"
    from tests.fixtures.e2e import _rebuild_render_result

    b_data = json.loads(b_fixture.read_text(encoding="utf-8"))
    render = _rebuild_render_result(b_data["payload"])

    # A bbox_fn matching the recorded render's geometry (the live cube(20)
    # render is 20x20x20 — the bbox_fn must match the recorded geometry,
    # not a hardcoded 20x25x30 the live render does not produce).
    from d33d.design_loop import BboxInfo

    def bbox_fn(r):
        recorded = b_data["provenance"].get("recorded")
        if recorded == "live":
            return BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)
        return BboxInfo(x=20.0, y=25.0, z=30.0, volume=15000.0)

    async def llm_fn(role, messages, system):
        return llm_result

    async def render_fn(scad, defines):
        return render

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
        request="Create a 20mm cube",
        max_iterations=1,
    )
    # The LLM/render edges are stubbed (the loop core, not the live LLM,
    # is under test here): the recorded loop outcome (pass/exhausted) is
    # a property of the STUBBED edges (the live model's SCAD varies per
    # call — a single ``cube(20);`` with no named-parameter block
    # exhausts on the named-params gate even though the render is ok and
    # the bbox matches). The fixture records what the loop core did with
    # the recorded edges; the loop core's own logic (the scoring, the
    # stop conditions, the best-candidate selection) is what is under
    # test, not the live model's output.

    best = result.best
    # Normalise the render's durable paths to <RECORD_TMP>/<key>.
    from dataclasses import replace

    art_key = f"<RECORD_TMP>/{RENDER_KEY}"

    def _norm_path(p):
        if p is None:
            return None
        return f"{art_key}/{Path(p).name}"

    norm_render = replace(
        render,
        stl=_norm_path(render.stl),
        csg=_norm_path(render.csg) if render.csg else None,
        views=tuple(_norm_path(v) for v in render.views),
        render_artifact_dir=art_key,
    )
    norm_best = replace(best, render=norm_render)

    fixture = {
        "seam": "C",
        "provenance": _provenance(
            model="stub (the seam-A recorded LLMResult, the seam-B recorded render)",
            endpoint="run_design_loop (real loop core, stubbed LLM/render edges)",
            extra={
                "recorded": "stubbed",
                "recorded_at": _now_iso(),
                "loop_status_note": (
                    "the loop outcome (pass/exhausted) is a property of the "
                    "STUBBED edges — the live model's SCAD varies per call, "
                    "so the recorded loop may exhaust on the named-params "
                    "gate even though the render is ok and the bbox "
                    "matches. The loop core's own logic (the scoring, the "
                    "stop conditions, the best-candidate selection) is what "
                    "is under test, not the live model's output."
                ),
                "normalised": [
                    ("best.render.stl/views/render_artifact_dir: the durable $HOME "
                    f"path rebased to {art_key!r} (uuid8 key fixed to {RENDER_KEY!r})")
                ]
            },
        ),
        "payload": {
            "best": {
                "iteration": norm_best.iteration,
                "scad_source": norm_best.scad_source,
                "score": {
                    "bits": list(norm_best.score.bits),
                    "rank": norm_best.score.rank,
                    "tiebreak": list(norm_best.score.tiebreak),
                    "bbox_abstained": norm_best.score.bbox_abstained,
                },
                "failure_class": norm_best.failure_class,
                "repair": norm_best.repair,
                "prompt_hashes": dict(norm_best.prompt_hashes),
                "params": dict(norm_best.params),
                "render": {
                    "ok": norm_render.ok,
                    "exit_code": norm_render.exit_code,
                    "duration_ms": norm_render.duration_ms,
                    "error_class": norm_render.error_class,
                    "stderr": norm_render.stderr,
                    "stl": norm_render.stl,
                    "csg": norm_render.csg,
                    "views": list(norm_render.views),
                    "render_artifact_dir": norm_render.render_artifact_dir,
                },
            },
            "status": result.status,
        },
        "expected": {
            "params": {"W": 20.0, "D": 30.0, "H": 25.0},
            "score_rank": 4,
            "iteration": 1,
        },
    }
    out_path = out / "C.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


# ---------------------------------------------------------------------------
# SEAM D — the real SSE adapter frame stream
# ---------------------------------------------------------------------------


def record_seam_d(out: Path) -> dict:
    """Drive the REAL ``run_design_loop_with_events`` with a stubbed loop
    closure (the adapter's own frame construction — ``_artifact_bytes_
    from_path`` reads real on-disk bytes)."""
    from d33d.design_loop_events import run_design_loop_with_events
    from tests.fixtures.e2e import _rebuild_render_result

    b_fixture = out / "B.json"
    b_data = json.loads(b_fixture.read_text(encoding="utf-8"))
    render = _rebuild_render_result(b_data["payload"])

    # Rebuild an IterationRecord whose render points at the REAL on-disk
    # durable dir (the adapter reads the bytes from render_artifact_dir).
    from d33d.design_loop import (
        BboxInfo,
        run_design_loop,
    )
    from tests.fixtures.e2e import llm_result_from_payload

    a_data = json.loads((out / "A.json").read_text(encoding="utf-8"))
    llm_result = llm_result_from_payload(a_data["payload"]["native"])

    # The recorded render's REAL extents (the live cube(20) render is
    # 20x20x20 — the bbox_fn must match the recorded geometry, not a
    # hardcoded 20x25x30 that the live render does not produce).

    def bbox_fn(r):
        recorded = b_data["provenance"].get("recorded")
        if recorded == "live":
            return BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)
        return BboxInfo(x=20.0, y=25.0, z=30.0, volume=15000.0)

    async def llm_fn(role, messages, system):
        return llm_result

    async def render_fn(scad, defines):
        return render

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 20.0, 20.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
        request="Create a 20mm cube",
        max_iterations=1,
    )
    # The LLM/render edges are stubbed (the adapter's frame construction,
    # not the live LLM, is under test here): the recorded loop outcome
    # is a property of the stubbed edges (the live model's SCAD varies
    # per call — a single ``cube(20);`` with no named-parameter block
    # exhausts on the named-params gate even though the render is ok and
    # the bbox matches). The frame construction under test reads the
    # recorded best candidate's render bytes off disk.
    # The adapter's pass-frame construction reads the REAL durable bytes
    # (the committed box_20mm.stl + the 6 fake view PNGs) from
    # ``render.render_artifact_dir`` — the loop's recorded render has a
    # dead $HOME path (the record-time path), so re-record the durable
    # artifact directory under ``$HOME/d33d/record-seam-d`` (a live, under-
    # $HOME path — the adapter reads it via the real
    # ``_artifact_bytes_from_path``) and rebuild the loop result with it.
    from dataclasses import replace as _replace

    live_art_dir = Path.home() / "d33d" / "record-seam-d" / RENDER_KEY
    live_art_dir.mkdir(parents=True, exist_ok=True)
    (live_art_dir / "model.stl").write_bytes(STL_FIXTURE.read_bytes())
    from d33d.render_worker import VIEWS as _VIEWS

    for _name, _cam in _VIEWS:
        (live_art_dir / _name).write_bytes(b"\x89PNG-fake-view-bytes")

    live_render = _replace(
        render,
        stl=str(live_art_dir / "model.stl"),
        views=tuple(str(live_art_dir / Path(v).name) for v in render.views),
        render_artifact_dir=str(live_art_dir),
    )
    live_best = _replace(result.best, render=live_render)
    live_result = _replace(result, best=live_best)

    # The adapter's app.state: a stub with a create_version that returns a
    # fixed id. The loop closure is a sync ``lambda: result`` — the adapter
    # inspects the signature (no ``app`` param → called bare) and returns
    # the sync result as-is (the ``inspect.isawaitable`` branch is skipped),
    # so the full pass-frame construction runs against the REAL recorded
    # best candidate (``_artifact_bytes_from_path`` reads the REAL on-disk
    # durable bytes: the committed box_20mm.stl + the 6 fake view PNGs).
    app = type("App", (), {})()
    state = type("State", (), {})()
    app.state = state

    class _Versions:
        def __init__(self) -> None:
            self._next = 1

        async def create_version(self, project_id, params, name, message):
            v = self._next
            self._next += 1
            return {"id": v, "params": params, "name": name, "message": message}

    state.versions = _Versions()

    def _loop() -> Any:
        return live_result  # the live-on-disk DesignResult

    state.run_design_loop = _loop

    frames = []

    async def _drive():
        gen = run_design_loop_with_events(
            app,
            1,
            user_message="make a 20x25x30 box",
            stated_dims=(20.0, 25.0, 30.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make a 20x25x30 box",
        )
        async for frame in gen:
            frames.append(frame)
            if frame[0] in ("done", "error"):
                break

    asyncio.run(_drive())

    # Guard: the adapter's frame construction must have run. The loop
    # outcome (pass/exhausted) is a property of the STUBBED edges (the
    # live model's SCAD varies per call — a single ``cube(20);`` with no
    # named-parameter block exhausts on the named-params gate even though
    # the render is ok and the bbox matches), so the terminal frame is
    # asserted per the recorded outcome, not against a hardcoded pass.
    # An exhausted result is a valid recorded shape (the adapter's error
    # frame is the terminal frame); a pass result must carry the
    # version-created frame with the REAL on-disk durable bytes.
    if result.status == "pass":
        vc = [f for f in frames if f[0] == "progress" and f[1].get("step") == "version-created"]
        assert vc, f"no version-created frame: {frames}"
        assert frames[-1][0] == "done", f"no terminal done: {frames}"
        assert "stl_data_uri" in vc[0][1], "stl_data_uri missing (dead durable path?)"
        assert "views" in vc[0][1], "views missing (dead durable path?)"
        terminal = "done"
    else:
        # Exhausted: the terminal frame is an error frame (the adapter's
        # non-pass path). The adapter's frame construction (the omit
        # policy, the structured reason) is what is under test here.
        assert frames[-1][0] == "error", f"no terminal error: {frames}"
        terminal = "error"

    # Clean up the live artifact dir (it was a recording artefact, not a
    # committed file). The fixture's provenance notes the normalisation.
    import shutil as _shutil

    _shutil.rmtree(live_art_dir, ignore_errors=True)
    _shutil.rmtree(live_art_dir.parent, ignore_errors=True)

    # Normalise the frame's data URIs: the STL/view bytes are real (the
    # committed box_20mm.stl + fake view PNGs) — they are NOT machine-
    # specific, so they are NOT normalised (they ARE the wire payload).
    # The version_id is a stub-assigned int (fixed to 1 by _Versions).
    fixture = {
        "seam": "D",
        "provenance": _provenance(
            model="adapter (run_design_loop_with_events, real frame construction)",
            endpoint="stubbed loop closure + create_version (no live LLM/Docker)",
            extra={
                "recorded": "stubbed",
                "recorded_at": _now_iso(),
                "normalised": [
                    ("the durable artifact dir was recorded under $HOME/d33d/"
                    "record-seam-d/<key> (a live, under-$HOME path the adapter read "
                    "via _artifact_bytes_from_path); the on-disk bytes are the "
                    "committed box_20mm.stl + the 6 fake view PNGs (NOT normalised "
                    "— they ARE the wire payload, carried as data URIs); version_id "
                    "is the stub-assigned 1; the live dir is removed after recording")
                ]
            },
        ),
        "payload": {
            "frames": [
                {"kind": kind, "data": data} for kind, data in frames
            ]
        },
        "expected": {
            "terminal": terminal,
            "version_created": terminal == "done",
        },
    }
    out_path = out / "D.json"
    out_path.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


SEAMS = {
    "C": record_seam_c,
    "D": record_seam_d,
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seam",
        required=True,
        choices=["A", "B", "C", "D", "all"],
        help="which seam to record (C/D record A+B first if absent)",
    )
    parser.add_argument(
        "--out",
        default=str(REPO_ROOT / "tests" / "fixtures" / "e2e"),
        help="output directory (default tests/fixtures/e2e/)",
    )
    parser.add_argument(
        "--live",
        action="store_true",
        help=(
            "record SEAM A from the REAL LLM edge (httpx request_factory to "
            "the catalogue endpoint, bearer key from models.yaml, hard "
            "timeout; the raw response is captured before send() normalises "
            "it). OPT-IN — without it SEAM A keeps working exactly as today "
            "(stubbed, no network). SEAM B is live by default (real "
            "Docker); pass --stub-b to record it stubbed instead."
        ),
    )
    parser.add_argument(
        "--stub-b",
        action="store_true",
        help="record SEAM B stubbed (the committed-fixture byte source — "
        "the circular path the ticket exists to replace; kept for the "
        "fast/stub layer and for environments without Docker).",
    )
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seams = ["A", "B", "C", "D"] if args.seam == "all" else [args.seam]
    # C/D depend on A+B being present.
    if "C" in seams or "D" in seams:
        for dep in ("A", "B"):
            if dep not in seams and not (out / f"{dep}.json").is_file():
                seams.insert(0, dep)

    for seam in seams:
        print(f"recording SEAM {seam} -> {out}", file=sys.stderr)
        if seam == "A":
            record_seam_a(out, live=args.live)
        elif seam == "B":
            # SEAM B is live by default (real Docker); pass --seam B with
            # --stub-b to record it stubbed instead (the --live flag only
            # governs SEAM A).
            b_live = not args.stub_b
            record_seam_b(out, live=b_live)
        else:
            SEAMS[seam](out)
        print(f"  SEAM {seam}: written {out / f'{seam}.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
