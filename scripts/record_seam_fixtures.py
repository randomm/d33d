#!/usr/bin/env python3
"""Record the issue #102 wire fixtures from REAL runs.

Each seam's payload is captured by driving the REAL consumer boundary
(``d33d.design_llm.send`` for SEAM A, ``render_for_design_loop`` for
SEAM B, the loop's record construction for SEAM C, the SSE adapter's
frame construction for SEAM D) with the EXTERNAL edges stubbed:

- SEAM A: the LLM HTTP edge (``request_factory``) is stubbed to return
  the recorded wire bytes (both real production shapes — T0 native
  tool_call with a JSON-STRING ``arguments`` [#80], T0 fenced-JSON-in-
  content [#79]). No live LLM, no network.
- SEAM B: the Docker edge (``subprocess.run``) is stubbed to plant the
  committed ``box_20mm.stl`` fixture bytes (the issue #84/#87 pattern —
  real worker pipeline, real ``classify`` table, real trimesh load).
  No Docker.
- SEAM C: a REAL ``run_design_loop`` run with a stubbed LLM/render edge
  (the loop's own record construction, ``_scad_from_result`` included).
- SEAM D: the REAL ``run_design_loop_with_events`` adapter with a stubbed
  loop closure (the adapter's own frame construction,
  ``_artifact_bytes_from_path`` included).

Machine-specific values are normalised before commit (the durable
``render_artifact_dir`` under ``$HOME`` → ``<RECORD_TMP>``; the render
key uuid8 → a fixed value); the normalisation list is written into each
fixture's provenance.

Usage:  uv run python scripts/record_seam_fixtures.py --seam A|B|C|D|all
        [--out tests/fixtures/e2e/]
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

STL_FIXTURE = REPO_ROOT / "tests" / "fixtures" / "stl" / "box_20mm.stl"
SCAD_SOURCE = "W = 20;\nH = 25;\nD = 30;\ncube([W, H, D]);\n"
RENDER_KEY = "deadbeef"  # normalised from a uuid4 hex8

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
    import datetime

    return datetime.datetime.now(datetime.UTC).date().isoformat()


def _provenance(model: str | None, endpoint: str | None, extra: dict | None = None) -> dict:
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
# SEAM A — the real send() over a stubbed HTTP edge
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


def _design_messages_wire() -> list[dict[str, Any]]:
    return [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "Request: make a 20x25x30 box"},
                {
                    "type": "image_url",
                    "image_url": {"url": "data:image/png;base64,REFPHOTO"},
                },
            ],
        }
    ]


def record_seam_a(out: Path) -> dict:
    from d33d.design_llm import send
    from tests.fixtures.e2e import llm_result_from_payload


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

    def _result_payload(result) -> dict[str, Any]:
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

    # Round-trip check: rebuild through the loader's own helper and
    # re-extract via the loop's own extractor — the recorded payload must
    # survive the round trip byte-for-byte (the replay test asserts the
    # same thing against the committed fixture).
    from d33d.design_loop import _scad_from_result

    for result in (native_result, fenced_result):
        rebuilt = llm_result_from_payload(_result_payload(result))
        assert _scad_from_result(rebuilt) == SCAD_SOURCE

    native_payload = _result_payload(native_result)
    fenced_payload = _result_payload(fenced_result)
    fixture = {
        "seam": "A",
        "provenance": _provenance(
            model="stub (T0 native + T0 fenced-JSON shapes, recorded via live send())",
            endpoint="stubbed request_factory (no live endpoint)",
            extra={
                "normalised": [
                    ("request_body messages (replaced with role + '…(normalised)' — "
                    "no photo/SCAD content in the committed fixture)")
                ]
            },
        ),
        "payload": {"native": native_payload, "fenced": fenced_payload},
        "expected": {"scad": SCAD_SOURCE},
    }
    out = out / "A.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


# ---------------------------------------------------------------------------
# SEAM B — the real render_for_design_loop over a stubbed Docker edge
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


def record_seam_b(out: Path) -> dict:
    import d33d.render_worker as rw

    renders_root = Path.home() / "d33d" / "record-seam-b"
    tmp_root = Path.home() / "d33d" / "record-seam-b-tmp"
    os.environ["D33D_RENDER_TMP"] = str(tmp_root)
    try:
        from unittest import mock

        with mock.patch.object(rw.subprocess, "run", _stub_docker), mock.patch.object(
            rw, "new_render_name", lambda: f"render-{RENDER_KEY}"
        ):
            result = rw.render_for_design_loop(
                SCAD_SOURCE, {}, renders_dir=renders_root
            )
    finally:
        os.environ.pop("D33D_RENDER_TMP", None)

    assert result.error_class == "ok", f"render did not classify ok: {result}"
    assert result.render_artifact_dir is not None

    # The durable artifacts must be present (the worker persisted them).
    art_dir = Path(result.render_artifact_dir)
    stl_bytes = (art_dir / "model.stl").read_bytes()
    assert stl_bytes == STL_FIXTURE.read_bytes()

    # The wire shape is result.json (to_dict); render_artifact_dir rides
    # alongside (it is on the returned object, not in result.json).
    # Normalise the durable $HOME paths (the machine-specific absolute
    # paths, the uuid8 render key, and the tempdir csg path) to
    # <RECORD_TMP>/<key>/… — the csg is normalised to the tempdir shape
    # (it is NOT persisted, so its path is a dead tempdir path: that is
    # the documented worker contract, not a secret).
    import re as _re

    art_key = f"<RECORD_TMP>/{RENDER_KEY}"

    def _rebase(p: str | None, tmp_prefix: str) -> str | None:
        if p is None:
            return None
        # Durable: <home>/d33d/record-seam-b/<uuid8>/<name> → <RECORD_TMP>/<key>/<name>
        m = _re.match(r"^.*record-seam-b/[^/]+/(.+)$", p)
        if m:
            return f"{art_key}/{m.group(1)}"
        # Temp csg: <home>/d33d/record-seam-b-tmp/tmpXXXX/out/<name> → <RECORD_TMP>/tmpXXXX/out/<name>
        m = _re.match(r"^.*record-seam-b-tmp/(tmp[^/]+/out/.+)$", p)
        if m:
            return f"{art_key}-tmp/{m.group(1)}"
        return p

    wire = result.to_dict()
    payload = {
        **wire,
        "render_artifact_dir": art_key,
        "artifacts": {
            "stl": _rebase(wire.get("artifacts", {}).get("stl"), art_key),
            "csg": _rebase(wire.get("artifacts", {}).get("csg"), art_key),
            "views": [_rebase(v, art_key) for v in wire.get("artifacts", {}).get("views", [])],
        },
        # Top-level stl/views (the re-pointed durable paths, post-#87).
        "stl": _rebase(result.stl, art_key),
        "views": [_rebase(v, art_key) for v in result.views],
        "csg": _rebase(result.csg, art_key),
        "artifacts_b64": {
            "model.stl": b64(stl_bytes),
            "views": {
                name: b64((art_dir / name).read_bytes())
                for name, _cam in rw.VIEWS
            },
        },
    }
    # The committed box_20mm.stl fixture is a 20x20x20 box (the bbox is
    # 20x20x20, not 20x25x30 — the SCAD's stated dims are a different
    # shape than the fixture). The expected bbox must match the actual
    # fixture geometry.
    import trimesh as _trimesh

    _mesh = _trimesh.load(str(STL_FIXTURE), process=False)
    _mesh.merge_vertices()
    _b = _mesh.bounds
    expected_bbox = {
        "x": float(_b[1, 0] - _b[0, 0]),
        "y": float(_b[1, 1] - _b[0, 1]),
        "z": float(_b[1, 2] - _b[0, 2]),
    }
    fixture = {
        "seam": "B",
        "provenance": _provenance(
            model="openscad (render-worker:local)",
            endpoint="docker (stubbed subprocess.run; no live Docker)",
            extra={
                "normalised": [
                    ("render_artifact_dir/stl/views: the durable $HOME path is "
                    "rebased to <RECORD_TMP>/<render_key>/… (the per-render uuid8 "
                    f"key is fixed to {RENDER_KEY!r} for reproducibility); the csg "
                    "tempdir path is rebased to <RECORD_TMP>/<key>-tmp/… (the csg is "
                    "never persisted — a dead tempdir path is the documented worker "
                    "contract, not a secret)")
                ]
            },
        ),
        "payload": payload,
        "expected": {
            "error_class": "ok",
            "bbox": expected_bbox,
            "stl_is_committed_fixture": True,
        },
    }
    out = out / "B.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
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

    # A bbox_fn that matches the 20x25x30 box (the committed fixture's
    # real extents).
    from d33d.design_loop import BboxInfo

    def bbox_fn(r):
        return BboxInfo(x=20.0, y=25.0, z=30.0, volume=15000.0)

    async def llm_fn(role, messages, system):
        return llm_result

    async def render_fn(scad, defines):
        return render

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 25.0, 30.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
        request="make a 20x25x30 box",
        max_iterations=1,
    )

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
    out = out / "C.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
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

    def bbox_fn(r):
        return BboxInfo(x=20.0, y=25.0, z=30.0, volume=15000.0)

    async def llm_fn(role, messages, system):
        return llm_result

    async def render_fn(scad, defines):
        return render

    result = run_design_loop(
        photo="data:image/png;base64,REF",
        stated_dims=(20.0, 25.0, 30.0),
        render_fn=render_fn,
        llm_fn=llm_fn,
        bbox_fn=bbox_fn,
        request="make a 20x25x30 box",
        max_iterations=1,
    )
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

    # Guard: the loop must have passed (the real loop core ran against the
    # recorded render: ok + 20x25x30 bbox + named params) and the adapter
    # must have built the version-created frame with the REAL on-disk
    # durable bytes. An exhausted result would mean the loop core regressed
    # — record loudly instead of writing a misleading fixture.
    assert result.status == "pass", f"loop did not pass: {result.status}"
    vc = [f for f in frames if f[0] == "progress" and f[1].get("step") == "version-created"]
    assert vc, f"no version-created frame: {frames}"
    assert frames[-1][0] == "done", f"no terminal done: {frames}"
    assert "stl_data_uri" in vc[0][1], "stl_data_uri missing (dead durable path?)"
    assert "views" in vc[0][1], "views missing (dead durable path?)"

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
            "terminal": "done",
            "version_created": True,
        },
    }
    out = out / "D.json"
    out.write_text(json.dumps(fixture, indent=2, sort_keys=False) + "\n", encoding="utf-8")
    return fixture


SEAMS = {"A": record_seam_a, "B": record_seam_b, "C": record_seam_c, "D": record_seam_d}


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
    args = parser.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    seams = list(SEAMS) if args.seam == "all" else [args.seam]
    # C/D depend on A+B being present.
    if "C" in seams or "D" in seams:
        for dep in ("A", "B"):
            if dep not in seams and not (out / f"{dep}.json").is_file():
                seams.insert(0, dep)

    for seam in seams:
        print(f"recording SEAM {seam} -> {out}", file=sys.stderr)
        SEAMS[seam](out)
        print(f"  SEAM {seam}: written {out / f'{seam}.json'}", file=sys.stderr)


if __name__ == "__main__":
    main()
