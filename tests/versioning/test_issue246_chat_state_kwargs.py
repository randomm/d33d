"""Issue #246 (chat-path fix): ``run_design_loop_with_events`` passes the
design-state block's inputs (``state_params`` / ``state_bbox`` /
``state_stated``) to the design loop on the CHAT path — the same values
the finalize path's ``_finalize_loop_kwargs`` passes — so the live chat
prompt, the finalize prompt and the GET the SPA reads all render the SAME
``Current design state`` block via ``state_block_for_version``.

The pre-existing defect: the chat adapter built its loop kwargs with
photo/chat_history/stated_dims/render_fn/llm_fn/bbox_fn/request/on_progress
+ design_source but NEVER the three state inputs, so the live chat prompt's
block was always empty even when a version with params/bbox/stated_dims
existed. The region-edit route goes through the same adapter and inherits
the fix.

All fast (stub loop, no LLM, no Docker).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Any

from tests.versioning.helpers import (
    create_project,
    create_version,
    run_async,
)

#: A distinctive model-emitted parameter that is NOT an axis name: its
#: row must render the ``assumed`` provenance mark (issue #246: no
#: model-emitted parameter is ever promoted to ``stated``).
BORE = "bore_diameter"


def _exhausted_stub_result(params: dict[str, Any]):
    """An exhausted-style duck-type result (the adapter's non-pass path —
    no version creation, no artifact reads)."""
    from d33d.design_loop import IterationRecord, Score
    from tests.versioning.test_design_loop_finalize import _default_render

    return _ExhaustedResult(params, _default_render(), IterationRecord, Score)


class _ExhaustedResult:
    def __init__(self, params: dict[str, Any], render, IterationRecord, Score) -> None:
        self.status = "exhausted"
        self.best = IterationRecord(
            iteration=0,
            scad_source="W = 30;\ncube([W, W, W]);",
            render=render,
            score=Score(bits=(False,)*5, rank=0, tiebreak=(False,)*5),
            params=dict(params),
        )
        self.failure_reason = "bbox_out_of_tolerance"


def _capturing_loop(captured: dict[str, Any]):
    """A production-seam-shaped stub loop (takes ``app``) that captures
    the loop kwargs and returns an exhausted result (no version write)."""

    async def _loop(app: Any, **kwargs: Any) -> Any:
        captured.update(kwargs)
        return _exhausted_stub_result(kwargs.get("state_params") or {})

    return _loop


def _drive_adapter(app, pid: int, run_loop):
    """Drive ``run_design_loop_with_events`` for one project; the call
    MUST be inside a lifespan (``_call`` inside ``run_async``). Returns
    a coroutine; ``await`` it."""
    from d33d.design_loop_events import run_design_loop_with_events

    async def _drive():
        app.state.run_design_loop = run_loop
        async for _ in run_design_loop_with_events(
            app,
            pid,
            user_message="make it a cube",
            stated_dims=(30.0, 30.0, 30.0),
            chat_history=(),
            photo="data:image/png;base64,REF",
            request_text="make it a cube",
        ):
            pass

    return _drive()


def test_chat_adapter_passes_state_kwargs_for_project_with_version(app_with_versions):
    """A project whose latest version has params/bbox/stated_dims: the
    loop kwargs the CHAT adapter builds carry ``state_params`` /
    ``state_bbox`` / ``state_stated`` equal to the latest version's values
    (mirroring ``_finalize_loop_kwargs`` exactly)."""
    captured: dict[str, Any] = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "D": 25.0, "H": 20.0, BORE: 8.0})
        svc = app_with_versions.state.versions
        # Persist a measurement + the run's per-axis stated set on the
        # version (the write path the design loop itself uses).
        v = await svc.create_version(
            pid,
            {"W": 30.0, "D": 25.0, "H": 20.0, BORE: 8.0},
            name="measured",
            bbox=(30.4, 25.0, 20.0),
            stated_dims={"W": 30.0, "D": 25.0},
        )
        # The adapter is a generator: reading latest_version must happen
        # DURING the run (lifespan/DB open). (The first version was
        # created via the API; the second — with the persisted bbox +
        # stated set — is what the adapter will read as the latest.)
        latest_before = svc.latest_version(pid)
        assert latest_before is not None
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
        latest = svc.latest_version(pid)
        return latest, v["id"]

    latest, _first_vid = run_async(app_with_versions, _call)

    # The version was created (the second one is the latest the adapter
    # read).
    assert latest is not None
    # The captured loop kwargs carry the design-state block's inputs.
    assert captured.get("state_params") == latest["params"]
    assert BORE in captured["state_params"]
    assert captured.get("state_bbox") == latest["bbox"] == {
        "x": 30.4,
        "y": 25.0,
        "z": 20.0,
    }
    assert captured.get("state_stated") == latest["stated_dims"] == {
        "W": 30.0,
        "D": 25.0,
    }


def test_chat_adapter_passes_through_baseline_genus_for_import(app_with_versions, tmp_path):
    """Issue #386 (final): the import-parent baseline is the STORED, REPAIRED
    part mesh's GENUS (``{repo}/versions/{v1}/part.stl``) — never the
    report's ``hole_count`` (``gaps_before + genus``, which overstates
    the baseline whenever the import had open gaps). A project with no
    part omits the kwarg (the loop defaults to 0). A part whose stored
    mesh is missing/unreadable carries the ``-1`` unknown sentinel (the
    check abstains) — never a fabricated baseline from ``hole_count``.
    """
    captured: dict[str, Any] = {}
    fixture_dir = Path(__file__).parent.parent / "fixtures" / "stl"

    def _write_stored_part(pid: int, stl_fixture: str):
        """Write the stored part mesh the import path does, plus a v1 row."""
        from tests.versioning.helpers import repo_path_for

        repo = repo_path_for(app_with_versions, pid)
        conn = app_with_versions.state.conn
        v1 = conn.raw.execute(
            "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        assert v1 is not None
        part_dir = repo / "versions" / str(v1[0])
        part_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_dir / stl_fixture, part_dir / "part.stl")

    def _make_call(set_part_report: bool | None):
        async def _call(client):
            proj = await create_project(client)
            pid = proj["id"]
            if set_part_report is None:
                # No-part project: no part columns, no version, no file.
                await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))
                return
            conn = app_with_versions.state.conn
            conn.execute(
                "UPDATE projects SET part_filename=?, part_format=?, "
                "part_unit_status=?, part_scale=?, part_report=? WHERE id=?",
                (
                    "part.stl", "stl", "settled", 1.0,
                    '{"hole_count": 2}', pid,
                ),
            )
            conn.commit()
            # Create the v1 row + write the stored (repaired) part mesh
            # (genus 0 — a watertight box; the report's hole_count is 2,
            # deliberately different, to prove the baseline is the stored
            # mesh's genus, not the report's count).
            await create_version(client, pid, {})
            _write_stored_part(pid, "box_20mm.stl")
            await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))

        return _call

    # Import project with a stored part (genus 0, hole_count 2 in the
    # report) → the kwarg is the STORED mesh's genus (0), not 2.
    captured.clear()
    run_async(app_with_versions, _make_call(True))
    assert captured.get("through_baseline_genus") == 0, (
        f"expected through_baseline_genus==0 from the stored part's genus "
        f"(the report's hole_count is 2 — never used), got "
        f"{captured.get('through_baseline_genus')!r}"
    )

    # No-part project (the fresh-project baseline) → kwarg absent.
    captured.clear()
    run_async(app_with_versions, _make_call(None))
    assert "through_baseline_genus" not in captured


def test_chat_adapter_v2_baseline_uses_parent_version_render(app_with_versions, tmp_path):
    """Issue #386 (v2+ edit case): a project with a part and an existing
    version whose ``render_artifact_dir`` carries a ``model.stl`` with a
    known genus — the loop kwargs the CHAT adapter builds carry
    ``through_baseline_genus`` equal to the PARENT VERSION's rendered
    genus (not the part report's hole_count). When the version has no
    ``render_artifact_dir`` (never measured), the kwarg falls back to the
    part report's hole_count."""
    captured: dict[str, Any] = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        conn = app_with_versions.state.conn
        # Set up the part (hole_count=2 in the report).
        conn.execute(
            "UPDATE projects SET part_filename=?, part_format=?, "
            "part_unit_status=?, part_scale=?, part_report=? WHERE id=?",
            ("part.stl", "stl", "settled", 1.0, '{"hole_count": 2}', pid),
        )
        conn.commit()

        # Create a version via the service.
        svc = app_with_versions.state.versions
        await svc.create_version(
            pid, {"W": 20.0}, name="v1", bbox=(20.0, 20.0, 20.0),
        )
        # Set up a render artifact dir with the genus-1 fixture as model.stl.
        from pathlib import Path as _Path
        fixture_dir = _Path(__file__).parent.parent / "fixtures" / "stl"
        render_dir = tmp_path / "render_v1"
        render_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_dir / "through_hole_genus1.stl", render_dir / "model.stl")
        latest = svc.latest_version(pid)
        assert latest is not None
        conn.execute(
            "UPDATE versions SET render_artifact_dir=? WHERE id=?",
            (str(render_dir), latest["id"]),
        )
        conn.commit()
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))

    run_async(app_with_versions, _call)
    # The v2+ baseline: the parent version's rendered genus (1 from the
    # fixture), NOT the part report's hole_count (2).
    assert captured.get("through_baseline_genus") == 1, (
        f"v2+ baseline should be the parent version's rendered genus "
        f"(1), got {captured.get('through_baseline_genus')!r}"
    )


def test_chat_adapter_v2_baseline_falls_back_to_stored_part_genus(app_with_versions, tmp_path):
    """Issue #386 (final, v2+ edit, parent never rendered): a project with
    a part and a version that has NO ``render_artifact_dir`` (never
    rendered). The baseline is the STORED part mesh's genus — NOT the
    report's ``hole_count`` (which overstates the baseline whenever the
    import had open gaps). The stored mesh here is a genus-1 fixture
    (``through_hole_genus1.stl``) while the report says ``hole_count`` 2:
    the kwarg must be 1."""
    captured: dict[str, Any] = {}
    fixture_dir = Path(__file__).parent.parent / "fixtures" / "stl"

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        conn = app_with_versions.state.conn
        conn.execute(
            "UPDATE projects SET part_filename=?, part_format=?, "
            "part_unit_status=?, part_scale=?, part_report=? WHERE id=?",
            ("part.stl", "stl", "settled", 1.0, '{"hole_count": 2}', pid),
        )
        conn.commit()
        # Create the v1 row (NO render_artifact_dir — never rendered).
        await create_version(client, pid, {"W": 20.0})
        # Write the stored part mesh (genus 1 — the report says 2).
        from tests.versioning.helpers import repo_path_for

        repo = repo_path_for(app_with_versions, pid)
        v1 = conn.raw.execute(
            "SELECT id FROM versions WHERE project_id = ? ORDER BY id ASC LIMIT 1",
            (pid,),
        ).fetchone()
        assert v1 is not None
        part_dir = repo / "versions" / str(v1[0])
        part_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(fixture_dir / "through_hole_genus1.stl", part_dir / "part.stl")
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))

    run_async(app_with_versions, _call)
    # No render_artifact_dir → the stored part mesh's genus (1), NOT the
    # report's hole_count (2).
    assert captured.get("through_baseline_genus") == 1, (
        f"v2+ baseline with no rendered parent should be the stored "
        f"part's genus (1), not the report's hole_count (2), got "
        f"{captured.get('through_baseline_genus')!r}"
    )


def test_chat_adapter_passes_none_state_kwargs_for_fresh_project(app_with_versions):
    """A fresh project (no version yet): the loop kwargs carry
    ``state_params`` / ``state_bbox`` / ``state_stated`` all ``None`` —
    the block renders its honest empty state, never a fabricated
    dimension."""
    captured: dict[str, Any] = {}

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await _drive_adapter(app_with_versions, pid, _capturing_loop(captured))

    run_async(app_with_versions, _call)
    assert "state_params" in captured, "the key must be present, not omitted"
    assert captured["state_params"] is None
    assert "state_bbox" in captured
    assert captured["state_bbox"] is None
    assert "state_stated" in captured
    assert captured["state_stated"] is None


def test_chat_adapter_kwargs_render_the_state_block_in_the_live_prompt():
    """Prompt-level: the design messages BUILT FROM the kwargs the chat
    adapter captures (``state_params`` / ``state_bbox`` / ``state_stated``
    for a version with an assumed model param and a stated axis) carry the
    ``Current design state`` block with the ``assumed`` mark on the model
    param and the ``stated`` mark on the stated axis row."""
    from d33d.design_loop import _design_messages

    # The exact values the chat adapter passes for a latest version with
    # these rows (mirroring ``_finalize_loop_kwargs``). ``state_bbox`` is
    # ``None`` (no persisted measurement yet) so the stated set drives
    # the axis rows without the measurement upgrade — the case the brief
    # asks for (``stated`` mark on the stated axis row, ``assumed`` mark
    # on the model param).
    state_params = {"W": 30.0, "D": 25.0, "H": 20.0, BORE: 8.0}
    state_bbox = None
    state_stated = {"W": 30.0, "D": 25.0}

    messages = _design_messages(
        photo="data:image/png;base64,REF",
        chat_history=(),
        stated=(30.0, 25.0, 20.0),
        repair=None,
        request="make it a cube",
        state_params=state_params,
        state_bbox=state_bbox,
        state_stated=state_stated,
    )
    text = next(p for p in messages[0]["content"] if p.get("type") == "text")["text"]

    assert "Current design state (mm):" in text
    # The model-emitted parameter renders the assumed mark (never stated).
    assert f"{BORE} = 8 (assumed — the user never set this)" in text
    # The stated axis row renders the stated mark (the per-axis protocol
    # evidence — the row the stated set drives, distinct from the
    # model's W param row, which the matching bbox upgrades to
    # ``measured``). Axis rows render their axis word (``Depth``), not
    # the bare letter, so the model can tell the protocol's axis from a
    # model param named ``D``.
    # Issue #316 de-dup: the W and D params (30/25) AGREE with their
    # stated axis rows (30/25, exact match) → their param rows are
    # DROPPED; the surviving W/D rows are the axis rows, which keep the
    # STRONGER provenance — the axis rows' own ``stated`` beats the
    # params' ``assumed`` (the user's words never lose to the model's
    # matching numbers). The H param (20) has no stated axis → keeps its
    # own param row (``assumed``).
    # The bore param (8) has no axis mapping → keeps its own param row.
    # The stated W/D lines MUST survive the collapse (the user said W =
    # 30 and D = 25 — the collapse must never downgrade them to the
    # model's ``assumed`` numbers).
    stated_rows = [ln for ln in text.split("\n") if ln.endswith("(stated by the user)")]
    assert stated_rows == [
        "Width (W) = 30 (stated by the user)",
        "Depth (D) = 25 (stated by the user)",
    ], f"the stated W/D lines must survive the collapse: {stated_rows}"
    # The W axis row keeps its ``stated`` provenance (never downgraded by
    # the agreeing assumed param) and carries the param's label.
    assert "Width (W) = 30 (stated by the user)" in text
    # The D axis row keeps its ``stated`` provenance (same rule).
    assert "Depth (D) = 25 (stated by the user)" in text
    # The H param row (no stated axis → not de-duped) carries ``assumed``.
    assert "H = 20 (assumed — the user never set this)" in text


def test_production_design_loop_forwards_state_stated():
    """``_build_production_design_loop``'s production wrapper forwards
    ``state_stated`` (and the sibling state inputs) to the inner loop
    hook (which hands them to ``run_design_loop_async``) — pinned with
    the inner hook stubbed (and the catalogue/probe/llm_fn pieces stubbed
    too, so no real models.yaml is required): the wrapper's ``kwargs``
    forwarding is what reaches the stub."""
    from pathlib import Path

    import d33d.app as app_mod
    from d33d.app import _build_production_design_loop
    from d33d.config.catalogue import Catalogue, ModelEntry, Provider
    from d33d.config.probes import CapabilityResult
    from d33d.config.resolve import RoleResolution
    from d33d.evals import failure_capture as fc

    # Synthetic catalogue / resolution / capability so the wrapper
    # reaches the hook call without touching a real models.yaml on disk.
    _PROVIDER = Provider(name="stub", base="http://stub", key="stub")
    _ENTRY = ModelEntry(id="design", provider="stub", model="stub-model")
    _CATALOGUE = Catalogue(
        source=Path("/dev/null"),
        providers={"stub": _PROVIDER},
        models={"design": _ENTRY},
        roles={"design": "design"},
    )
    _RESOLUTION = RoleResolution(role="design", entry=_ENTRY, provider=_PROVIDER)
    _CAPABILITY = CapabilityResult(
        tools=True, json_schema=True, vision=False, max_images=0, fenced_json=True, validated=False
    )

    def _stub_load_catalogue(_path): return _CATALOGUE
    def _stub_resolve_model(_cat, _role, **_kw): return _RESOLUTION
    async def _stub_probe_capabilities(*_a, **_k): return _CAPABILITY
    def _stub_http_request_factory(_base, _key): return (lambda *a, **k: None)

    class _StubAppState:
        catalogue_path = Path("/dev/null")
        db_path = Path("/dev/null")
        failures_jsonl_path = Path("/dev/null")

    inner: dict[str, Any] = {}

    async def _inner_hook(**kwargs: Any) -> Any:
        # ``kwargs`` here is what the inner hook forwards into
        # ``run_design_loop_async`` (the production closure pops only its
        # own model/prompt_version/request before the await).
        inner.update(kwargs)
        return None

    def _inner_hook_factory(*, path: Any = None) -> Any:
        return _inner_hook

    originals = {
        "load_catalogue": getattr(app_mod, "load_catalogue", None),
        "resolve_model": getattr(app_mod, "resolve_model", None),
        "_http_request_factory": getattr(app_mod, "_http_request_factory", None),
        "probe_capabilities": getattr(app_mod, "probe_capabilities", None),
        "fc_hook": getattr(fc, "default_run_design_loop_hook", None),
    }
    try:
        # The names the wrapper's body references (resolved via closure at
        # call time — the wrapper's body references the names in the
        # MODULE's namespace, so we patch the module attribute the wrapper
        # will look up at call time). For names the wrapper resolves via a
        # function-local ``from ... import`` (i.e. the name is bound at
        # function-entry time, not call time), we need to build a FRESH
        # wrapper AFTER the patch — which is exactly what we do below.
        if originals["load_catalogue"] is not None:
            app_mod.load_catalogue = _stub_load_catalogue
        if originals["resolve_model"] is not None:
            app_mod.resolve_model = _stub_resolve_model
        if originals["_http_request_factory"] is not None:
            app_mod._http_request_factory = _stub_http_request_factory
        # ``probe_capabilities`` is imported inside ``_build_production_
        # design_loop``'s body — patch the SOURCE module so the fresh
        # wrapper's ``from ... import`` picks up the stub. (The wrapper's
        # own function-local imports re-bind ``load_catalogue`` and
        # ``resolve_model`` too, but those are referenced at ``_loop``
        # CALL time from the module namespace, so the ``app_mod`` patch
        # suffices for them.)
        import d33d.config.probes as probes_mod
        _orig_probe = probes_mod.probe_capabilities
        probes_mod.probe_capabilities = _stub_probe_capabilities
        originals["_probe_mod"] = (probes_mod, _orig_probe)
        # Also patch the wrapper's OWN function-local import of
        # ``load_catalogue`` / ``resolve_model`` — the wrapper's body has
        # ``from d33d.config.catalogue import load_catalogue`` and
        # ``from d33d.config.resolve import resolve_model`` (function-
        # local, re-bound at wrapper-entry time). Patch the SOURCE modules
        # so the fresh wrapper's body re-imports the stubs.
        import d33d.config.catalogue as cat_mod
        import d33d.config.resolve as resolve_mod
        _orig_cat = cat_mod.load_catalogue
        _orig_res = resolve_mod.resolve_model
        cat_mod.load_catalogue = _stub_load_catalogue
        resolve_mod.resolve_model = _stub_resolve_model
        originals["_cat_mod"] = (cat_mod, _orig_cat)
        originals["_res_mod"] = (resolve_mod, _orig_res)
        fc.default_run_design_loop_hook = _inner_hook_factory
        # A FRESH wrapper built under the patched names sees the stubs.
        # The wrapper's body has function-local ``from ... import``s that
        # re-bind the names at wrapper-entry time — but ``load_catalogue``
        # and ``resolve_model`` are referenced at ``_loop`` CALL time from
        # the module's namespace (they're imported at app.py's module top
        # level, not inside ``_loop``). So patching ``app_mod.load_catalogue``
        # works. For ``probe_capabilities`` (imported inside
        # ``_build_production_design_loop``'s body), we patched the SOURCE
        # module and built a FRESH wrapper, so the fresh wrapper's body
        # re-imports the stub. Good.
        wrapper = _build_production_design_loop()

        # The wrapper's ``_wrapper(app, **kwargs)`` closes over ``_loop``
        # and calls it with ``app.state`` — the stub ``_StubAppState``
        # carries the fields ``_loop`` reads (``catalogue_path``,
        # ``db_path``, ``failures_jsonl_path``) so the wrapper's full
        # path (catalogue load → role resolve → probe → llm_fn build →
        # hook call) is exercised with stubs at every external touch.
        import asyncio

        asyncio.run(wrapper(type("App", (), {"state": _StubAppState()})(), state_stated={"W": 30.0}))
    finally:
        # Restore every patched binding.
        for name, val in originals.items():
            if name == "_probe_mod":
                mod, orig = val
                mod.probe_capabilities = orig
            elif name == "_cat_mod":
                mod, orig = val
                mod.load_catalogue = orig
            elif name == "_res_mod":
                mod, orig = val
                mod.resolve_model = orig
            elif name == "fc_hook":
                fc.default_run_design_loop_hook = val
            elif val is not None:
                setattr(app_mod, name, val)

    # The production wrapper forwarded ``state_stated`` to the inner
    # hook (which hands it to ``run_design_loop_async`` — the chat
    # path's design-state inputs must survive the production closure).
    assert inner.get("state_stated") == {"W": 30.0}


def test_production_design_loop_forwards_part_kwargs():
    """Issue #380 (regression): the production closure's hook call FORWARDS
    ``part_scale`` / ``part_bbox_mm`` (the import section's inputs — the
    adapter sets both on an import project) to the inner hook. The pre-fix
    drop: ``_loop``'s explicit kwarg list omitted both, so ``run_design_
    loop_async`` received ``None`` and the import guard (gated on
    ``part_scale is not None``) never fired on the live chat path. The
    captured-hook pattern (``fc.default_run_design_loop_hook`` monkeypatched
    BEFORE the fresh ``_build_production_design_loop()``) makes the drop
    visible: without the forward the captured dict has no ``part_scale`` /
    ``part_bbox_mm`` at all (a ``kwargs.get(...)`` absent key is ``None``
    either way — the discriminator is presence AND value)."""
    import asyncio
    from pathlib import Path

    import d33d.app as app_mod
    import d33d.config.catalogue as cat_mod
    import d33d.config.probes as probes_mod
    import d33d.config.resolve as resolve_mod
    from d33d.app import _build_production_design_loop
    from d33d.config.catalogue import Catalogue, ModelEntry, Provider
    from d33d.config.probes import CapabilityResult
    from d33d.config.resolve import RoleResolution
    from d33d.evals import failure_capture as fc

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
        tools=True, json_schema=True, vision=False, max_images=0,
        fenced_json=True, validated=False,
    )

    def _stub_load_catalogue(_path):
        return catalogue

    def _stub_resolve_model(_cat, _role, **_kw):
        return resolution

    async def _stub_probe_capabilities(*_a, **_k):
        return capability

    def _stub_http_request_factory(_base, _key):
        return (lambda *a, **k: None)

    class _StubAppState:
        catalogue_path = Path("/dev/null")
        db_path = Path("/dev/null")
        failures_jsonl_path = Path("/dev/null")

    app = type("App", (), {"state": _StubAppState()})()

    def _drive(**loop_kwargs):
        captured: dict[str, Any] = {}

        async def _inner_hook(**kwargs: Any) -> Any:
            captured.update(kwargs)
            return None

        def _inner_hook_factory(*, path: Any = None) -> Any:
            return _inner_hook

        originals = {
            "cat": cat_mod.load_catalogue,
            "res": resolve_mod.resolve_model,
            "probe": probes_mod.probe_capabilities,
            "fc_hook": fc.default_run_design_loop_hook,
            "app_lc": getattr(app_mod, "load_catalogue", None),
            "app_rm": getattr(app_mod, "resolve_model", None),
            "app_http": getattr(app_mod, "_http_request_factory", None),
        }
        try:
            cat_mod.load_catalogue = _stub_load_catalogue
            resolve_mod.resolve_model = _stub_resolve_model
            probes_mod.probe_capabilities = _stub_probe_capabilities
            if originals["app_lc"] is not None:
                app_mod.load_catalogue = _stub_load_catalogue
            if originals["app_rm"] is not None:
                app_mod.resolve_model = _stub_resolve_model
            if originals["app_http"] is not None:
                app_mod._http_request_factory = _stub_http_request_factory
            fc.default_run_design_loop_hook = _inner_hook_factory
            wrapper = _build_production_design_loop()
            asyncio.run(wrapper(app, **loop_kwargs))
        finally:
            cat_mod.load_catalogue = originals["cat"]
            resolve_mod.resolve_model = originals["res"]
            probes_mod.probe_capabilities = originals["probe"]
            fc.default_run_design_loop_hook = originals["fc_hook"]
            if originals["app_lc"] is not None:
                app_mod.load_catalogue = originals["app_lc"]
            if originals["app_rm"] is not None:
                app_mod.resolve_model = originals["app_rm"]
            if originals["app_http"] is not None:
                app_mod._http_request_factory = originals["app_http"]
        return captured

    # Import project (the adapter's shape: both kwargs present).
    captured = _drive(
        part_scale=1.0,
        part_bbox_mm=(20.0, 20.0, 20.0),
    )
    assert "part_scale" in captured, "the hook call must forward part_scale"
    assert "part_bbox_mm" in captured, "the hook call must forward part_bbox_mm"
    assert captured["part_scale"] == 1.0
    assert tuple(captured["part_bbox_mm"]) == (20.0, 20.0, 20.0)

    # No-part project (the adapter omits both): ``kwargs.get`` degrades to
    # ``None`` — the loop's own default — so a part-less run stays
    # byte-identical (the import guard must NOT fire).
    captured = _drive()
    assert captured.get("part_scale") is None
    assert captured.get("part_bbox_mm") is None
