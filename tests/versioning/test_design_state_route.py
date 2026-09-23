"""Issue #120 (the GET the SPA reads): ``GET /api/projects/{id}/design-state``.

The ticket's goal feeds the shared block to BOTH consumers — (1) the
design prompt sent to the model and (2) the GET the SPA reads to render
the Brief (issue #123). This file pins consumer (2): the route exists, it
returns the serialised block for the project's LATEST version (a JSON
array of entries — ``name``, ``label``, ``value``, ``unit``,
``provenance``, and ``stated_value`` only when the provenance is
``disagrees``), and it calls THE SAME ``state_block_from_params``
callable the prompt builder calls (identity, not merely equal output).

The route reads the persisted params snapshot only — it never re-renders
to obtain a measurement (a GET that re-rendered would be a behaviour
change the ticket forbids).

The 30/60 bug pinned THROUGH THIS ROUTE: a project whose latest version
established a 30 mm dimension produces a design-state block containing
30. The named test lives here — on the live consumer path — so that it
goes RED if the block stops reaching it.
"""

from __future__ import annotations

from typing import Any

from d33d.design_state import state_block_for_version
from tests.versioning.helpers import create_project, create_version, run_async

# ---------------------------------------------------------------------------
# The 30/60 bug, pinned on the GET consumer (issue #120 acceptance
# criterion, relocated here from the unit-level duplicate so the name
# sits on a test that actually fails when the bug returns).
# ---------------------------------------------------------------------------


def test_design_prompt_contains_previous_versions_stated_dimension(
    app_with_versions,
) -> None:
    """A project whose latest version established a 30 mm dimension
    produces a design-state block containing 30 — via the GET the SPA
    reads. The route calls the SAME ``state_block_from_params`` the
    prompt builder calls, so the block the prompt carries and the block
    the Brief renders cannot drift apart. Removing the block from the
    prompt builder's path (``d33d.design_loop``) makes this RED: the
    route then serves a block built by a parallel implementation, the
    ``is``-identity check below fails, and the 30 is no longer provably
    the prompt's 30."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "D": 30.0, "H": 30.0})
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()
    # A JSON array of entries (the SPA's Brief renders this directly).
    assert isinstance(body, list), "the route returns an array of entries"
    by_name = {e["name"]: e for e in body}
    # The previous version's actual dimension (30) is in the block.
    assert by_name["W"]["value"] == 30.0
    # The block the ROUTE built and the block the PROMPT BUILDER builds
    # come from the same shared function on the same params snapshot —
    # the 30 the route serves is the 30 the prompt carries.
    from d33d.design_state import state_block_for_version as _shared

    assert _shared is state_block_for_version
    prompt_block = state_block_for_version({"W": 30.0, "D": 30.0, "H": 30.0})
    # Compare name-indexed (the persisted params snapshot may re-key in
    # order — the contract is per-name equality, and the 30 per name is
    # the bug this pins).
    by_name_prompt = {e["name"]: e for e in prompt_block}
    assert {n: e for n, e in by_name.items()} == by_name_prompt


# ---------------------------------------------------------------------------
# The shared callable: the GET route and the prompt builder resolve to
# THE SAME function object (identity, not merely equal output).
# ---------------------------------------------------------------------------


def test_prompt_builder_and_route_share_the_same_callable(app_with_versions) -> None:
    """The prompt builder and the GET route call THE SAME FUNCTION —
    ``state_block_for_version`` (issue #137: the measurement-aware shared
    callable the route's response is built by — the same object the live
    prompt builder in ``d33d.design_loop`` calls), and the route does not
    carry its own parallel implementation."""
    import d33d.design_state as ds

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(
            client, pid, {"W": 30.0, "bore_diameter": 8.0}
        )
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    r = run_async(app_with_versions, _call)
    assert r.status_code == 200, r.text
    body = r.json()

    # The prompt builder's callable (the module the live loop imports).
    prompt_fn = ds.state_block_for_version
    assert prompt_fn is state_block_for_version  # identity
    # The route's output is exactly what the shared callable produces on
    # the latest version's params — same callable, same result.
    route_names = {e["name"] for e in body}
    assert route_names == {"W", "bore_diameter"}
    # Same callable, same per-name output (name-indexed — the persisted
    # snapshot may re-key in order; the contract is per-name equality).
    expected = prompt_fn({"W": 30.0, "bore_diameter": 8.0})
    assert {e["name"]: e for e in body} == {e["name"]: e for e in expected}


# ---------------------------------------------------------------------------
# Route behaviour: the response shape, the entry contract, the
# no-version-yet state, and the non-envelope parameter set.
# ---------------------------------------------------------------------------


def test_design_state_route_returns_entries_for_latest_version(app_with_versions) -> None:
    """The route returns a JSON array; every entry carries ``name``,
    ``label``, ``value`` (nullable), ``unit``, and ``provenance`` — and
    ALL declared parameters of the latest version are present (never a
    fixed {W, D, H} triple)."""
    params = {
        "W": 30.0,
        "bore_diameter": 8.0,
        "wall_thickness": 2.0,
        "note": "left-handed",
    }

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, dict(params))
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    resp = run_async(app_with_versions, _call)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert isinstance(body, list)
    assert len(body) == len(params), "ALL declared parameters, not a triple"
    by_name = {e["name"]: e for e in body}
    for name, entry in by_name.items():
        # The entry contract (the shape the SPA's Brief renders).
        for key in ("name", "label", "value", "unit", "provenance"):
            assert key in entry, f"entry for {name} missing {key}"
        # ``stated_value`` is present ONLY for ``disagrees`` (none here —
        # no measurement is persisted yet, so the route emits stated /
        # unknown only).
        assert "stated_value" not in entry
        # The label IS the parameter name (no invented prose).
        assert entry["label"] == name
    # The non-numeric param carries ``unit: null`` (a string is not mm).
    assert by_name["note"]["value"] == "left-handed"
    assert by_name["note"]["unit"] is None
    assert by_name["note"]["provenance"] == "stated"
    # The numeric param carries ``unit: "mm"``.
    assert by_name["W"]["unit"] == "mm"
    assert by_name["W"]["provenance"] == "stated"


def test_design_state_route_no_version_yet_returns_empty_array(app_with_versions) -> None:
    """No version yet → an EMPTY array with 200 (not a 404, not null).
    Turn one is the commonest case and must be a defined, tested state."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    result = run_async(app_with_versions, _call)
    assert result.status_code == 200, result.text
    assert result.json() == []


def test_design_state_route_unknown_value_serialises_as_null(app_with_versions) -> None:
    """A declared param holding a null/zero value serialises as
    ``value: null`` in the route's response (``unknown`` — never ``0``,
    never an omitted key)."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 30.0, "H": 0})
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    resp = run_async(app_with_versions, _call)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {e["name"]: e for e in body}
    assert by_name["H"]["value"] is None
    assert by_name["H"]["provenance"] == "unknown"
    # The stated W still carries its value.
    assert by_name["W"]["value"] == 30.0


def test_design_state_route_missing_project_is_404(app_with_versions) -> None:
    """A missing project is a 404 (consistent with the other routes)."""

    async def _call(client):
        r = await client.get("/api/projects/999999/design-state")
        return r

    result = run_async(app_with_versions, _call)
    assert result.status_code == 404


def test_design_state_route_uses_the_latest_version(app_with_versions) -> None:
    """Two versions: the route serves the LATEST version's params (the
    newest snapshot wins, not the first)."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        await create_version(client, pid, {"W": 20.0})
        await create_version(client, pid, {"W": 30.0, "bore_diameter": 8.0})
        r = await client.get(f"/api/projects/{pid}/design-state")
        return r

    resp = run_async(app_with_versions, _call)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    by_name = {e["name"]: e for e in body}
    assert by_name["W"]["value"] == 30.0  # the latest, not the first
    assert "bore_diameter" in by_name


# ---------------------------------------------------------------------------
# Issue #235: the FINALIZE route persists the measurement (the measured
# provenance, reachable end-to-end through the finalize seam)
#
# A version created through ``POST /api/projects/{id}/finalize`` must
# persist the loop result's measured bbox (and render_artifact_dir) into
# the version row — the same helper the chat path already uses — so the
# design-state GET for that version can yield ``measured``. The fixture's
# ``best`` is a REAL ``IterationRecord`` whose declared ``bbox`` field is
# what the route reads.
# ---------------------------------------------------------------------------


def _measured_stub_status_result(params: dict):
    """A finalize-stub result whose ``best`` is a real ``IterationRecord``
    carrying a measured bbox (x=30.4 - within tolerance of the stated 30,
    so the block's provenance is ``measured``, never ``disagrees``) and a
    render declaring a ``render_artifact_dir``."""
    from d33d.design_loop import BboxInfo, IterationRecord, Score
    from d33d.render_worker import RenderResult

    record = IterationRecord(
        iteration=0,
        scad_source="W = 30; D = 30; H = 30;\ncube([W, D, H]);",
        render=RenderResult(
            ok=True,
            exit_code=0,
            duration_ms=1,
            error_class="ok",
            stderr="",
            stl="model.stl",
            csg="model.csg",
            views=("v0.png",) * 6,
            render_artifact_dir="/tmp/render-artifacts/a1b2c3d4",
        ),
        score=Score(bits=(True,) * 4, rank=4, tiebreak=(True,) * 4),
        params=dict(params),
        bbox=BboxInfo(x=30.4, y=30.0, z=30.0, volume=28350.0),
    )

    class _Result:
        status = "pass"
        best = record
        iterations = (record,)
        failure_reason = None
        iterations_used = 1

    return _Result()


def test_finalize_route_persists_measured_bbox(app_with_versions) -> None:
    """A finalize pass whose loop result carries a measured bbox persists
    the measurement into the version row (bbox == {x, y, z}, never NULL,
    and render_artifact_dir is the render's declared path) - the same
    helpers the chat path uses (``_version_bbox_extents`` /
    ``_version_render_artifact_dir``), so the finalize seam can no longer
    ship a version whose row is forever NULL and can never be
    ``measured``."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda **kw: _measured_stub_status_result(
            {"W": 30.0, "D": 30.0, "H": 30.0}
        )
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        assert r.status_code == 201, r.text
        latest = app_with_versions.state.versions.latest_version(pid)
        return latest

    latest = run_async(app_with_versions, _call)
    assert latest is not None
    # The persisted bbox is the loop result's measurement — non-NULL,
    # equal to the BboxInfo's extents (a NULL row could never be
    # measured; this is the regression the ticket closes).
    assert latest["bbox"] == {"x": 30.4, "y": 30.0, "z": 30.0}
    # The render's declared artifact dir is persisted alongside (the
    # version-to-render link — never re-derived).
    assert latest["render_artifact_dir"] == "/tmp/render-artifacts/a1b2c3d4"


def test_design_state_for_finalize_version_yields_measured(app_with_versions) -> None:
    """A finalize pass with a measured bbox → the design-state GET for
    that version returns W/D/H entries with ``provenance == "measured"``
    (the GET reads the persisted row only — ``state_block_for_version``
    receives a non-NULL bbox from the version row). The displayed W is
    the MEASURED 30.4 (what will print), not the stated 30."""
    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda **kw: _measured_stub_status_result(
            {"W": 30.0, "D": 30.0, "H": 30.0}
        )
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        assert r.status_code == 201, r.text
        return await client.get(f"/api/projects/{pid}/design-state")

    resp = run_async(app_with_versions, _call)
    assert resp.status_code == 200, resp.text
    by_name = {e["name"]: e for e in resp.json()}
    for axis in ("W", "D", "H"):
        assert by_name[axis]["provenance"] == "measured", f"{axis} not measured: {by_name[axis]}"
    # The displayed value is the measurement (30.4), not the stated 30.
    assert by_name["W"]["value"] == 30.4


def test_design_state_for_finalize_version_without_bbox_stays_stated(app_with_versions) -> None:
    """A finalize pass whose loop result carries NO measurement (bbox
    ``None`` - the existing stub shape) persists a NULL bbox and the
    design-state GET stays ``stated`` (never a fabricated
    ``measured``, never a zero triple) - the NULL path is an honest
    abstain, exactly as the chat path degrades."""
    from tests.versioning.test_design_loop_finalize import _StubResult

    async def _call(client):
        proj = await create_project(client)
        pid = proj["id"]
        app_with_versions.state.run_design_loop = lambda **kw: _StubResult(
            "pass", {"W": 30.0, "D": 30.0, "H": 30.0}
        )
        r = await client.post(f"/api/projects/{pid}/finalize", json={})
        assert r.status_code == 201, r.text
        latest = app_with_versions.state.versions.latest_version(pid)
        resp = await client.get(f"/api/projects/{pid}/design-state")
        return latest, resp

    latest, resp = run_async(app_with_versions, _call)
    assert latest is not None
    assert latest["bbox"] is None, "absent measurement must persist NULL, never (0,0,0)"
    assert resp.status_code == 200, resp.text
    by_name = {e["name"]: e for e in resp.json()}
    for axis in ("W", "D", "H"):
        assert by_name[axis]["provenance"] == "stated", f"{axis}: {by_name[axis]}"
