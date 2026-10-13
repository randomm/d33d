"""Issue #433 (regression): the stated FEATURE size (``a 5 mm hole``) reaches
the design prompt through the REAL production closure.

The pre-fix defect: ``d33d.app._build_production_design_loop``'s ``_loop``
forwarded an explicit kwarg list to the hook that omitted
``stated_features``; the design-state block and the ``Stated feature:``
prompt line were therefore built from ``None``. The chat-side tests stub
the loop above the closure, so they never saw the drop.

This test drives the PRODUCTION wrapper through the UNSTUBBED hook and the
real ``run_design_loop_async``; the LLM factory records every request body
so the assertion reads what the model actually received.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from d33d.design_loop import BboxInfo
from d33d.render_worker import RenderResult
from d33d.statement_extraction import StatedFeature

#: A 20x20x20 cube with a hole of diameter 8 (the model's own choice — the
#: user stated 5). The bbox matches the render, so the only thing that can
#: differ is the feature comparison.
_SCAD_HOLE_8 = (
    "hole_diameter = 8;\n"
    "difference() {\n  cube([20, 20, 20]);\n"
    "  translate([10, 10, -1]) cylinder(h = 22, d = hole_diameter);\n}\n"
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.is_success = True

    def json(self) -> dict[str, Any]:
        return self._payload


def _recording_factory(bodies: list[str]):
    """The closure's ``_http_request_factory(base, key)`` edge, stubbed:
    records every request body and answers with the hole-8 SCAD."""
    fenced = "```json\n" + json.dumps(
        {"tool": "emit_design", "arguments": {"scad": _SCAD_HOLE_8}}
    ) + "\n```"

    def _make(_base: str, _key: str):
        async def _factory(body: dict[str, Any]) -> Any:
            bodies.append(json.dumps(body))
            return _FakeResponse({"choices": [{"message": {"content": fenced}}]})

        return _factory

    return _make


def _run_closure(tmp_path: Path, monkeypatch: Any, **extra: Any) -> tuple[Any, list[str]]:
    import d33d.app as app_mod
    import d33d.config.catalogue as cat_mod
    import d33d.config.probes as probes_mod
    import d33d.config.resolve as resolve_mod
    import d33d.db as db_mod
    from d33d.app import _build_production_design_loop
    from d33d.config.catalogue import Catalogue, ModelEntry, Provider
    from d33d.config.probes import CapabilityResult
    from d33d.config.resolve import RoleResolution

    db_path = str(tmp_path / "d33d.sqlite3")
    db_mod.connect(db_path).close()
    _db_path = Path(db_path)  # class-body scope cannot read the local

    provider = Provider(name="stub", base="http://stub", key="stub")
    entry = ModelEntry(id="design", provider="stub", model="stub-model")
    catalogue = Catalogue(
        source=Path("/dev/null"),
        providers={"stub": provider},
        models={"design": entry},
        roles={"design": "design"},
    )
    resolution = RoleResolution(role="design", entry=entry, provider=provider)
    capability = CapabilityResult(
        tools=False, json_schema=False, vision=False, max_images=0,
        fenced_json=True, validated=True,
    )
    bbox = BboxInfo(x=20.0, y=20.0, z=20.0, volume=8000.0)
    ok_render = RenderResult(
        ok=True, exit_code=0, duration_ms=10, error_class="ok", stderr="",
        stl="model.stl", csg="model.csg",
        views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
    )

    def _render_fn(_scad, _defines, **_kw: Any) -> RenderResult:
        return ok_render

    class _StubAppState:
        catalogue_path = Path("/dev/null")
        db_path = _db_path
        failures_jsonl_path = Path("/dev/null")

    bodies: list[str] = []
    monkeypatch.setattr(app_mod, "render_for_design_loop", _render_fn)
    monkeypatch.setattr(cat_mod, "load_catalogue", lambda _p: catalogue)
    monkeypatch.setattr(resolve_mod, "resolve_model", lambda _c, _r, **_k: resolution)

    async def _probe(*_a, **_k):
        return capability

    monkeypatch.setattr(probes_mod, "probe_capabilities", _probe)
    monkeypatch.setattr(app_mod, "_http_request_factory", _recording_factory(bodies))

    wrapper = _build_production_design_loop()
    result = asyncio.run(
        wrapper(
            type("App", (), {"state": _StubAppState()})(),
            photo="data:image/png;base64,REF",
            chat_history=(),
            stated_dims=(20.0, 20.0, 20.0),
            render_fn=_render_fn,
            llm_fn=None,
            bbox_fn=lambda r: bbox if r.error_class == "ok" else None,
            request="add a 5 mm hole",
            **extra,
        )
    )
    return result, bodies


def test_stated_feature_reaches_design_prompt_and_state_block(
    tmp_path: Path, monkeypatch: Any
) -> None:
    """Through the production closure: the user's ``a 5 mm hole`` is in the
    design prompt as a ``Stated feature:`` line, AND the design-state block
    the prompt carries flags ``hole_diameter`` as the user's stated 5 vs the
    model's made 8 (the Brief's ``disagrees`` row) — one fact, two surfaces.
    """
    feats = [StatedFeature(noun="hole", role=None, value=5.0)]
    _result, bodies = _run_closure(
        tmp_path,
        monkeypatch,
        stated_features=feats,
        state_params={"hole_diameter": 8.0},
        state_meta=None,
    )
    assert bodies, "the design role must have been called"
    body = json.loads(bodies[0])
    text = "\n".join(str(m.get("content", "")) for m in body["messages"])
    assert "Stated feature: hole = 5 mm" in text
    # The design-state line for the hole's diameter is the disagreement row
    # (the user stated it, the model made 8 — flagged, not silently kept).
    row = next(line for line in text.splitlines() if "hole_diameter" in line)
    assert "8" in row and "you stated this" in row, row


def test_finalize_route_carries_and_persists_stated_feature(app_with_versions):
    """The FINALIZE seam (issue #433): a finalize message ``add a 5 mm hole``
    reaches the loop as the ``stated_features`` kwarg AND is persisted to
    ``projects.carried_stated_features`` (the next chat turn's carry). The
    pre-fix finalize path never asserted either."""
    from tests.versioning.helpers import create_project, run_async

    captured: dict[str, Any] = {}

    from tests.versioning.test_design_loop_finalize import _StubResult

    class _Res(_StubResult):
        def __init__(self) -> None:
            super().__init__("pass", {"hole_diameter": 5.0})

    def _fake_loop(app: Any, **kwargs: Any) -> Any:  # ``app`` selects the kwargs call
        captured.update(kwargs)
        return _Res()

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = _fake_loop
        await client.post(
            f"/api/projects/{pid}/finalize",
            json={"name": "the bracket", "message": "add a 5 mm hole"},
        )
        row = app_with_versions.state.conn.get_project(pid)
        return row

    row = run_async(app_with_versions, _call)
    feats = captured.get("stated_features")
    assert feats is not None, "finalize must hand stated_features to the loop"
    assert [(f.noun, f.value) for f in feats] == [("hole", 5.0)]
    assert row.get("carried_stated_features") is not None
    assert "hole" in row["carried_stated_features"]


def test_reply_mismatch_is_the_brief_comparison(app_with_versions):
    """The done-frame mismatch list is built from the version's design-state
    block (the Brief's inputs): a made 8 against a stated 5 is one record; a
    made 5 (within tolerance) and a missing version yield none."""
    from d33d.design_loop_events import _feature_mismatches_for_version
    from d33d.statement_extraction import StatedFeature
    from tests.versioning.helpers import create_project, run_async

    feats = [StatedFeature(noun="hole", role=None, value=5.0)]

    app = app_with_versions

    async def _call(client):
        pid = (await create_project(client))["id"]
        v8 = await app.state.versions.create_version(pid, {"hole_diameter": 8.0})
        v5 = await app.state.versions.create_version(pid, {"hole_diameter": 5.0})
        return (
            _feature_mismatches_for_version(app, pid, v8["id"], feats),
            _feature_mismatches_for_version(app, pid, v5["id"], feats),
            _feature_mismatches_for_version(app, pid, None, feats),
        )

    made8, within, none_version = run_async(app, _call)
    assert made8 == [
        {"noun": "hole", "stated_mm": 5.0, "made_mm": 8.0, "label": "hole_diameter"}
    ]
    assert within == []
    assert none_version == []
