"""Issue #380 (regression): the production design-loop closure forwards the
imported part's ``part_scale`` / ``part_bbox_mm`` through the REAL hook to
``run_design_loop_async`` — so the import guard (gated on ``part_scale is
not None``) fires on the live chat path when the model ignores the import
instruction.

The pre-fix defect: ``d33d.app._build_production_design_loop``'s ``_loop``
called ``default_run_design_loop_hook`` with an explicit kwarg list that
omitted both, so the loop ran part-less — a from-scratch ``cube()`` with
no ``import("part.stl")`` scored against the part's own 20×20×20 bbox,
passed every gate, and "edited" the imported part without touching it.

The test drives the PRODUCTION wrapper (fresh build under stubbed
pre-flight) through the UNSTUBBED hook: the LLM returns a no-import SCAD
(a 20×20×20 cube minus a 5 mm cylinder), the render is ``ok`` with a
bbox matching the part, and the import guard's ``no_import`` detection is
the ONLY thing that can fail iteration one — a gate failure would be
indistinguishable from the guard, so the test pins the guard's own
evidence text on the repair directive.

Hermetic pre-flight: the test supplies ``image_check`` (via the app
state, forwarded to the loop's ``image_check`` seam) and ``renderer_check``
(never — the conftest hermetic stub covers the ``renderer_is_available``
probe) so the loop's Docker pre-flight never shells out. On CI the render
worker image is absent while the Docker daemon IS present, so without
``image_check`` the real image probe reports ``image_missing`` and the
loop short-circuits to ``renderer_image_stale`` BEFORE iteration one —
the guard never runs and the test fails on ``failure_class is None``.
Injecting ``image_check=lambda: None`` (the hermetic "image present, label
matches" default) makes the outcome independent of the host's Docker
state, matching the conftest hermetic stub's contract.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from d33d.design_loop import BboxInfo
from d33d.render_worker import RenderResult

#: A 20×20×20 cube with a 5 mm through-hole — the bbox is 20×20×20, the
#: same as the part (only the import guard can reject this candidate).
_NO_IMPORT_SCAD = (
    "width = 20;\nheight = 20;\nhole = 5;\n"
    "difference() {\n  cube([width, width, height]);\n"
    "  translate([7.5, 7.5, 0]) cylinder(h = height, d = hole);\n}\n"
)


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]):
        self._payload = payload
        self.is_success = True

    def json(self) -> dict[str, Any]:
        return self._payload


def _no_import_llm_factory(_base: str, _key: str):
    """The closure's ``_http_request_factory`` edge (``base, key``
    signature — the closure calls ``_http_request_factory(res.provider.
    base, api_key)``), stubbed: every design-role call answers with the
    no-import SCAD (T1 fenced JSON)."""
    fenced = "```json\n" + json.dumps(
        {"tool": "emit_design", "arguments": {"scad": _NO_IMPORT_SCAD}}
    ) + "\n```"

    async def _factory(body: dict[str, Any]) -> Any:
        return _FakeResponse(
            {"choices": [{"message": {"content": fenced}}]}
        )

    return _factory


def test_production_closure_live_path_fires_import_guard(tmp_path: Path):
    """The production closure, driven with a part project's kwargs
    (``part_scale=1.0`` + the part's measured 20×20×20 bbox), reaches the
    real ``run_design_loop_async`` through the real hook and the import
    guard fires: iteration one is NOT a pass, and the repair carries the
    ``no_import`` evidence (routed via the existing ``geometrically_wrong``
    class)."""
    import d33d.app as app_mod
    import d33d.config.catalogue as cat_mod
    import d33d.config.probes as probes_mod
    import d33d.config.resolve as resolve_mod
    import d33d.db as db_mod
    import d33d.design_loop as dl_mod
    from d33d.app import _build_production_design_loop
    from d33d.config.catalogue import Catalogue, ModelEntry, Provider
    from d33d.config.probes import CapabilityResult
    from d33d.config.resolve import RoleResolution

    # A real (empty) DB so the loop's request-log / part-row reads
    # don't raise (they degrade to warnings, but a missing file path
    # would pollute the test output).
    db_path = str(tmp_path / "d33d.sqlite3")
    _conn = db_mod.connect(db_path)
    _conn.close()
    _db_path = db_path  # captured for the class body below

    provider = Provider(name="stub", base="http://stub", key="stub")
    entry = ModelEntry(id="design", provider="stub", model="stub-model")
    catalogue = Catalogue(
        source=Path("/dev/null"),
        providers={"stub": provider},
        models={"design": entry},
        roles={"design": "design"},
    )
    resolution = RoleResolution(role="design", entry=entry, provider=provider)
    # T1 with NO vision: the photo part is skipped, so the stub factory
    # never sees an image-bearing request (hermetic, no image decoding).
    capability = CapabilityResult(
        tools=False, json_schema=False, vision=False, max_images=0,
        fenced_json=True, validated=True,
    )

    def _stub_load_catalogue(_path):
        return catalogue

    def _stub_resolve_model(_cat, _role, **_kw):
        return resolution

    async def _stub_probe_capabilities(*_a, **_k):
        return capability

    class _StubAppState:
        catalogue_path = Path("/dev/null")
        db_path = Path(_db_path)
        failures_jsonl_path = Path("/dev/null")
        # The loop's pre-flight image probe (issue #346): the app forwards
        # ``getattr(app_state, "image_check", None)`` to the loop's
        # ``image_check`` seam. On CI the Docker daemon is present but the
        # render-worker image is absent, so a ``None`` here lets the REAL
        # probe report ``image_missing`` and short-circuit the loop to
        # ``renderer_image_stale`` before iteration one (the guard never
        # runs). The hermetic "image present, label matches" default is
        # ``None`` (no fault) — the same value the conftest hermetic stub
        # installs, keeping the test independent of host Docker state.
        image_check = staticmethod(lambda: None)

    bbox = BboxInfo(
        x=20.0, y=20.0, z=20.0,
        volume=8000.0,
    )
    ok_render = RenderResult(
        ok=True,
        exit_code=0,
        duration_ms=10,
        error_class="ok",
        stderr="",
        stl="model.stl",
        csg="model.csg",
        views=("v0.png", "v1.png", "v2.png", "v3.png", "v4.png", "v5.png"),
    )

    def _render_fn(_scad, _defines):
        return ok_render

    originals = {
        "cat": cat_mod.load_catalogue,
        "res": resolve_mod.resolve_model,
        "probe": probes_mod.probe_capabilities,
        "app_http": app_mod._http_request_factory,
        "renderer": dl_mod.renderer_is_available,
    }
    try:
        cat_mod.load_catalogue = _stub_load_catalogue
        resolve_mod.resolve_model = _stub_resolve_model
        probes_mod.probe_capabilities = _stub_probe_capabilities
        app_mod._http_request_factory = _no_import_llm_factory
        # The loop's pre-flight ``docker info`` probe (issue #277) — the
        # stub render must run without a Docker daemon. The image probe is
        # handled by ``_StubAppState.image_check`` (see class body above).
        dl_mod.renderer_is_available = lambda: True

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
                request="drill a hole",
                part_scale=1.0,
                part_bbox_mm=(20.0, 20.0, 20.0),
            )
        )
    finally:
        cat_mod.load_catalogue = originals["cat"]
        resolve_mod.resolve_model = originals["res"]
        probes_mod.probe_capabilities = originals["probe"]
        app_mod._http_request_factory = originals["app_http"]
        dl_mod.renderer_is_available = originals["renderer"]
        # The hook's failures.jsonl append (an exhausted result) pointed at
        # /dev/null — nothing on disk to clean.

    assert result is not None, "the loop must have run to completion"
    # The import guard fires when ``part_scale is not None`` — the
    # pre-fix bug (``part_scale`` arriving ``None`` at the loop) makes
    # the guard inert, so a no-import candidate passes. Post-fix: the
    # guard fires and the loop does NOT pass on iteration 1.
    #
    # The loop's pass condition is: ALL five gate bits True AND no
    # repair directive. The no-import candidate's bbox matches the part
    # (20×20×20 == 20×20×20) so all gates pass; the ONLY thing that can
    # fail iteration 1 is the import guard's ``no_import`` detection.
    #
    # Pre-fix (guard inert): iteration 1 has failure_class=None, repair=
    # None, all bits True → the loop passes.
    # Post-fix (guard fires): iteration 1 has failure_class=
    # "geometrically_wrong" with the no_import evidence in the repair.
    #
    # The test asserts the guard fired: either the loop did NOT pass,
    # OR (if the loop passed) iteration 1 carries the guard's evidence.
    # Both conditions together are the discriminator.
    if result.status == "pass":
        # The loop passed — the guard must have NOT fired, meaning
        # iteration 1 had no failure. This is the pre-fix bug.
        it1 = result.iterations[0]
        assert it1.failure_class is not None, (
            "a no-import candidate on an import project must NOT pass — "
            "the import guard fires when part_scale is forwarded. "
            "Got status='pass' with iteration 1 failure_class=None "
            "(the guard never saw the part)."
        )
    else:
        # The loop did not pass — verify the guard is the cause.
        it1 = result.iterations[0]
        assert it1.failure_class == "geometrically_wrong", (
            f"expected the import guard's geometrically_wrong, "
            f"got {it1.failure_class!r}"
        )
        assert it1.repair is not None
        assert 'import("part.stl")' in it1.repair.get("evidence", ""), (
            f"the repair's evidence must be the guard's no_import detail, "
            f"got: {it1.repair!r}"
        )
